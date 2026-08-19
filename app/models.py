from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean, Text, UniqueConstraint
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Upload(Base):
    __tablename__ = "uploads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id = Column(String)
    telegram_chat_id = Column(String)
    filename = Column(String)
    upload_timestamp = Column(DateTime)
    file_hash = Column(String, index=True)
    row_count = Column(Integer)
    column_count = Column(Integer)
    validation_status = Column(String)   # PASSED | FAILED | PARTIAL
    processing_status = Column(String)   # SUCCESS | FAILED
    processing_duration_ms = Column(Integer)
    new_records = Column(Integer, default=0)
    updated_records = Column(Integer, default=0)
    completed_records = Column(Integer, default=0)
    new_alerts = Column(Integer, default=0)
    duplicates_suppressed = Column(Integer, default=0)
    anomaly_count = Column(Integer, default=0)
    stopped_items_count = Column(Integer, default=0)
    errors = Column(Text)


class RecordSnapshot(Base):
    """Latest known state of every record (identified by rfid, fallback slip_no+rfid)."""
    __tablename__ = "record_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_id = Column(String, unique=True, index=True)  # rfid (or composite)
    slip_no = Column(String, index=True)
    rfid = Column(String)
    item_name = Column(String)
    slip_type = Column(String)
    stage = Column(String)          # current pending stage: AGENCY/TAILOR/FINISHING/PACKING/DELIVERY/COMPLETED
    status = Column(String)         # PENDING/COMPLETED
    last_upload_id = Column(Integer)
    first_seen_at = Column(DateTime)
    last_seen_at = Column(DateTime)
    is_removed = Column(Boolean, default=False)  # missing from most recent upload
    raw_json = Column(Text)         # full normalized row, for diffing meaningful fields


class AlertHistory(Base):
    __tablename__ = "alert_history"
    __table_args__ = (
        UniqueConstraint("record_id", "rule_id", "alert_stage", name="uq_alert_fingerprint"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_id = Column(String, index=True)
    rule_id = Column(String, index=True)
    alert_stage = Column(String)       # e.g. "24h", "1h", "OVERDUE"
    severity = Column(String)
    first_triggered_at = Column(DateTime)
    last_triggered_at = Column(DateTime)
    last_seen_at = Column(DateTime)
    alert_count = Column(Integer, default=1)
    status = Column(String, default="ACTIVE")  # ACTIVE | RESOLVED
    message_hash = Column(String)


class Anomaly(Base):
    __tablename__ = "anomalies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_id = Column(String, index=True)
    dq_rule_id = Column(String)
    description = Column(Text)
    detected_at = Column(DateTime)
    upload_id = Column(Integer)
    status = Column(String, default="OPEN")  # OPEN | REVIEWED
