import json
import pandas as pd
from datetime import datetime
from dataclasses import dataclass, field
from app.models import RecordSnapshot
from app.excel_parser import record_id, determine_stage

# fields whose change counts as a "meaningful update" (ignore noisy/derived HRS columns)
MEANINGFUL_FIELDS = [
    "sent_to_agency", "received_by_tailor", "tailor_name", "tailor_complete",
    "finishing_complete", "finish_name", "packing_complete",
    "delivered_customer", "slip_type",
]


@dataclass
class DiffResult:
    new: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    updated: list = field(default_factory=list)
    completed: list = field(default_factory=list)   # newly completed this upload
    removed_ids: list = field(default_factory=list)  # present before, missing now


def _row_to_dict(row: pd.Series) -> dict:
    d = row.to_dict()
    for k, v in d.items():
        if pd.isna(v):
            d[k] = None
        elif isinstance(v, (pd.Timestamp,)):
            d[k] = v.isoformat()
    return d


def diff_snapshot(session, df: pd.DataFrame, upload_id: int) -> DiffResult:
    result = DiffResult()
    now = datetime.utcnow()
    seen_ids = set()

    for _, row in df.iterrows():
        rid = record_id(row)
        stage, status = determine_stage(row)
        new_data = _row_to_dict(row)

        if rid in seen_ids:
            # Duplicate record_id within the same upload (e.g. same RFID logged twice --
            # a DQ_003 data-quality issue, not a modeling bug). Keep the first occurrence's
            # snapshot and skip re-processing this row as a distinct record to avoid a
            # unique-constraint crash; anomaly_detector.py separately reports DQ_003/DQ_004.
            continue
        seen_ids.add(rid)

        existing = session.query(RecordSnapshot).filter_by(record_id=rid).one_or_none()

        if existing is None:
            snap = RecordSnapshot(
                record_id=rid,
                slip_no=str(row.get("slip_no")),
                rfid=str(row.get("rfid")),
                item_name=row.get("item_name"),
                slip_type=row.get("slip_type"),
                stage=stage,
                status=status,
                last_upload_id=upload_id,
                first_seen_at=now,
                last_seen_at=now,
                is_removed=False,
                raw_json=json.dumps(new_data, default=str),
            )
            session.add(snap)
            result.new.append((rid, row, stage, status))
            continue

        old_data = json.loads(existing.raw_json) if existing.raw_json else {}
        was_completed = existing.status == "COMPLETED"
        changed_fields = [f for f in MEANINGFUL_FIELDS if old_data.get(f) != new_data.get(f)]

        existing.last_seen_at = now
        existing.last_upload_id = upload_id
        existing.is_removed = False
        existing.stage = stage
        existing.status = status
        existing.raw_json = json.dumps(new_data, default=str)

        if status == "COMPLETED" and not was_completed:
            result.completed.append((rid, row, stage, status))
        elif changed_fields:
            result.updated.append((rid, row, stage, status, changed_fields))
        else:
            result.unchanged.append((rid, row, stage, status))

    # anything previously seen but not in this upload -> removed (not deleted, just flagged)
    all_active = session.query(RecordSnapshot).filter_by(is_removed=False).all()
    for snap in all_active:
        if snap.record_id not in seen_ids:
            snap.is_removed = True
            result.removed_ids.append(snap.record_id)

    session.commit()
    return result
