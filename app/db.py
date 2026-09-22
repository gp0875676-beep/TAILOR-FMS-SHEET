from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from app.models import Base
from app.config import settings

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(settings.DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Columns added after a table already existed in production. create_all()
# only creates missing TABLES, not missing COLUMNS on existing ones -- so an
# existing deployment needs these added by hand, once, without losing the
# data already in the table.
_NEW_COLUMNS = {
    "uploads": {
        "duplicates_suppressed": "INTEGER DEFAULT 0",
        "anomaly_count": "INTEGER DEFAULT 0",
        "stopped_items_count": "INTEGER DEFAULT 0",
    },
    "record_snapshots": {
        "order_ocs": "VARCHAR",
    },
}


def _migrate_tables():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table_name, new_columns in _NEW_COLUMNS.items():
        if table_name not in existing_tables:
            continue  # brand new DB -- create_all() already made it with all columns

        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        missing = {name: ddl for name, ddl in new_columns.items() if name not in existing_columns}
        if not missing:
            continue

        with engine.begin() as conn:
            for name, ddl in missing.items():
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}"))


def init_db():
    Base.metadata.create_all(engine)  # creates any missing tables (fresh DB, or new tables like anomalies)
    _migrate_tables()                 # adds any missing columns to tables that already existed


def get_session():
    return SessionLocal()
