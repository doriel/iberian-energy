"""Placeholder web service.

Exists so the deployment target has a real URL and a health check before the
read models it will serve are in place.
"""

from fastapi import FastAPI

app = FastAPI(title="MIBEL Market Intelligence")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/")
def index():
    return {"service": "mibel-market-intelligence", "status": "placeholder"}
