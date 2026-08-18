from app.config import settings

STAGE_LABELS_HI = {
    "AGENCY": "एजेंसी",
    "TAILOR": "टेलर",
    "FINISHING": "फिनिशिंग",
    "PACKING": "पैकिंग",
    "DELIVERY": "डिलीवरी",
}

SEVERITY_EMOJI = {
    "INFO": "ℹ️",
    "WARNING": "⚠️",
    "URGENT": "🚨",
    "CRITICAL": "🔥",
    "OVERDUE": "🔴",
    "MOST_URGENT": "🆘",
    "DATA_QUALITY": "⚠️",
}


def _fmt_remaining(minutes: float) -> str:
    m_total = abs(int(minutes))
    h, m = divmod(m_total, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _header_label(evaluation: dict) -> str:
    stage_raw = evaluation["alert_stage"]
    if stage_raw == "OVERDUE":
        return "OVERDUE"
    if stage_raw == "MISSING_DEADLINE_DATA":
        return "DATA ISSUE"
    if stage_raw.endswith("m_pre_deadline"):
        mins = stage_raw.split("m_")[0]
        return f"{mins} MIN WARNING"
    return stage_raw


def render_deadline_alert(row, evaluation: dict) -> str:
    emoji = SEVERITY_EMOJI.get(evaluation["severity"], "🚨")
    stage = evaluation["stage"]

    if evaluation["alert_stage"] == "MISSING_DEADLINE_DATA":
        return (
            f"{emoji} FMS ALERT — {stage} {_header_label(evaluation)}\n\n"
            f"🧾 Slip No: {row.get('slip_no')}\n"
            f"📦 Item: {row.get('item_name')}\n"
            f"🏷️ Type: {row.get('slip_type')}\n"
            f"🔖 RFID: {row.get('rfid')}\n\n"
            f"📍 Stage: {stage}\n\n"
            f"⚠️ Issue: {stage.title()} was marked complete but its deadline field "
            f"was never filled in — please check this slip's data.\n\n"
            f"🔥 Severity: {evaluation['severity']}\n"
            f"Rule: {evaluation['rule_id']}"
        )

    deadline = evaluation["deadline"]
    remaining = evaluation["remaining_minutes"]
    lang = settings.LANGUAGE

    lines = [
        f"{emoji} FMS ALERT — {stage} {_header_label(evaluation)}",
        "",
        f"🧾 Slip No: {row.get('slip_no')}",
        f"📦 Item: {row.get('item_name')}",
        f"🏷️ Type: {row.get('slip_type')}",
        f"🔖 RFID: {row.get('rfid')}",
        "",
        f"📍 Stage: {stage}",
    ]
    if lang in ("hi", "both") and stage in STAGE_LABELS_HI:
        lines.append(f"स्टेज: {STAGE_LABELS_HI[stage]}")

    responsible = row.get("tailor_name") if stage in ("TAILOR", "AGENCY") else row.get("finish_name")
    if responsible and str(responsible) != "nan":
        lines.append(f"👤 Responsible: {responsible}")

    lines += [
        "",
        f"⏰ Deadline: {deadline}",
        f"⌛ {'Overdue by' if remaining <= 0 else 'Remaining'}: {_fmt_remaining(remaining)}",
        "",
        f"🔥 Severity: {evaluation['severity']}",
        f"Rule: {evaluation['rule_id']}",
    ]
    return "\n".join(lines)


def render_anomaly(anomaly: dict) -> str:
    return (
        f"⚠️ DATA ANOMALY [{anomaly['dq_rule_id']}]\n\n"
        f"Record: {anomaly['record_id']}\n"
        f"Issue: {anomaly['description']}\n\n"
        "This row requires review. Source data unchanged."
    )


def render_upload_summary(upload_meta: dict) -> str:
    return (
        "📊 FMS UPLOAD SUMMARY\n\n"
        f"File: {upload_meta['filename']}\n"
        f"Rows: {upload_meta['row_count']}\n\n"
        f"🆕 New: {upload_meta['new_records']}\n"
        f"🔄 Updated: {upload_meta['updated_records']}\n"
        f"✅ Completed: {upload_meta['completed_records']}\n\n"
        f"📨 Alerts Sent: {upload_meta['new_alerts']}\n"
        f"🔕 Duplicates Suppressed: {upload_meta.get('duplicates_suppressed', 0)}\n"
        f"⚠️ Data Quality Issues: {upload_meta.get('anomaly_count', 0)}\n\n"
        f"Validation: {upload_meta['validation_status']}"
    )


def render_stopped_items_report(stopped_items: list, max_chars: int = 3500) -> list[str]:
    """RULE_012-adjacent feature (Condition 7): after every upload, list every
    piece whose deadline has actually passed (time nikal gaya) and it's still
    stuck there. Deliberately minimal -- just Slip No + Stage, per user's spec.

    Returns a LIST of message chunks (not a single string) because Telegram
    caps messages at 4096 chars and a full-workbook overdue list can easily
    exceed that -- e.g. 338 items = ~8000 chars in the real workbook."""
    if not stopped_items:
        return []

    header = f"🛑 STOPPED ITEMS REPORT\n\n({len(stopped_items)} item(s) past their deadline)\n\n"
    lines = [f"Slip {slip_no} — {stage}" for slip_no, stage in stopped_items]

    chunks = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > max_chars:
            chunks.append(current.rstrip())
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())

    # number the chunks if there's more than one, so it reads as a continuation
    if len(chunks) > 1:
        chunks = [f"{c}\n\n(part {i+1}/{len(chunks)})" for i, c in enumerate(chunks)]

    return chunks


def render_status_change(row, from_stage: str, to_stage: str) -> str:
    return f"✅ Status Change\n\nSlip {row.get('slip_no')}\n{from_stage} → {to_stage}"
