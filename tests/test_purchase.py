import threading
from datetime import timedelta

from app import clock, pricing


def test_same_idempotency_key_charges_once(client):
    h = {"Idempotency-Key": "abc-1"}
    a = client.post("/purchases", json={"user_id": "u1", "amount_inr": "500.00"}, headers=h).json()
    b = client.post("/purchases", json={"user_id": "u1", "amount_inr": "500.00"}, headers=h).json()
    assert a["txn_id"] == b["txn_id"] and a["duplicate"] is False and b["duplicate"] is True
    assert len(client.get("/users/u1/ledger").json()["transactions"]) == 1


def test_keyless_duplicate_within_window_is_one_charge(client):
    a = client.post("/purchases", json={"user_id": "u1", "amount_inr": 333.33}).json()
    b = client.post("/purchases", json={"user_id": "u1", "amount_inr": 333.33}).json()
    assert a["txn_id"] == b["txn_id"]
    assert a["amount_inr"] == "333.33" and a["gold_grams"] == "0.0266"
    clock.set_override(clock.now() + timedelta(seconds=120))   # outside the window: a real second purchase
    c = client.post("/purchases", json={"user_id": "u1", "amount_inr": 333.33}).json()
    assert c["txn_id"] != a["txn_id"]


def test_parallel_requests_same_key_charge_once(client):
    out = []
    def go():
        out.append(client.post("/purchases", json={"user_id": "u2", "amount_inr": "100"},
                               headers={"Idempotency-Key": "race"}).json()["txn_id"])
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(set(out)) == 1
    assert len(client.get("/users/u2/ledger").json()["transactions"]) == 1


def test_bad_amounts_rejected_and_nothing_recorded(client):
    for amt in ("-5000", "0", "0.5", "100000000000", "333.333"):
        r = client.post("/purchases", json={"user_id": "u3", "amount_inr": amt})
        assert r.status_code == 422, amt
    assert client.get("/users/u3/ledger").json()["transactions"] == []


def test_ledger_and_portfolio_are_exact(client):
    for amt in ("333.33", "333.33", "10.00"):
        clock.set_override(clock.now() + timedelta(minutes=5))
        client.post("/purchases", json={"user_id": "u4", "amount_inr": amt})
    p = client.get("/users/u4/portfolio").json()
    assert p["total_invested_inr"] == "676.66"
    assert p["total_gold_grams"] == "0.0540"   # 266 + 266 + 8 units


def test_price_outage_refuses_and_charges_nothing(client):
    # no cached price at all + source down => refuse
    pricing.MOCK.outage = True
    r = client.post("/purchases", json={"user_id": "u5", "amount_inr": "100"})
    assert r.status_code == 503 and r.json()["error"] == "price_unavailable"
    assert client.get("/users/u5/ledger").json()["transactions"] == []
    # source recovers => same user can buy
    pricing.MOCK.outage = False
    assert client.post("/purchases", json={"user_id": "u5", "amount_inr": "100"}).status_code == 200


def test_outage_uses_recent_price_but_not_old_one(client):
    client.post("/purchases", json={"user_id": "u6", "amount_inr": "100"})          # caches a fresh price
    pricing.MOCK.outage = True
    clock.set_override(clock.now() + timedelta(seconds=30))
    ok = client.post("/purchases", json={"user_id": "u6", "amount_inr": "200"})
    assert ok.status_code == 200 and ok.json()["price_stale"] is True
    clock.set_override(clock.now() + timedelta(seconds=600))
    assert client.post("/purchases", json={"user_id": "u6", "amount_inr": "300"}).status_code == 503
