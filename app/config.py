import os
import yaml
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The business (and every timestamp typed into the FMS Excel sheet) operates
# in India Standard Time (IST, UTC+5:30). The server runs in UTC. Every place
# that computes "what time is it right now" for deadline math MUST use this
# helper instead of datetime.utcnow() directly, or every SLA calculation
# ends up off by 5.5 hours -- confirmed as a real production bug 22-Aug-2026:
# alerts were silently not firing because the system thought deadlines were
# hours in the future when they had already passed in real IST time.
IST_OFFSET = timedelta(hours=5, minutes=30)


def business_now() -> datetime:
    """Naive 'now', in IST terms, matching how dates are entered in the
    workbook. Use this for ALL deadline/SLA comparisons against Excel data."""
    return datetime.utcnow() + IST_OFFSET


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


class Settings:
    TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    AUTHORIZED_CHAT_IDS: list[str] = _env_list("AUTHORIZED_CHAT_IDS")
    AUTHORIZED_USER_IDS: list[str] = _env_list("AUTHORIZED_USER_IDS")
    TEST_CHAT_ID: str = os.environ.get("TEST_CHAT_ID", "")

    DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///./fms_state.db")

    DRY_RUN: bool = os.environ.get("DRY_RUN", "false").lower() == "true"
    LANGUAGE: str = os.environ.get("LANGUAGE", "both")  # en | hi | both

    ACTIVE_SHEET: str = os.environ.get("ACTIVE_SHEET", "Sheet1")

    # Confirmed with user 20-Aug-2026: automatic background re-checking, so
    # tight-window rules (RULE_001's 5-min pre-deadline warning) don't get
    # missed just because no new Excel was uploaded in that window.
    ENABLE_TIME_BASED_MONITORING: bool = os.environ.get(
        "ENABLE_TIME_BASED_MONITORING", "true"
    ).lower() == "true"
    TIME_BASED_CHECK_INTERVAL_MINUTES: int = int(
        os.environ.get("TIME_BASED_CHECK_INTERVAL_MINUTES", "5")
    )

    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

    TEMP_DIR: str = os.environ.get("TEMP_DIR", "/tmp/fms_uploads")

    # Confirmed with user 20-Aug-2026: individual Telegram alerts (the ones
    # that flood the chat) should ONLY fire for records whose ALTERATION SLIP
    # DATE is within this many days of "now". Older records (a big historical
    # backfill upload, for example) still show up in the Stopped Items /
    # Pending Report -- they just don't generate a per-record ping.
    RECENT_ALERT_WINDOW_DAYS: int = int(os.environ.get("RECENT_ALERT_WINDOW_DAYS", "7"))

    # Confirmed with user 22-Aug-2026: Delivery-related rules (RULE_009 --
    # 4h-before-delivery reminder + overdue, RULE_011 -- 24h/4h packing lead
    # time reminders) should NOT push individual Telegram alerts anymore.
    # They're still evaluated and still show up in /pending and the Stopped
    # Items Report -- just no proactive ping. TAILOR (RULE_001, RULE_002) and
    # FINISHING (RULE_007) are untouched and keep alerting as before.
    SUPPRESSED_ALERT_RULES: list[str] = _env_list("SUPPRESSED_ALERT_RULES") or ["RULE_009", "RULE_011"]


def load_rules_config() -> dict:
    path = os.path.join(BASE_DIR, "config", "rules.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


settings = Settings()
