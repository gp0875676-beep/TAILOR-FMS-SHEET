import os
import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def load_rules_config() -> dict:
    path = os.path.join(BASE_DIR, "config", "rules.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


settings = Settings()
