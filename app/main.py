import os
import sys
import time
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
    # Start the health server FIRST, before any DB work. Render's deploy
    # health check starts probing $PORT almost immediately -- if the port
    # isn't listening yet because we're still doing DB schema
    # inspection/migration (see app/db.py's _migrate_uploads_table, which
    # talks to Postgres over the network), Render can decide the deploy is
    # unhealthy and kill+restart it before the bot ever gets a chance to run.
    health_thread = threading.Thread(target=_run_health_server, daemon=True)
    health_thread.start()
    time.sleep(0.5)  # small grace period for uvicorn to actually bind the port

    logger.info("Initializing database...")
    init_db()

    logger.info("Starting Telegram bot (polling)...")
    application = build_app()
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Ensure a crash is ALWAYS visible in the logs with a full traceback --
        # previously the process could exit silently with no error output at
        # all, making it impossible to diagnose from Render's logs.
        logger.exception("Fatal error -- process is exiting")
        sys.exit(1)
