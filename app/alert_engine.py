import hashlib
from app.models import AlertHistory
from app.config import business_now


def _fingerprint(record_id: str, rule_id: str, alert_stage: str) -> str:
    return hashlib.sha256(f"{record_id}|{rule_id}|{alert_stage}".encode()).hexdigest()


def should_alert(session, record_id: str, rule_id: str, alert_stage: str, message: str) -> bool:
    """Returns True (and records the alert) only if this exact fingerprint
    hasn't already been sent. A new alert_stage (e.g. escalating from '1h' to
    'OVERDUE') is a different fingerprint and WILL alert again."""
    now = business_now()
    msg_hash = hashlib.sha256(message.encode()).hexdigest()

    existing = (
        session.query(AlertHistory)
        .filter_by(record_id=record_id, rule_id=rule_id, alert_stage=alert_stage)
        .one_or_none()
    )

    if existing:
        existing.last_seen_at = now
        if existing.status == "ACTIVE":
            # already alerted for this exact stage -- suppress duplicate
            session.commit()
            return False
        # was resolved, now re-triggered (rule reset) -> alert again
        existing.status = "ACTIVE"
        existing.last_triggered_at = now
        existing.alert_count = (existing.alert_count or 0) + 1
        existing.message_hash = msg_hash
        session.commit()
        return True

    session.add(AlertHistory(
        record_id=record_id,
        rule_id=rule_id,
        alert_stage=alert_stage,
        severity=alert_stage,
        first_triggered_at=now,
        last_triggered_at=now,
        last_seen_at=now,
        alert_count=1,
        status="ACTIVE",
        message_hash=msg_hash,
    ))
    session.commit()
    return True


def resolve_alerts_for_completed(session, record_id: str):
    """When a record completes, mark its open alerts RESOLVED so a future
    re-open (e.g. data correction) can re-trigger cleanly."""
    rows = session.query(AlertHistory).filter_by(record_id=record_id, status="ACTIVE").all()
    for r in rows:
        r.status = "RESOLVED"
    session.commit()


def mark_alert_failed(session, record_id: str, rule_id: str, alert_stage: str):
    """Confirmed real production bug (23-Aug-2026): should_alert() marks a
    fingerprint as ACTIVE the moment it's EVALUATED, not once it's actually
    delivered. If the real Telegram send later fails (network timeout, pool
    exhaustion under a message flood, etc.) even after retries, the alert was
    permanently treated as 'already sent' and would never be retried on a
    future upload or periodic check -- even though the user never received it.

    Call this after a send ultimately fails so the fingerprint resets and the
    NEXT evaluation (next upload or next periodic check) tries again."""
    row = (
        session.query(AlertHistory)
        .filter_by(record_id=record_id, rule_id=rule_id, alert_stage=alert_stage)
        .one_or_none()
    )
    if not row:
        return
    if row.alert_count == 1:
        # this was its first-ever attempt and it failed outright -- delete so
        # should_alert() treats it as brand new on the next evaluation
        session.delete(row)
    else:
        # had succeeded before (e.g. a prior escalation), this particular
        # re-trigger attempt failed -- mark RESOLVED so it can retrigger cleanly
        row.status = "RESOLVED"
    session.commit()
