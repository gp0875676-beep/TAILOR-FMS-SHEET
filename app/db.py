from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from app.models import Base
from app.config import settings

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Columns added after the "uploads" table already existed in production.
# create_all() only creates missing TABLES, not missing COLUMNS on existing
# ones -- so an existing deployment needs these added by hand, once, without
# losing the upload history already in the table.
_NEW_UPLOAD_COLUMNS = {
    "duplicates_suppressed": "INTEGER DEFAULT 0",
    "anomaly_count": "INTEGER DEFAULT 0",
    "stopped_items_count": "INTEGER DEFAULT 0",
}


def _migrate_uploads_table():
    inspector = inspect(engine)
    if "uploads" not in inspector.get_table_names():
        return  # brand new DB -- create_all() already made the table with all columns

    existing_columns = {col["name"] for col in inspector.get_columns("uploads")}
    missing = {name: ddl for name, ddl in _NEW_UPLOAD_COLUMNS.items() if name not in existing_columns}
    if not missing:
        return

    with engine.begin() as conn:
        for name, ddl in missing.items():
            conn.execute(text(f"ALTER TABLE uploads ADD COLUMN {name} {ddl}"))


def init_db():
    Base.metadata.create_all(engine)  # creates any missing tables (fresh DB, or new tables like anomalies)
    _migrate_uploads_table()          # adds any missing columns to a table that already existed


def get_session():
    return SessionLocal()
