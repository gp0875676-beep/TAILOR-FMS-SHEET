import os
import hashlib
import pandas as pd
from dataclasses import dataclass, field
from app.mapping import REQUIRED_COLUMNS
from app.config import settings


@dataclass
class ValidationResult:
    ok: bool
    status: str  # PASSED | FAILED | PARTIAL
    reason: str = ""
    missing_columns: list = field(default_factory=list)
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    invalid_row_indices: list = field(default_factory=list)
    row_count: int = 0
    column_count: int = 0


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_extension(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in (".xlsx", ".xlsm")


def validate_workbook(path: str) -> tuple[ValidationResult, pd.DataFrame | None]:
    try:
        xls = pd.ExcelFile(path)
    except Exception as e:
        return ValidationResult(ok=False, status="FAILED", reason=f"Unreadable/corrupt workbook: {e}"), None

    sheet = settings.ACTIVE_SHEET
    if sheet not in xls.sheet_names:
        return ValidationResult(
            ok=False, status="FAILED",
            reason=f"Required sheet '{sheet}' not found. Sheets present: {xls.sheet_names}"
        ), None

    try:
        df = xls.parse(sheet)
    except Exception as e:
        return ValidationResult(ok=False, status="FAILED", reason=f"Failed to parse sheet: {e}"), None

    if df.empty:
        return ValidationResult(ok=False, status="FAILED", reason="Workbook/sheet is empty"), None

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return ValidationResult(
            ok=False, status="FAILED",
            reason=f"Missing required column(s): {', '.join(missing)}",
            missing_columns=missing,
        ), None

    total = len(df)

    # Row-level validity: must have a non-null identity (slip_no + rfid) and a
    # parseable slip_date. Previously this only checked for a non-null cell --
    # a text value like "ABC123" in the date column would pass validation
    # silently, then get skipped later by rule_engine.py's own defensive
    # parsing, invisibly excluding the row from alerting without ever being
    # flagged as an invalid row in the validation summary. Now actually try
    # to parse it.
    invalid_idx = []
    for idx, row in df.iterrows():
        bad = False
        if pd.isna(row.get("alteration slip NO")) or pd.isna(row.get("RFID")):
            bad = True

        slip_date = row.get("ALTERATION SLIP DATE")
        if pd.isna(slip_date):
            bad = True
        elif not isinstance(slip_date, (pd.Timestamp,)):
            # not already a proper datetime (e.g. a stray text value) -- confirm it's parseable
            try:
                pd.to_datetime(slip_date)
            except Exception:
                bad = True

        if bad:
            invalid_idx.append(idx)

    valid_rows = total - len(invalid_idx)
    status = "PASSED" if not invalid_idx else "PARTIAL"

    return ValidationResult(
        ok=True,
        status=status,
        total_rows=total,
        valid_rows=valid_rows,
        invalid_rows=len(invalid_idx),
        invalid_row_indices=invalid_idx,
        row_count=total,
        column_count=len(df.columns),
    ), df
