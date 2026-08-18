"""
Minimal health endpoint. Deploy this as a Render Web Service if you want an
HTTP health check separate from the polling worker (which has no HTTP port).
Run: uvicorn app.healthcheck:app --host 0.0.0.0 --port $PORT
"""
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}
