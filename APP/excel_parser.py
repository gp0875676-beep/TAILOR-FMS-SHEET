import pandas as pd
from app.mapping import COLUMN_MAP


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Rename columns to normalized internal names. Original workbook is never touched --
    this operates on an in-memory copy only."""
    rename = {}
    for norm, orig in COLUMN_MAP.items():
        # tolerate trailing/leading whitespace differences in headers
        for col in df.columns:
            if col.strip() == orig.strip():
                rename[col] = norm
                break
    out = df.rename(columns=rename).copy()

    # ensure every expected normalized column exists even if source was missing it
    for norm in COLUMN_MAP.keys():
        if norm not in out.columns:
            out[norm] = pd.NA

    # normalize identity columns to string for stable hashing/keys
    out["rfid"] = out["rfid"].astype("string")
    out["slip_no"] = out["slip_no"].astype("string")
    out["slip_type"] = out["slip_type"].astype("string").str.strip()

    return out


def record_id(row: pd.Series) -> str:
    """Primary identity = RFID. Falls back to slip_no+rfid composite (rarely needed)."""
    rfid = row.get("rfid")
    slip_no = row.get("slip_no")
    if pd.notna(rfid):
        return f"RFID:{rfid}"
    return f"COMPOSITE:{slip_no}:{rfid}"


def determine_stage(row: pd.Series) -> tuple[str, str]:
    """Returns (stage, status). stage is the current pending stage or COMPLETED."""
    if pd.notna(row.get("delivered_customer")):
        return "COMPLETED", "COMPLETED"
    if pd.notna(row.get("packing_complete")):
        return "DELIVERY", "PENDING"
    if pd.notna(row.get("finishing_complete")):
        return "PACKING", "PENDING"
    if pd.notna(row.get("tailor_complete")):
        return "FINISHING", "PENDING"
    if pd.notna(row.get("received_by_tailor")):
        return "TAILOR", "PENDING"
    if pd.notna(row.get("sent_to_agency")):
        return "AGENCY", "PENDING"
    return "NOT_STARTED", "PENDING"
