"""
Minimal health endpoint, run inside the same process as the Telegram bot
(see app/main.py) so a single Render Web Service can satisfy both:
- Render's port/health check (this FastAPI app, bound to $PORT)
- The actual bot (Telegram long-polling, runs in the main thread)
"""
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}
