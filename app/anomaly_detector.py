import pandas as pd


def detect_anomalies(df: pd.DataFrame) -> list[dict]:
    """Returns a list of dicts: {record_id, dq_rule_id, description}.
    Source data is never modified -- this only reports."""
    anomalies = []

    # DQ_001: completion out of order (finishing before tailor, packing before finishing, etc.)
    order_checks = [
        ("finishing_complete", "tailor_complete", "Finishing completed before Tailor completed"),
        ("packing_complete", "finishing_complete", "Packing completed before Finishing completed"),
        ("delivered_customer", "packing_complete", "Delivered before Packing completed"),  # also DQ_005
    ]
    for later_field, earlier_field, desc in order_checks:
        sub = df.dropna(subset=[later_field, earlier_field])
        bad = sub[sub[later_field] < sub[earlier_field]]
        for _, row in bad.iterrows():
            anomalies.append({
                "record_id": f"RFID:{row.get('rfid')}",
                "dq_rule_id": "DQ_001" if "Delivered" not in desc else "DQ_005",
                "description": f"{desc} (slip {row.get('slip_no')})",
            })

    # DQ_002: completed stage but missing responsible person
    tailor_missing = df[df["tailor_complete"].notna() & df["tailor_name"].isna()]
    for _, row in tailor_missing.iterrows():
        anomalies.append({
            "record_id": f"RFID:{row.get('rfid')}",
            "dq_rule_id": "DQ_002",
            "description": f"Tailor Complete set but Tailor Name missing (slip {row.get('slip_no')})",
        })

    finish_missing = df[df["finishing_complete"].notna() & df["finish_name"].isna()]
    for _, row in finish_missing.iterrows():
        anomalies.append({
            "record_id": f"RFID:{row.get('rfid')}",
            "dq_rule_id": "DQ_002",
            "description": f"Finishing Complete set but Finish Name missing (slip {row.get('slip_no')})",
        })

    # DQ_003: duplicate RFID
    dup_rfid = df[df["rfid"].notna() & df["rfid"].duplicated(keep=False)]
    for rfid, group in dup_rfid.groupby("rfid"):
        slip_nos = ", ".join(group["slip_no"].astype(str).unique())
        anomalies.append({
            "record_id": f"RFID:{rfid}",
            "dq_rule_id": "DQ_003",
            "description": f"RFID {rfid} appears {len(group)}x (slips: {slip_nos})",
        })

    # DQ_004: duplicate slip_no+rfid composite (should be ~0)
    comp_dup = df[df.duplicated(subset=["slip_no", "rfid"], keep=False) & df["rfid"].notna()]
    if not comp_dup.empty:
        for (slip_no, rfid), group in comp_dup.groupby(["slip_no", "rfid"]):
            anomalies.append({
                "record_id": f"RFID:{rfid}",
                "dq_rule_id": "DQ_004",
                "description": f"Composite slip_no+RFID collision: slip {slip_no}, RFID {rfid} ({len(group)} rows)",
            })

    # DQ_006: stage started but downstream deadline missing
    deadline_checks = [
        ("received_by_tailor", "tailor_deadline", "Received by Tailor but Tailor Date (deadline) missing"),
        ("tailor_complete", "finishing_deadline", "Tailor Complete but Finishing Date (deadline) missing"),
        ("finishing_complete", "qc_deadline", "Finishing Complete but QC Deadline missing"),
        ("packing_complete", "delivery_deadline", "Packing Complete but Delivery Date (deadline) missing"),
    ]
    for started_field, deadline_field, desc in deadline_checks:
        sub = df[df[started_field].notna() & df[deadline_field].isna()]
        for _, row in sub.iterrows():
            anomalies.append({
                "record_id": f"RFID:{row.get('rfid')}",
                "dq_rule_id": "DQ_006",
                "description": f"{desc} (slip {row.get('slip_no')})",
            })

    return anomalies
