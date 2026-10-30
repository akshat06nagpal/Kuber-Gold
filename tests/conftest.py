import os
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import clock, db, logs, pricing

NOW = datetime(2026, 10, 29, 10, 0, tzinfo=clock.IST)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("LOG_PATH", str(tmp_path / "logs" / "app.jsonl"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("PRICE_MODE", "mock")
    monkeypatch.setenv("MOCK_PRICE_INR_PER_GRAM", "12500.00")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    pricing.MOCK.outage = False
    clock.set_override(NOW)
    logs.setup_logging()
    db.init_db()
    yield
    clock.set_override(None)
    pricing.MOCK.outage = False


@pytest.fixture
def client():
    with TestClient(__import__("app.main", fromlist=["app"]).app) as c:
        yield c


def say(client, user, msg, key=None):
    h = {"Idempotency-Key": key} if key else {}
    r = client.post("/chat", json={"user_id": user, "message": msg}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()
