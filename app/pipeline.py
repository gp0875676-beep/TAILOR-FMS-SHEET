import time
import hashlib
import pandas as pd
from datetime import datetime, timedelta

from app.validator import validate_workbook, file_hash
from app.excel_parser import normalize, record_id
from app.snapshot_manager import diff_snapshot
from app.rule_engine import evaluate_deadline_rules
from app.anomaly_detector import detect_anomalies
from app.alert_engine import should_alert, resolve_alerts_for_completed
from app.message_renderer import (
    render_deadline_alert, render_anomaly, render_upload_summary, render_stopped_items_report
)
from app.models import Upload, Anomaly
from app.config import settings, load_rules_config, business_now


class ProcessingError(Exception):
    def __init__(self, user_message: str):
        self.user_message = user_message
        super().__init__(user_message)


def process_upload(session, file_path: str, filename: str, telegram_user_id: str,
                    telegram_chat_id: str, send_fn) -> dict:
    """
    send_fn(chat_id, text, fingerprint=None) -> queues/sends a Telegram message
    (or no-ops in DRY_RUN). `fingerprint`, when set, is (record_id, rule_id,
    alert_stage) -- the caller uses it to roll back alert_engine's dedup
    record if the actual Telegram delivery ultimately fails, so a failed send
    gets retried instead of being silently treated as "already sent forever."
    Returns the summary dict that was sent, for /status caching etc.
    """
    started = time.time()

    fhash = file_hash(file_path)
    prior_same_hash = session.query(Upload).filter_by(file_hash=fhash).first()

    validation, df = validate_workbook(file_path)

    if not validation.ok:
        raise ProcessingError(
            f"❌ FMS UPLOAD FAILED\n\nReason:\n{validation.reason}\n\nFile:\n{filename}\n\nNo alerts were generated."
        )

    if prior_same_hash is not None:
        send_fn(telegram_chat_id,
                f"⚠️ IDENTICAL FILE\n\nThis file is identical to a previous upload "
                f"(upload #{prior_same_hash.id}).\n\nNo data changes detected.\nDuplicate alerts suppressed.")
        return {"skipped": True, "reason": "identical_file"}

    df_norm = normalize(df)
    if validation.invalid_row_indices:
        df_norm = df_norm.drop(index=validation.invalid_row_indices)

    upload = Upload(
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        filename=filename,
        upload_timestamp=business_now(),
        file_hash=fhash,
        row_count=validation.row_count,
        column_count=validation.column_count,
        validation_status=validation.status,
        processing_status="RUNNING",
    )
    session.add(upload)
    session.commit()

    try:
        diff = diff_snapshot(session, df_norm, upload.id)

        rules_cfg = load_rules_config()
        new_alerts = 0
        duplicates_suppressed = 0

        # resolve alerts for newly-completed records
        for rid, row, stage, status in diff.completed:
            resolve_alerts_for_completed(session, rid)

        # Confirmed with user 20-Aug-2026: individual Telegram pings only fire
        # for records whose ALTERATION SLIP DATE is within the last N days
        # (RECENT_ALERT_WINDOW_DAYS, default 7). Older records -- e.g. a big
        # historical backfill upload -- still get evaluated and still show up
        # in the Stopped Items / Pending Report, they just don't flood the chat
        # with hundreds of individual messages.
        now = business_now()
        recent_cutoff = now - timedelta(days=settings.RECENT_ALERT_WINDOW_DAYS)

        # evaluate deadline rules across all currently-pending rows
        pending_groups = diff.new + [(r[0], r[1], r[2], r[3]) for r in diff.updated] + diff.unchanged
        stopped_items = []  # Condition 7: (slip_no, stage) for anything whose deadline has actually passed
        for rid, row, stage, status in pending_groups:
            if status == "COMPLETED":
                continue

            slip_date = row.get("slip_date")
            if isinstance(slip_date, str):
                try:
                    slip_date = pd.to_datetime(slip_date)
                except Exception:
                    slip_date = None
            is_recent = pd.notna(slip_date) and slip_date >= recent_cutoff

            evaluations = evaluate_deadline_rules(row, rules_cfg, now=now)
            for ev in evaluations:
                # "time nikal gaya" = remaining_minutes is a real number and <= 0.
                # Tracked for the report regardless of recency -- old AND new
                # overdue items both belong in the Pending Report.
                if ev.get("remaining_minutes") is not None and ev["remaining_minutes"] <= 0:
                    stopped_items.append((row.get("slip_no"), ev["stage"]))

                if not is_recent:
                    continue  # older than the window -- report only, no individual ping

                if ev["rule_id"] in settings.SUPPRESSED_ALERT_RULES:
                    continue  # e.g. Delivery rules -- report only, no individual ping

                msg = render_deadline_alert(row, ev)
                if should_alert(session, rid, ev["rule_id"], ev["alert_stage"], msg):
                    if not settings.DRY_RUN:
                        send_fn(telegram_chat_id, msg, (rid, ev["rule_id"], ev["alert_stage"]))
                    new_alerts += 1
                else:
                    duplicates_suppressed += 1

        # de-dupe (slip_no, stage) pairs -- a row can be re-evaluated across new/updated/unchanged groups
        stopped_items = sorted(set(stopped_items), key=lambda x: (str(x[1]), str(x[0])))

        # anomaly detection (separate channel)
        anomalies = detect_anomalies(df_norm)
        for a in anomalies:
            exists = session.query(Anomaly).filter_by(
                record_id=a["record_id"], dq_rule_id=a["dq_rule_id"], status="OPEN"
            ).first()
            if exists:
                continue
            session.add(Anomaly(
                record_id=a["record_id"], dq_rule_id=a["dq_rule_id"],
                description=a["description"], detected_at=business_now(),
                upload_id=upload.id, status="OPEN",
            ))
        session.commit()

        summary = {
            "filename": filename,
            "row_count": validation.row_count,
            "new_records": len(diff.new),
            "updated_records": len(diff.updated),
            "completed_records": len(diff.completed),
            "new_alerts": new_alerts,
            "duplicates_suppressed": duplicates_suppressed,
            "anomaly_count": len(anomalies),
            "validation_status": validation.status,
        }

        upload.processing_status = "SUCCESS"
        upload.processing_duration_ms = int((time.time() - started) * 1000)
        upload.new_records = summary["new_records"]
        upload.updated_records = summary["updated_records"]
        upload.completed_records = summary["completed_records"]
        upload.new_alerts = summary["new_alerts"]
        upload.duplicates_suppressed = summary["duplicates_suppressed"]
        upload.anomaly_count = summary["anomaly_count"]
        upload.stopped_items_count = len(stopped_items)
        session.commit()

        send_fn(telegram_chat_id, render_upload_summary(summary))

        # Condition 7: stopped-items report, sent right after the summary.
        # Chunked because Telegram caps messages at 4096 chars.
        stopped_report_chunks = render_stopped_items_report(stopped_items)
        if stopped_report_chunks and not settings.DRY_RUN:
            for chunk in stopped_report_chunks:
                send_fn(telegram_chat_id, chunk)
        summary["stopped_items_count"] = len(stopped_items)

        return summary

    except Exception as e:
        upload.processing_status = "FAILED"
        upload.errors = str(e)
        session.commit()
        raise ProcessingError(
            f"❌ FMS PROCESSING FAILED\n\nFile:\n{filename}\n\nTechnical details have been logged."
        ) from e
