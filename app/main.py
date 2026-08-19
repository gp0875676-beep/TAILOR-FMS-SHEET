import os
import logging
import threading

import uvicorn

from app.config import settings
from app.db import init_db
from app.telegram_bot import build_app
from app.healthcheck import app as health_app

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("fms_main")


def _run_health_server():
    """Render's free tier only offers free Web Services (not Background
    Workers), which means this process MUST bind to $PORT and answer HTTP
    requests or Render's health check will keep restarting it. This runs a
    tiny FastAPI health endpoint in a background thread so the main thread
    is free to run the Telegram bot's polling loop."""
    port = int(os.environ.get("PORT", "10000"))
    logger.info(f"Starting health check server on port {port}...")
    uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="warning")


def main():
    logger.info("Initializing database...")
    init_db()

    health_thread = threading.Thread(target=_run_health_server, daemon=True)
    health_thread.start()

    logger.info("Starting Telegram bot (polling)...")
    application = build_app()
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
