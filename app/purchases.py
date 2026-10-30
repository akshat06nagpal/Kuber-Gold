import uuid
from datetime import timedelta

from . import clock, db, pricing
from .config import get_settings
from .logs import log_event, request_id_var
from .money import check_amount, gold_units, grams_str, inr_str


def new_txn_id() -> str:
    return "txn_" + uuid.uuid4().hex[:12]


def receipt(row, duplicate: bool) -> dict:
    return {
        "txn_id": row["txn_id"], "user_id": row["user_id"], "kind": row["kind"],
        "amount_inr": inr_str(row["amount_paise"]),
        "gold_grams": grams_str(row["gold_units"]),
        "price_inr_per_gram": inr_str(row["price_paise_per_gram"]),
        "price_stale": bool(row["price_stale"]),
        "sip_id": row["sip_id"], "sip_idx": row["sip_idx"],
        "created_at": row["created_at"], "duplicate": duplicate,
    }


def insert_transaction(conn, user_id, amount_paise, quote, key, kind, sip_id=None, sip_idx=None):
    units = gold_units(amount_paise, quote.price_paise)
    conn.execute(
        "INSERT INTO transactions(txn_id,user_id,kind,amount_paise,price_paise_per_gram,gold_units,"
        "price_stale,sip_id,sip_idx,idempotency_key,request_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_txn_id(), user_id, kind, amount_paise, quote.price_paise, units, int(quote.stale),
         sip_id, sip_idx, key, request_id_var.get(), clock.now().isoformat()))
    return conn.execute("SELECT * FROM transactions WHERE user_id=? AND idempotency_key=?", (user_id, key)).fetchone()


def _by_key(conn, user_id, key):
    return conn.execute("SELECT * FROM transactions WHERE user_id=? AND idempotency_key=?", (user_id, key)).fetchone()


def _recent_keyless_dup(conn, user_id, amount_paise):
    since = (clock.now() - timedelta(seconds=get_settings().dup_window_s)).isoformat()
    return conn.execute(
        "SELECT * FROM transactions WHERE user_id=? AND kind='one_time' AND amount_paise=? "
        "AND idempotency_key LIKE 'auto:%' AND created_at>=? ORDER BY id DESC LIMIT 1",
        (user_id, amount_paise, since)).fetchone()


def buy(user_id: str, amount_paise: int, idem_key=None) -> dict:
    """One-time purchase. Safe to call twice with the same key (returns the first result).
    With no key, an identical purchase inside DUP_WINDOW_S is treated as a retry."""
    check_amount(amount_paise)
    db.ensure_user(user_id)
    auto = idem_key is None
    key = idem_key or f"auto:{uuid.uuid4().hex}"
    with db.tx() as c:
        existing = _by_key(c, user_id, key) or (_recent_keyless_dup(c, user_id, amount_paise) if auto else None)
    if existing:
        log_event("purchase_duplicate_returned", txn_id=existing["txn_id"], idempotency_key=key)
        return receipt(existing, True)

    quote = pricing.get_quote(for_execution=True)   # network call happens OUTSIDE the db lock

    with db.tx() as c:
        existing = _by_key(c, user_id, key) or (_recent_keyless_dup(c, user_id, amount_paise) if auto else None)
        if existing:
            log_event("purchase_duplicate_returned", txn_id=existing["txn_id"], idempotency_key=key)
            return receipt(existing, True)
        row = insert_transaction(c, user_id, amount_paise, quote, key, "one_time")
    log_event("purchase_executed", txn_id=row["txn_id"], amount_inr=inr_str(amount_paise),
              gold_grams=grams_str(row["gold_units"]), price_inr_per_gram=inr_str(quote.price_paise),
              price_stale=quote.stale, idempotency_key=key)
    return receipt(row, False)


def ledger(user_id: str) -> list:
    c = db.connect()
    try:
        rows = c.execute("SELECT * FROM transactions WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    finally:
        c.close()
    return [receipt(r, False) for r in rows]


def portfolio(user_id: str) -> dict:
    c = db.connect()
    try:
        r = c.execute("SELECT COALESCE(SUM(amount_paise),0) a, COALESCE(SUM(gold_units),0) g, COUNT(*) n "
                      "FROM transactions WHERE user_id=?", (user_id,)).fetchone()
    finally:
        c.close()
    return {"user_id": user_id, "total_invested_inr": inr_str(r["a"]),
            "total_gold_grams": grams_str(r["g"]), "transactions": r["n"]}
