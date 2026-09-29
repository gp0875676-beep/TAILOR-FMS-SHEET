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


# -------------------- RULE_001: Piece not scanned by Tailor within 3 days (revised 24-Aug-2026) --------------------

def _rule001_row(item_name, sent_to_agency=None, slip_date=None, received_by_tailor=None,
                  tailor_deadline="__default__"):
    # tailor_deadline defaults to "present" (non-null) so existing tests exercise
    # RULE_001's normal path; pass tailor_deadline=None explicitly to test the
    # new "piece has no tailor stage at all" skip behavior.
    if tailor_deadline == "__default__":
        tailor_deadline = datetime.utcnow() + timedelta(days=30)
    return _base_row(
        item_name=item_name,
        sent_to_agency=sent_to_agency,
        slip_date=slip_date,
        received_by_tailor=received_by_tailor,
        tailor_deadline=tailor_deadline,
    )


def test_rule001_fires_overdue_after_3_days():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule001_row("RMN-DRESS", slip_date=now - timedelta(days=3, minutes=5))
    evals = evaluate_deadline_rules(row, cfg, now=now)
    r1 = [e for e in evals if e["rule_id"] == "RULE_001"]
    assert len(r1) == 1
    assert r1[0]["alert_stage"] == "OVERDUE"
    assert r1[0]["stage"] == "AGENCY"  # relabeled from TAILOR so it's distinct from RULE_002's stage


def test_rule001_excludes_plain_saree():
    cfg = load_rules_config()
    now = datetime.utcnow()
    for excluded in ("SAREE", "RMN-D.SAREE", "SAREE(NO LESS)"):
        row = _rule001_row(excluded, slip_date=now - timedelta(days=4))
        evals = evaluate_deadline_rules(row, cfg, now=now)
        assert all(e["rule_id"] != "RULE_001" for e in evals), f"{excluded} should be excluded"


def test_rule001_does_not_exclude_saree_stitch_or_blouse():
    cfg = load_rules_config()
    now = datetime.utcnow()
    for included in ("SAREE STITCH", "SAREE BLOUSE SET"):
        row = _rule001_row(included, slip_date=now - timedelta(days=4))
        evals = evaluate_deadline_rules(row, cfg, now=now)
        assert any(e["rule_id"] == "RULE_001" for e in evals), f"{included} should NOT be excluded"


def test_rule001_skips_pieces_with_no_tailor_stage():
    """Confirmed with user 24-Aug-2026: TAILOR DATE being empty means this
    piece has no tailor stage in its workflow at all -- RULE_001 must not
    flag it as 'not scanned', since scanning was never expected."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule001_row("RMN-DRESS", slip_date=now - timedelta(days=10), tailor_deadline=None)
    evals = evaluate_deadline_rules(row, cfg, now=now)
    assert all(e["rule_id"] != "RULE_001" for e in evals)


def test_rule001_no_alert_before_3_days():
    """Confirms this is now a 3-day report-only threshold, not the old 1-hour
    push-alert window -- under 3 days, nothing fires yet."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule001_row("RMN-SUIT", sent_to_agency=now - timedelta(hours=5))
    evals = evaluate_deadline_rules(row, cfg, now=now)
    assert all(e["rule_id"] != "RULE_001" for e in evals)


def test_rule001_uses_agency_time_over_slip_date_when_both_present():
    cfg = load_rules_config()
    now = datetime.utcnow()
    # slip cut 5 days ago (would be overdue on slip_date alone), but sent to
    # agency only 1 day ago -> reference time should be sent_to_agency, so
    # this should NOT be overdue yet (under the 3-day window from agency time)
    row = _rule001_row(
        "RMN-SUIT",
        slip_date=now - timedelta(days=5),
        sent_to_agency=now - timedelta(days=1),
    )
    evals = evaluate_deadline_rules(row, cfg, now=now)
    assert all(e["rule_id"] != "RULE_001" for e in evals)


def test_rule001_no_alert_when_already_received():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule001_row("RMN-SUIT", slip_date=now - timedelta(days=4),
                        received_by_tailor=now - timedelta(minutes=1))
    evals = evaluate_deadline_rules(row, cfg, now=now)
    assert all(e["rule_id"] != "RULE_001" for e in evals)


def test_rule001_no_alert_when_far_from_deadline():
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _rule001_row("RMN-SUIT", slip_date=now - timedelta(minutes=20))
    evals = evaluate_deadline_rules(row, cfg, now=now)
    assert all(e["rule_id"] != "RULE_001" for e in evals)


def test_rule001_is_in_suppressed_alert_rules_by_default():
    """Confirms Condition 1's Alert #1/#2 were deleted -- RULE_001 no longer
    pushes individual Telegram messages, only contributes to the report."""
    from app.config import settings
    assert "RULE_001" in settings.SUPPRESSED_ALERT_RULES


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
    still empty. This guard must suppress RULE_007 for all of them --
    specifically when the piece IS actively at tailor (tailor_deadline is
    set, i.e. it has a tailor stage, just hasn't finished it yet)."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _base_row(finishing_deadline=now + timedelta(minutes=5), tailor_complete=None,
                     tailor_deadline=now + timedelta(hours=2))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_007"]
    assert evals == []


def test_rule007_fires_when_piece_has_no_tailor_stage_at_all():
    """Confirmed with user 24-Aug-2026: pieces with NO tailor_deadline at all
    (no TAILOR DATE entered) skip the tailor stage entirely -- RULE_007
    should still track their FINISHING deadline without waiting on
    tailor_complete, since that will never be filled in for these."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    row = _base_row(finishing_deadline=now + timedelta(minutes=5), tailor_complete=None,
                     tailor_deadline=None)
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_007"]
    assert len(evals) == 1
    assert evals[0]["alert_stage"] == "15m_pre_deadline"


# -------------------- RULE_011: DISABLED (25-Aug-2026, replaced by /packing command) --------------------

def _rule011_row(delivery_deadline=None, packing_complete=None):
    return _base_row(delivery_deadline=delivery_deadline, packing_complete=packing_complete)


def test_rule011_disabled_no_alerts_fire_at_all():
    """Confirmed with user 25-Aug-2026: RULE_011's lead-time reminders were
    completely replaced by the /packing command (a plain stage listing, no
    deadline math). RULE_011 stays in rules.yaml for history but is disabled
    -- it must never produce an evaluation, at any timing."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    for hours_out in (30, 20, 3, -2):
        row = _rule011_row(delivery_deadline=now + timedelta(hours=hours_out))
        evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_011"]
        assert evals == [], f"RULE_011 should never fire (tested at {hours_out}h), got {evals}"


# -------------------- RULE_009: DISABLED (25-Aug-2026, replaced by /order command) --------------------

def _rule009_row(delivery_deadline=None, delivered_customer=None):
    # packing_complete defaults to "already done" so this row would have
    # tripped the old deadline math if the rule were still enabled.
    return _base_row(
        delivery_deadline=delivery_deadline, delivered_customer=delivered_customer,
        packing_complete=datetime.utcnow() - timedelta(hours=1),
    )


def test_rule009_disabled_no_alerts_fire_at_all():
    """Confirmed with user 25-Aug-2026 (Condition 5): RULE_009's delivery
    reminder/overdue alerts were completely replaced by the /order command
    (a plain pending-order-pieces listing, no deadline math). RULE_009 stays
    in rules.yaml for history but is disabled -- it must never produce an
    evaluation, at any timing, delivered or not."""
    cfg = load_rules_config()
    now = datetime.utcnow()
    for hours_out in (3, 6, -0.5, 2):
        row = _rule009_row(delivery_deadline=now + timedelta(hours=hours_out))
        evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_009"]
        assert evals == [], f"RULE_009 should never fire (tested at {hours_out}h), got {evals}"

    # Even with delivered_customer already set, or packing_complete still null.
    row = _rule009_row(delivery_deadline=now + timedelta(hours=2), delivered_customer=now - timedelta(minutes=5))
    evals = [e for e in evaluate_deadline_rules(row, cfg, now=now) if e["rule_id"] == "RULE_009"]
    assert evals == []

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
