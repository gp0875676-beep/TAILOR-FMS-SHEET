"""
Deadline evaluation engine.

For every row and every DEADLINE-category rule in rules.yaml, compute the
time remaining (or overdue duration) and decide whether a reminder threshold
has been crossed. Returns a flat list of "evaluation" dicts; alert_engine.py
is responsible for deduplication against alert_history.
"""
import pandas as pd
from datetime import datetime, timedelta


def _tier_for(slip_type: str, thresholds_cfg: dict) -> list:
    tier = "urgent" if str(slip_type).strip().lower() == "urgent" else "normal"
    return thresholds_cfg.get(tier, [])


def _evaluate_agency_to_tailor_rule(row: pd.Series, rule: dict, now: datetime) -> dict | None:
    """RULE_001: RECEIVED BY TAILOR must happen within `window_minutes` of either
    SEND TO AGENCY (if present) or ALTERATION SLIP DATE (slip-cut time), whichever
    applies. Excludes plain saree items. Single pre-deadline alert + overdue escalation
    (not a multi-tier reminder ladder like the generic DEADLINE rules)."""
    item_name = str(row.get("item_name", "")).strip()
    if item_name in rule.get("excluded_items", []):
        return None

    if pd.notna(row.get("received_by_tailor")):
        return None  # already received, nothing to alert on

    reference_time = row.get("sent_to_agency")
    if pd.isna(reference_time):
        reference_time = row.get("slip_date")
    if pd.isna(reference_time):
        return None  # can't compute a deadline without a reference point

    if isinstance(reference_time, str):
        try:
            reference_time = pd.to_datetime(reference_time)
        except Exception:
            return None

    deadline = reference_time + timedelta(minutes=rule["window_minutes"])
    remaining_minutes = (deadline - now).total_seconds() / 60.0
    pre_window = rule.get("pre_deadline_alert_minutes", 5)

    if remaining_minutes <= 0:
        return {
            "rule_id": rule["id"],
            "stage": rule["stage"],
            "alert_stage": "OVERDUE",
            "severity": rule.get("severity_overdue", "MOST_URGENT"),
            "deadline": deadline,
            "remaining_minutes": remaining_minutes,
        }
    if remaining_minutes <= pre_window:
        return {
            "rule_id": rule["id"],
            "stage": rule["stage"],
            "alert_stage": f"{pre_window}m_pre_deadline",
            "severity": rule.get("severity_pre_deadline", "CRITICAL"),
            "deadline": deadline,
            "remaining_minutes": remaining_minutes,
        }
    return None


def _evaluate_stage_deadline_tiered(row: pd.Series, rule: dict, now: datetime) -> dict | None:
    """Generic tiered deadline alert with per-rule custom minute thresholds
    (unlike the global reminder_thresholds tiers, these are the same for Normal
    and Urgent unless the rule config says otherwise). Used for RULE_002 (tailor
    stage completion), RULE_007 (finishing stage completion), and any future
    stage with its own exact SLA numbers.

    Supports both pre-deadline tiers (`tiers`) and post-deadline / overdue
    tiers (`overdue_tiers`, e.g. "alert again 15 min after deadline passes").
    If a rule has no `overdue_tiers`, it falls back to a single flat
    `overdue_severity` the moment the deadline passes (RULE_002's behavior).

    `upstream_field`, if set, must be non-null before this rule evaluates at
    all -- e.g. don't alert on the FINISHING deadline for a piece that hasn't
    even finished TAILOR yet. Confirmed real-world need: in the actual
    workbook, downstream deadline dates (FINISHING DATE, QC DEADLINE, etc.)
    are often pre-scheduled at slip-creation time, long before the piece
    reaches that stage -- without this guard, 243 rows in the real workbook
    triggered a premature FINISHING alert while still stuck at TAILOR."""
    deadline_field = rule["deadline_field"]
    complete_field = rule["complete_field"]

    upstream_field = rule.get("upstream_field")
    if upstream_field and pd.isna(row.get(upstream_field)):
        return None  # piece hasn't reached this stage yet -- too early to alert

    complete = row.get(complete_field)
    if pd.notna(complete):
        return None  # already completed

    deadline = row.get(deadline_field)
    if pd.isna(deadline):
        return None  # handled separately as DQ_006 (missing deadline)

    if isinstance(deadline, str):
        try:
            deadline = pd.to_datetime(deadline)
        except Exception:
            return None

    remaining_minutes = (deadline - now).total_seconds() / 60.0

    if remaining_minutes <= 0:
        if rule.get("suppress_overdue"):
            return None  # this rule was specified as pre-deadline reminders only

        overdue_minutes = -remaining_minutes
        overdue_tiers = rule.get("overdue_tiers")

        if overdue_tiers:
            # pick the MOST escalated tier crossed so far (largest minutes-overdue threshold met)
            crossed = [t for t in overdue_tiers if overdue_minutes >= t["minutes"]]
            if not crossed:
                return None  # shouldn't happen if a 0-minute tier is configured, but be safe
            best = max(crossed, key=lambda t: t["minutes"])
            return {
                "rule_id": rule["id"],
                "stage": rule["stage"],
                "alert_stage": best["label"],
                "severity": best["severity"],
                "deadline": deadline,
                "remaining_minutes": remaining_minutes,
            }

        return {
            "rule_id": rule["id"],
            "stage": rule["stage"],
            "alert_stage": "OVERDUE",
            "severity": rule.get("overdue_severity", "MOST_URGENT"),
            "deadline": deadline,
            "remaining_minutes": remaining_minutes,
        }

    crossed = [t for t in rule["tiers"] if remaining_minutes <= t["minutes"]]
    if crossed:
        best = min(crossed, key=lambda t: t["minutes"])
        return {
            "rule_id": rule["id"],
            "stage": rule["stage"],
            "alert_stage": best["label"],
            "severity": best["severity"],
            "deadline": deadline,
            "remaining_minutes": remaining_minutes,
        }
    return None


def _evaluate_missing_deadline_but_completed(row: pd.Series, rule: dict) -> dict | None:
    """RULE_012: flags a data-consistency problem as a BUSINESS alert (not filed
    quietly under DQ_*) -- a stage was marked complete even though its deadline
    field was never filled in. Fires every upload until the data is corrected
    (dedup handles not re-sending it every single upload)."""
    deadline_field = rule["deadline_field"]
    complete_field = rule["complete_field"]

    if pd.isna(row.get(deadline_field)) and pd.notna(row.get(complete_field)):
        return {
            "rule_id": rule["id"],
            "stage": rule["stage"],
            "alert_stage": "MISSING_DEADLINE_DATA",
            "severity": rule.get("severity", "URGENT"),
            "deadline": None,
            "remaining_minutes": None,
        }
    return None


def evaluate_deadline_rules(row: pd.Series, rules_cfg: dict, now: datetime = None) -> list:
    now = now or datetime.utcnow()
    thresholds_cfg = rules_cfg["reminder_thresholds"]
    overdue_severity = rules_cfg.get("overdue_severity", "OVERDUE")
    evaluations = []

    for rule in rules_cfg["rules"]:
        if not rule.get("enabled", True):
            continue

        category = rule.get("category")

        if category == "AGENCY_TO_TAILOR_SLA":
            ev = _evaluate_agency_to_tailor_rule(row, rule, now)
            if ev:
                evaluations.append(ev)
            continue

        if category == "STAGE_DEADLINE_TIERED":
            ev = _evaluate_stage_deadline_tiered(row, rule, now)
            if ev:
                evaluations.append(ev)
            continue

        if category == "MISSING_DEADLINE_ALERT":
            ev = _evaluate_missing_deadline_but_completed(row, rule)
            if ev:
                evaluations.append(ev)
            continue

        if category != "DEADLINE":
            continue

        deadline_field = rule["deadline_field"]
        complete_field = rule["complete_field"]

        upstream_field = rule.get("upstream_field")
        if upstream_field and pd.isna(row.get(upstream_field)):
            continue  # piece hasn't reached this stage yet -- too early to alert

        deadline = row.get(deadline_field)
        complete = row.get(complete_field)

        # already completed -> no active deadline alert for this stage
        if pd.notna(complete):
            continue
        if pd.isna(deadline):
            continue  # handled separately as DQ_006 (missing deadline)

        if isinstance(deadline, str):
            try:
                deadline = pd.to_datetime(deadline)
            except Exception:
                continue

        remaining_minutes = (deadline - now).total_seconds() / 60.0
        tiers = _tier_for(row.get("slip_type"), thresholds_cfg)

        if remaining_minutes <= 0:
            evaluations.append({
                "rule_id": rule["id"],
                "stage": rule["stage"],
                "alert_stage": "OVERDUE",
                "severity": overdue_severity,
                "deadline": deadline,
                "remaining_minutes": remaining_minutes,
            })
            continue

        # pick the tightest crossed threshold (smallest minutes value that we're now within)
        crossed = [t for t in tiers if remaining_minutes <= t["minutes"]]
        if crossed:
            best = min(crossed, key=lambda t: t["minutes"])
            evaluations.append({
                "rule_id": rule["id"],
                "stage": rule["stage"],
                "alert_stage": best["label"],
                "severity": best["severity"],
                "deadline": deadline,
                "remaining_minutes": remaining_minutes,
            })

    return evaluations
