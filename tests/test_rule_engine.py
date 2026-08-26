import pandas as pd
from datetime import datetime, timedelta
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.rule_engine import evaluate_deadline_rules
from app.excel_parser import determine_stage
from app.config import load_rules_config


def _base_row(**overrides):
    row = {
        "slip_no": "1001", "rfid": "RFID1", "item_name": "TEST", "slip_type": "Normal",
        "slip_date": None, "sent_to_agency": None, "received_by_tailor": None, "tailor_name": None,
        "tailor_deadline": None, "tailor_complete": None,
        "finishing_deadline": None, "finishing_complete": None, "finish_name": None,
        "qc_deadline": None, "packing_complete": None,
        "delivery_deadline": None, "delivered_customer": None,
    }
    row.update(overrides)
    return pd.Series(row)


def test_stage_not_started():
    row = _base_row()
    stage, status = determine_stage(row)
    assert stage == "NOT_STARTED"
    assert status == "PENDING"


def test_stage_completed():
    row = _base_row(delivered_customer=datetime.utcnow())
    stage, status = determine_stage(row)
    assert stage == "COMPLETED"
    assert status == "COMPLETED"


def test_qc_packing_overdue_triggers_overdue_severity():
    """RULE_008 (QC/Packing deadline) still uses the generic global-tier engine --
    its exact thresholds haven't been confirmed by the user yet, so it's still
    running on defaults."""
    cfg = load_rules_config()
    row = _base_row(
        finishing_complete=datetime.utcnow() - timedelta(days=1),
        qc_deadline=datetime.utcnow() - timedelta(hours=1),  # deadline already passed
    )
    evals = evaluate_deadline_rules(row, cfg)
    qc_evals = [e for e in evals if e["rule_id"] == "RULE_008"]
    assert len(qc_evals) == 1
    assert qc_evals[0]["alert_stage"] == "OVERDUE"


def test_qc_packing_urgent_gets_tighter_threshold_than_normal():
    """Also generic global-tier behavior (RULE_008, default/unconfirmed) --
    RULE_002 (Tailor) and RULE_007 (Finishing), both confirmed, deliberately do
    NOT split by slip_type -- see the RULE_002/RULE_007 tests below."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    deadline = now + timedelta(minutes=45)  # 45 min out

    normal_row = _base_row(slip_type="Normal", finishing_complete=now - timedelta(days=1), qc_deadline=deadline)
    urgent_row = _base_row(slip_type="Urgent", finishing_complete=now - timedelta(days=1), qc_deadline=deadline)

    normal_evals = evaluate_deadline_rules(normal_row, cfg, now=now)
    urgent_evals = evaluate_deadline_rules(urgent_row, cfg, now=now)

    # at 45 min remaining: normal tier only fires <=60m ("1h"); urgent tier fires <=180m ("3h")
    normal_stages = [e["alert_stage"] for e in normal_evals if e["rule_id"] == "RULE_008"]
    urgent_stages = [e["alert_stage"] for e in urgent_evals if e["rule_id"] == "RULE_008"]
    assert "1h" in normal_stages
    assert "3h" in urgent_stages


def test_completed_stage_produces_no_deadline_alert():
    cfg = load_rules_config()
    row = _base_row(
        tailor_deadline=datetime.utcnow() - timedelta(hours=5),
        tailor_complete=datetime.utcnow() - timedelta(hours=6),  # completed before deadline
    )
    evals = evaluate_deadline_rules(row, cfg)
    assert all(e["rule_id"] != "RULE_002" for e in evals)


# -------------------- RULE_001: Slip -> Tailor receipt within 1 hour --------------------

def _rule001_row(item_name, sent_to_agency=None, slip_date=None, received_by_tailor=None):
    return _base_row(
        item_name=item_name,
        sent_to_agency=sent_to_agency,
        slip_date=slip_date,
        received_by_tailor=received_by_tailor,
    )


def test_rule001_fires_5min_before_deadline():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule001_row("RMN-DRESS", slip_date=now - timedelta(minutes=58))
    evals = evaluate_deadline_rules(row, cfg, now=now)
    r1 = [e for e in evals if e["rule_id"] == "RULE_001"]
    assert len(r1) == 1
    assert r1[0]["severity"] == "CRITICAL"


def test_rule001_excludes_plain_saree():
    cfg = load_rules_config()
    now = datetime.utcnow()
    for excluded in ("SAREE", "RMN-D.SAREE", "SAREE(NO LESS)"):
        row = _rule001_row(excluded, slip_date=now - timedelta(minutes=58))
        evals = evaluate_deadline_rules(row, cfg, now=now)
        assert all(e["rule_id"] != "RULE_001" for e in evals), f"{excluded} should be excluded"


def test_rule001_does_not_exclude_saree_stitch_or_blouse():
    cfg = load_rules_config()
    now = datetime.utcnow()
    for included in ("SAREE STITCH", "SAREE BLOUSE SET"):
        row = _rule001_row(included, slip_date=now - timedelta(minutes=58))
        evals = evaluate_deadline_rules(row, cfg, now=now)
        assert any(e["rule_id"] == "RULE_001" for e in evals), f"{included} should NOT be excluded"


def test_rule001_overdue_escalates_to_most_urgent():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule001_row("RMN-SUIT", sent_to_agency=now - timedelta(minutes=65))
    evals = evaluate_deadline_rules(row, cfg, now=now)
    r1 = [e for e in evals if e["rule_id"] == "RULE_001"]
    assert len(r1) == 1
    assert r1[0]["alert_stage"] == "OVERDUE"
    assert r1[0]["severity"] == "MOST_URGENT"


def test_rule001_uses_agency_time_over_slip_date_when_both_present():
    cfg = load_rules_config()
    now = datetime.utcnow()
    # slip cut 3 hours ago (would be very overdue), but sent to agency only 58 min ago
    # -> reference time should be sent_to_agency, so this is still within the pre-deadline window
    row = _rule001_row(
        "RMN-SUIT",
        slip_date=now - timedelta(hours=3),
        sent_to_agency=now - timedelta(minutes=58),
    )
    evals = evaluate_deadline_rules(row, cfg, now=now)
    r1 = [e for e in evals if e["rule_id"] == "RULE_001"]
    assert len(r1) == 1
    assert r1[0]["alert_stage"] == "5m_pre_deadline"  # not OVERDUE


def test_rule001_no_alert_when_already_received():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule001_row("RMN-SUIT", slip_date=now - timedelta(minutes=58),
                        received_by_tailor=now - timedelta(minutes=1))
    evals = evaluate_deadline_rules(row, cfg, now=now)
    assert all(e["rule_id"] != "RULE_001" for e in evals)


def test_rule001_no_alert_when_far_from_deadline():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule001_row("RMN-SUIT", slip_date=now - timedelta(minutes=20))
    evals = evaluate_deadline_rules(row, cfg, now=now)
    assert all(e["rule_id"] != "RULE_001" for e in evals)


# -------------------- RULE_002: Tailor stage completion deadline --------------------

def _rule002_row(tailor_deadline=None, tailor_complete=None, slip_type="Normal"):
    # received_by_tailor defaults to "already received" so the upstream-stage
    # guard doesn't block these deadline-math tests; explicit-None tests for
    # the guard itself are separate (see test_rule002_no_alert_before_received).
    return _base_row(
        tailor_deadline=tailor_deadline, tailor_complete=tailor_complete, slip_type=slip_type,
        received_by_tailor=datetime.utcnow() - timedelta(hours=1),
    )


def test_rule002_20min_warning():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule002_row(tailor_deadline=now + timedelta(minutes=18))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_002"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "20m_pre_deadline"
    assert evals[0]["severity"] == "URGENT"


def test_rule002_10min_most_urgent():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule002_row(tailor_deadline=now + timedelta(minutes=8))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_002"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "10m_pre_deadline"
    assert evals[0]["severity"] == "MOST_URGENT"


def test_rule002_overdue():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule002_row(tailor_deadline=now - timedelta(minutes=1))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_002"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "OVERDUE"
    assert evals[0]["severity"] == "MOST_URGENT"


def test_rule002_same_thresholds_for_normal_and_urgent():
    cfg = load_rules_config()
    now = datetime.utcnow()
    normal = _rule002_row(tailor_deadline=now + timedelta(minutes=8), slip_type="Normal")
    urgent = _rule002_row(tailor_deadline=now + timedelta(minutes=8), slip_type="Urgent")
    n_ev = [e for e in evaluate_deadline_rules(normal, cfg, now=now) if e["rule_id"] == "RULE_002"]
    u_ev = [e for e in evaluate_deadline_rules(urgent, cfg, now=now) if e["rule_id"] == "RULE_002"]
    assert n_ev[0]["alert_stage"] == u_ev[0]["alert_stage"] == "10m_pre_deadline"


def test_rule002_no_alert_when_completed():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule002_row(tailor_deadline=now - timedelta(minutes=5), tailor_complete=now - timedelta(minutes=10))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_002"]
    assert evals == []


def test_rule002_no_alert_when_more_than_20min_left():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule002_row(tailor_deadline=now + timedelta(minutes=45))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_002"]
    assert evals == []


def test_rule002_no_alert_before_received_by_tailor():
    """Upstream guard: if the piece hasn't even been received by the tailor
    yet, RULE_002 (tailor completion deadline) must not fire -- that would be
    RULE_001's territory (slip -> tailor receipt SLA)."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _base_row(tailor_deadline=now + timedelta(minutes=5), received_by_tailor=None)
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_002"]
    assert evals == []


# -------------------- RULE_007: Finishing stage completion deadline (3 alerts) --------------------

def _rule007_row(finishing_deadline=None, finishing_complete=None, slip_type="Normal"):
    # tailor_complete defaults to "already done" so the upstream-stage guard
    # doesn't block these deadline-math tests (see test_rule007 upstream guard test).
    return _base_row(
        finishing_deadline=finishing_deadline, finishing_complete=finishing_complete, slip_type=slip_type,
        tailor_complete=datetime.utcnow() - timedelta(hours=1),
    )


def test_rule007_alert1_15min_before():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule007_row(finishing_deadline=now + timedelta(minutes=12))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_007"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "15m_pre_deadline"
    assert evals[0]["severity"] == "URGENT"


def test_rule007_alert2_at_deadline():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule007_row(finishing_deadline=now - timedelta(minutes=1))  # just passed, not yet +15
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_007"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "OVERDUE"
    assert evals[0]["severity"] == "MOST_URGENT"


def test_rule007_alert3_15min_after_deadline():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule007_row(finishing_deadline=now - timedelta(minutes=16))  # 16 min overdue -> past the +15 mark
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_007"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "15m_post_deadline"
    assert evals[0]["severity"] == "MOST_URGENT"


def test_rule007_exactly_3_distinct_alert_points():
    """Confirms there are exactly 3 alert stages possible for RULE_007, as requested."""
    cfg = load_rules_config()
    rule = next(r for r in cfg["rules"] if r["id"] == "RULE_007")
    total_alert_points = len(rule["tiers"]) + len(rule["overdue_tiers"])
    assert total_alert_points == 3


def test_rule007_no_alert_when_completed():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule007_row(finishing_deadline=now - timedelta(minutes=20), finishing_complete=now - timedelta(minutes=30))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_007"]
    assert evals == []


def test_rule007_same_thresholds_for_normal_and_urgent():
    cfg = load_rules_config()
    now = datetime.utcnow()
    normal = _rule007_row(finishing_deadline=now + timedelta(minutes=10), slip_type="Normal")
    urgent = _rule007_row(finishing_deadline=now + timedelta(minutes=10), slip_type="Urgent")
    n_ev = [e for e in evaluate_deadline_rules(normal, cfg, now=now) if e["rule_id"] == "RULE_007"]
    u_ev = [e for e in evaluate_deadline_rules(urgent, cfg, now=now) if e["rule_id"] == "RULE_007"]
    assert n_ev[0]["alert_stage"] == u_ev[0]["alert_stage"] == "15m_pre_deadline"


def test_rule007_no_alert_before_tailor_complete():
    """The real bug: user reported pieces still stuck at TAILOR/AGENCY showing
    up as FINISHING alerts, because FINISHING DATE is often pre-scheduled in
    the workbook long before TAILOR actually finishes. Confirmed against the
    real workbook: 243 rows had finishing_deadline set with tailor_complete
    still empty. This guard must suppress RULE_007 for all of them."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _base_row(finishing_deadline=now + timedelta(minutes=5), tailor_complete=None)
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_007"]
    assert evals == []


# -------------------- RULE_011: Packing completion vs Delivery Date --------------------

def _rule011_row(delivery_deadline=None, packing_complete=None):
    return _base_row(delivery_deadline=delivery_deadline, packing_complete=packing_complete)


def test_rule011_24h_reminder():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule011_row(delivery_deadline=now + timedelta(hours=20))  # within 24h window
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_011"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "24h_pre_delivery"
    assert evals[0]["severity"] == "WARNING"


def test_rule011_4h_reminder():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule011_row(delivery_deadline=now + timedelta(hours=3))  # within 4h window
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_011"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "4h_pre_delivery"
    assert evals[0]["severity"] == "URGENT"


def test_rule011_no_alert_more_than_24h_out():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule011_row(delivery_deadline=now + timedelta(hours=30))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_011"]
    assert evals == []


def test_rule011_no_overdue_alert_past_delivery_date():
    """User explicitly asked for only 2 pre-deadline reminders here -- no
    post-deadline escalation for this rule (RULE_009 covers actual delivery
    overdue separately)."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule011_row(delivery_deadline=now - timedelta(hours=2))  # delivery date already passed
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_011"]
    assert evals == []


def test_rule011_no_alert_when_packing_already_complete():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule011_row(delivery_deadline=now + timedelta(hours=2), packing_complete=now - timedelta(minutes=5))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_011"]
    assert evals == []


# -------------------- RULE_009: Delivery Date vs Delivered to Customer --------------------

def _rule009_row(delivery_deadline=None, delivered_customer=None):
    # packing_complete defaults to "already done" so the upstream-stage guard
    # doesn't block these deadline-math tests.
    return _base_row(
        delivery_deadline=delivery_deadline, delivered_customer=delivered_customer,
        packing_complete=datetime.utcnow() - timedelta(hours=1),
    )


def test_rule009_4h_reminder():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule009_row(delivery_deadline=now + timedelta(hours=3))  # within 4h window
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_009"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "4h_pre_delivery"
    assert evals[0]["severity"] == "URGENT"


def test_rule009_no_alert_more_than_4h_out():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule009_row(delivery_deadline=now + timedelta(hours=6))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_009"]
    assert evals == []


def test_rule009_overdue_when_delivery_date_passed():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule009_row(delivery_deadline=now - timedelta(minutes=30))  # deadline passed, not delivered
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_009"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "OVERDUE"
    assert evals[0]["severity"] == "MOST_URGENT"


def test_rule009_no_alert_when_already_delivered():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule009_row(delivery_deadline=now + timedelta(hours=2), delivered_customer=now - timedelta(minutes=5))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_009"]
    assert evals == []


def test_rule009_no_alert_before_packing_complete():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _base_row(delivery_deadline=now + timedelta(hours=2), packing_complete=None)
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_009"]
    assert evals == []


def test_rule008_no_alert_before_finishing_complete():
    """RULE_008 (QC/Packing deadline) uses the older generic DEADLINE code
    path, not STAGE_DEADLINE_TIERED -- confirms the upstream guard was added
    there too, not just in the newer rule types."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _base_row(qc_deadline=now + timedelta(hours=2), finishing_complete=None)
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_008"]
    assert evals == []


# -------------------- RULE_012: Tailor completed with no Tailor Date on record --------------------

def test_rule012_fires_when_deadline_missing_but_completed():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _base_row(tailor_deadline=None, tailor_complete=now - timedelta(minutes=5))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_012"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "MISSING_DEADLINE_DATA"
    assert evals[0]["severity"] == "URGENT"


def test_rule012_no_alert_when_deadline_present():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _base_row(tailor_deadline=now - timedelta(hours=1), tailor_complete=now - timedelta(minutes=5))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_012"]
    assert evals == []


def test_rule012_no_alert_when_not_completed_either():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _base_row(tailor_deadline=None, tailor_complete=None)
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_012"]
    assert evals == []


def test_rule012_message_renders_without_crashing_on_none_deadline():
    from app.message_renderer import render_deadline_alert
    now = datetime.utcnow()
    row = _base_row(slip_no="9999", rfid="RFIDX", item_name="RMN-SUIT",
                     tailor_deadline=None, tailor_complete=now - timedelta(minutes=5))
    cfg = load_rules_config()
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_012"]
    msg = render_deadline_alert(row, evals[0])
    assert "9999" in msg
    assert "URGENT" in msg
    assert "None" not in msg  # deadline/remaining fields shouldn't leak a raw "None"


# -------------------- Condition 7: Stopped items report --------------------

def test_stopped_report_empty_list_returns_no_chunks():
    from app.message_renderer import render_stopped_items_report
    assert render_stopped_items_report([]) == []


def test_stopped_report_small_list_fits_in_one_chunk():
    from app.message_renderer import render_stopped_items_report
    items = [("12345", "TAILOR"), ("12346", "FINISHING")]
    chunks = render_stopped_items_report(items)
    assert len(chunks) == 1
    assert "Slip 12345 — TAILOR" in chunks[0]
    assert "Slip 12346 — FINISHING" in chunks[0]


def test_stopped_report_large_list_splits_under_telegram_limit():
    from app.message_renderer import render_stopped_items_report
    items = [(f"SLIP{i}", "DELIVERY") for i in range(338)]  # mirrors real workbook volume
    chunks = render_stopped_items_report(items)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 4096  # Telegram's hard message limit
    # every item must appear somewhere across the chunks, none dropped
    combined = "\n".join(chunks)
    for i in range(338):
        assert f"SLIP{i} —" in combined


def test_stopped_report_only_includes_truly_overdue_not_missing_deadline():
    """RULE_012 (MISSING_DEADLINE_DATA) has no real deadline/remaining_minutes --
    it must NOT be counted as a 'stopped/time nikal gaya' item."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _base_row(tailor_deadline=None, tailor_complete=now - timedelta(minutes=5))
    evals = evaluate_deadline_rules(row, cfg, now=now)
    time_based_overdue = [e for e in evals if e.get("remaining_minutes") is not None and e["remaining_minutes"] <= 0]
    assert time_based_overdue == []  # only RULE_012 fires here, and it must be excluded
