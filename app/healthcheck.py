"""
Minimal health endpoint, run inside the same process as the Telegram bot
(see app/main.py) so a single Render Web Service can satisfy both:
- Render's port/health check (this FastAPI app, bound to $PORT)
- The actual bot (Telegram long-polling, runs in the main thread)

Accepts GET, HEAD, POST, and OPTIONS on both "/" and "/health" -- different
uptime monitors (UptimeRobot, Better Uptime, Pulsetic, etc.) default to
different HTTP methods, and FastAPI returns 405 for anything not explicitly
listed. Being permissive here is safer than guessing which method any given
monitor uses.
"""
from fastapi import FastAPI

app = FastAPI()

_METHODS = ["GET", "HEAD", "POST", "OPTIONS"]


def _ok():
    return {"status": "ok"}


app.add_api_route("/", _ok, methods=_METHODS)
app.add_api_route("/health", _ok, methods=_METHODS)
