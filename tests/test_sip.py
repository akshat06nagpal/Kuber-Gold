from datetime import date, timedelta

from app import clock, pricing
from app.sips import due_date


def test_monthly_31st_schedule_clamps_without_drifting():
    a = date(2026, 10, 31)
    got = [due_date(a, 31, "monthly", k) for k in range(6)]
    assert got == [date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31),
                   date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31)]
    assert due_date(date(2027, 12, 31), 31, "monthly", 2) == date(2028, 2, 29)   # leap year


def _create(client, user, **kw):
    body = {"user_id": user, "amount_inr": "100.00", "frequency": "daily", **kw}
    return client.post("/sips", json=body).json()


def test_three_daily_instalments_land_in_ledger(client):
    s = _create(client, "s1")
    assert s["next_due_date"] == "2026-10-29"
    for d in ("2026-10-29", "2026-10-30", "2026-10-31"):
        client.post("/admin/run-due-sips", json={"as_of": d})
    tx = client.get("/users/s1/ledger").json()["transactions"]
    assert [t["sip_idx"] for t in tx] == [0, 1, 2] and all(t["kind"] == "sip" for t in tx)


def test_running_scheduler_twice_never_double_charges(client):
    _create(client, "s2")
    for _ in range(3):
        client.post("/admin/run-due-sips", json={"as_of": "2026-10-29"})
    assert len(client.get("/users/s2/ledger").json()["transactions"]) == 1


def test_duplicate_sip_creation_returns_existing(client):
    a, b = _create(client, "s3"), _create(client, "s3")
    assert a["sip_id"] == b["sip_id"] and b["duplicate"] is True
    assert len(client.get("/users/s3/sips").json()["sips"]) == 1


def test_weekly_starting_yesterday_is_not_backfilled(client):
    s = _create(client, "s4", frequency="weekly", amount_inr="1000", start_date="2026-10-28")
    assert s["next_due_date"] == "2026-11-04" and "not back-filled" in s["note"]
    client.post("/admin/run-due-sips", json={"as_of": "2026-10-29"})
    assert client.get("/users/s4/ledger").json()["transactions"] == []


def test_pause_resume_cancel(client):
    s = _create(client, "s5")
    sid = s["sip_id"]
    assert client.post(f"/sips/{sid}/pause", json={"user_id": "s5"}).json()["status"] == "PAUSED"
    client.post("/admin/run-due-sips", json={"as_of": "2026-11-05"})
    assert client.get("/users/s5/ledger").json()["transactions"] == []        # paused => nothing charged
    r = client.post(f"/sips/{sid}/resume", json={"user_id": "s5"}).json()
    assert r["status"] == "ACTIVE" and r["next_due_date"] == "2026-10-29"       # no back-fill of missed days
    assert client.post(f"/sips/{sid}/cancel", json={"user_id": "s5"}).json()["status"] == "CANCELLED"
    client.post("/admin/run-due-sips", json={"as_of": "2026-12-01"})
    assert client.get("/users/s5/ledger").json()["transactions"] == []
    assert client.post(f"/sips/{sid}/resume", json={"user_id": "s5"}).status_code == 409
    assert client.post(f"/sips/{sid}/pause", json={"user_id": "someone-else"}).status_code == 404


def test_price_outage_defers_instalment_then_catches_up_once(client):
    _create(client, "s6")
    pricing.MOCK.outage = True
    r = client.post("/admin/run-due-sips", json={"as_of": "2026-10-29"}).json()["processed"]
    assert r[0]["status"] == "deferred"
    assert client.get("/users/s6/ledger").json()["transactions"] == []
    pricing.MOCK.outage = False
    client.post("/admin/run-due-sips", json={"as_of": "2026-10-29"})
    client.post("/admin/run-due-sips", json={"as_of": "2026-10-29"})
    assert len(client.get("/users/s6/ledger").json()["transactions"]) == 1
