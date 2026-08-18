import logging
from app.config import settings
from app.db import init_db
from app.telegram_bot import build_app

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fms_main")


def main():
    logger.info("Initializing database...")
    init_db()

    logger.info("Starting Telegram bot (polling)...")
    application = build_app()
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
