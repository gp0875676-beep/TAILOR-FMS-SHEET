"""
Formal mapping: original Excel headers <-> normalized internal field names.

Do NOT rename source columns in the workbook itself. This mapping is the
single source of truth used by excel_parser/normalizer so that if the
workbook's header text ever changes slightly, only this file needs updating.
"""

# normalized_name: original_excel_header
COLUMN_MAP = {
    "item_name": "Item name",
    "slip_no": "alteration slip NO",
    "barcode": "BARCODE",
    "rfid": "RFID",
    "order_ocs": "ORDER OCS",
    "slip_date": "ALTERATION SLIP DATE",
    "slip_type": "SLIP TYPE",
    "sent_to_agency": "SEND TO AGENCY",
    "received_by_tailor": "RECEIVED BY TAILOR",
    "tailor_name": "TAILOR NAME",
    "agency_hrs": "SEND TO AGENCY HRS",          # stage DURATION, not deadline variance
    "tailor_deadline": "TAILOR DATE",
    "tailor_complete": "TAILOR COMPLETE",
    "tailor_hrs": "TAILOR HRS",                   # deadline variance (complete - deadline)
    "finishing_deadline": "FINISHING DATE",
    "finish_name": "FINISH NAME ",                # NOTE: source header has a trailing space
    "finishing_complete": "FINISHING COMPLETE",
    "finishing_hrs": "FINISHING HRS",             # deadline variance
    "qc_deadline": "QC DEADLINE",                 # doubles as the packing-stage deadline
    "packing_complete": "PACKING COMPLETE",
    "packing_hrs": "PACKING HRS",                 # variance vs QC DEADLINE, not a separate packing date
    "delivery_deadline": "DELIVERY DATE",
    "delivered_customer": "DELIVERED CUSTOMER",
    "delivery_hrs": "DELIVERY HOURS",             # deadline variance
}

# Columns that must exist for the workbook to be considered a valid FMS file.
REQUIRED_COLUMNS = [
    "Item name",
    "alteration slip NO",
    "RFID",
    "SLIP TYPE",
    "TAILOR DATE",
    "TAILOR COMPLETE",
    "FINISHING DATE",
    "FINISHING COMPLETE",
    "QC DEADLINE",
    "PACKING COMPLETE",
    "DELIVERY DATE",
    "DELIVERED CUSTOMER",
]

REVERSE_MAP = {v.strip(): k for k, v in COLUMN_MAP.items()}

# Data dictionary (Step 50) -- used for reference / docs generation, not enforced at runtime.
DATA_DICTIONARY = [
    # (original, normalized, dtype, meaning, required, used_by)
    ("Item name", "item_name", "str", "Garment/item description", True, "display"),
    ("alteration slip NO", "slip_no", "int", "Customer slip number; one slip can cover multiple items", True, "identity, display"),
    ("BARCODE", "barcode", "str", "Per-item barcode; unreliable for generic item types (placeholder text)", False, "display, DQ"),
    ("RFID", "rfid", "int", "Per-item RFID tag; primary identity field (98.5% unique)", True, "identity, dedup"),
    ("ORDER OCS", "order_ocs", "float", "Optional order-system reference; mostly null (1407/1505)", False, "display"),
    ("ALTERATION SLIP DATE", "slip_date", "datetime", "Date the alteration slip was raised", True, "SLA baseline"),
    ("SLIP TYPE", "slip_type", "str", "Normal | Urgent", True, "escalation logic"),
    ("SEND TO AGENCY", "sent_to_agency", "datetime", "Timestamp sent to outside agency (optional stage)", False, "AGENCY rules"),
    ("RECEIVED BY TAILOR", "received_by_tailor", "datetime", "Timestamp tailor received item", False, "TAILOR pending rule"),
    ("TAILOR NAME", "tailor_name", "str", "Responsible tailor", False, "message rendering"),
    ("SEND TO AGENCY HRS", "agency_hrs", "float", "= received_by_tailor - sent_to_agency (duration, not variance)", False, "DQ only"),
    ("TAILOR DATE", "tailor_deadline", "datetime", "Tailor stage deadline", True, "deadline engine"),
    ("TAILOR COMPLETE", "tailor_complete", "datetime", "Tailor stage completion timestamp", True, "state machine"),
    ("TAILOR HRS", "tailor_hrs", "float", "= tailor_complete - tailor_deadline; neg=early, pos=late", False, "SLA reporting"),
    ("FINISHING DATE", "finishing_deadline", "datetime", "Finishing stage deadline", True, "deadline engine"),
    ("FINISH NAME ", "finish_name", "str", "Responsible finisher", False, "message rendering"),
    ("FINISHING COMPLETE", "finishing_complete", "datetime", "Finishing stage completion timestamp", True, "state machine"),
    ("FINISHING HRS", "finishing_hrs", "float", "= finishing_complete - finishing_deadline", False, "SLA reporting"),
    ("QC DEADLINE", "qc_deadline", "datetime", "QC deadline; also the effective packing deadline", True, "deadline engine"),
    ("PACKING COMPLETE", "packing_complete", "datetime", "Packing completion timestamp", True, "state machine"),
    ("PACKING HRS", "packing_hrs", "float", "= packing_complete - qc_deadline", False, "SLA reporting"),
    ("DELIVERY DATE", "delivery_deadline", "datetime", "Delivery deadline", True, "deadline engine"),
    ("DELIVERED CUSTOMER", "delivered_customer", "datetime", "Delivery completion timestamp", True, "state machine"),
    ("DELIVERY HOURS", "delivery_hrs", "float", "= delivered_customer - delivery_deadline", False, "SLA reporting"),
]
