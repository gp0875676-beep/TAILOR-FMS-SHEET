"""
Condition 8 (confirmed 20-Aug-2026, Hinglish): tight-window rules like
RULE_001 (5-min pre-deadline warning) need re-checking even when no new
Excel has been uploaded. This module re-evaluates every currently-PENDING
record (from the last known snapshot in the DB) against the CURRENT time,
independent of when it was last uploaded.

Dedup is handled by the exact same alert_engine.should_alert() mechanism as
the upload-triggered path, using the same (record_id, rule_id, alert_stage)
fingerprint -- so this can never double-send an alert that an upload already
sent, and vice versa.
"""
import json
import logging
from datetime import datetime, timedelta

import pandas as pd

from app.models import RecordSnapshot
from app.rule_engine import evaluate_deadline_rules
from app.message_renderer import render_deadline_alert
from app.alert_engine import should_alert
from app.config import settings, load_rules_config, business_now

logger = logging.getLogger("fms_scheduler")


def _row_from_snapshot(snap: RecordSnapshot) -> pd.Series:
    """Reconstructs a pandas Series from the record's stored raw_json --
    close enough to the original normalized row for rule evaluation.
    Date fields come back as ISO strings; rule_engine.py already handles
    str -> datetime conversion for every field it does date math on."""
    data = json.loads(snap.raw_json) if snap.raw_json else {}
    return pd.Series(data)


def evaluate_pending_records(session) -> list[tuple[str, tuple]]:
    """Returns a list of (message, fingerprint) tuples for new (not-yet-sent)
    alerts. fingerprint = (record_id, rule_id, alert_stage) -- the caller
    uses it to roll back alert_engine's dedup record if the actual Telegram
    delivery ultimately fails (see alert_engine.mark_alert_failed), so a
    failed send gets retried on the next cycle instead of being silently
    treated as "already sent forever." Caller is responsible for actually
    sending them and for closing the session."""
    rules_cfg = load_rules_config()
    now = business_now()
    recent_cutoff = now - timedelta(days=settings.RECENT_ALERT_WINDOW_DAYS)

    pending = session.query(RecordSnapshot).filter(
        RecordSnapshot.status == "PENDING", RecordSnapshot.is_removed == False  # noqa: E712
    ).all()

    results = []
    for snap in pending:
        try:
            row = _row_from_snapshot(snap)

            slip_date = row.get("slip_date")
            if isinstance(slip_date, str):
                try:
                    slip_date = pd.to_datetime(slip_date)
                except Exception:
                    slip_date = None
            is_recent = pd.notna(slip_date) and slip_date >= recent_cutoff
            if not is_recent:
                continue  # same last-N-days filter as upload-triggered alerts

            evaluations = evaluate_deadline_rules(row, rules_cfg, now=now)
            for ev in evaluations:
                if ev["rule_id"] in settings.SUPPRESSED_ALERT_RULES:
                    continue  # e.g. Delivery rules -- report only, no individual ping
                msg = render_deadline_alert(row, ev)
                if should_alert(session, snap.record_id, ev["rule_id"], ev["alert_stage"], msg,
                                 severity=ev["severity"], persist=not settings.DRY_RUN):
                    results.append((msg, (snap.record_id, ev["rule_id"], ev["alert_stage"])))
        except Exception:
            # one bad/malformed snapshot shouldn't take down the whole scan
            logger.exception(f"Failed evaluating snapshot {snap.record_id} during periodic check")
            session.rollback()

    return results
