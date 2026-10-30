"""SIP (recurring purchase) lifecycle + schedule maths.

Schedule: instalment k falls on due_date(anchor, frequency, k) -- computed from the ANCHOR,
never by repeatedly adding to the previous date (that drifts: Jan 31 -> Feb 28 -> Mar 28...).
Monthly SIPs on the 29th/30th/31st fall on the last day of shorter months.
"""
import calendar
import uuid
from datetime import date, timedelta

from . import clock, db, pricing
from .logs import log_event, set_user
from .money import check_amount, grams_str, inr_str
from .purchases import insert_transaction

FREQS = ("daily", "weekly", "monthly")


class SipError(Exception):
    def __init__(self, code: str, message: str, http: int = 400):
        super().__init__(message)
        self.code, self.message, self.http = code, message, http


def due_date(anchor: date, anchor_day: int, freq: str, k: int) -> date:
    if freq == "daily":
        return anchor + timedelta(days=k)
    if freq == "weekly":
        return anchor + timedelta(days=7 * k)
    months = anchor.month - 1 + k
    y, m = anchor.year + months // 12, months % 12 + 1
    return date(y, m, min(anchor_day, calendar.monthrange(y, m)[1]))


def _next_on_or_after(day: int, today: date) -> date:
    cand = date(today.year, today.month, min(day, calendar.monthrange(today.year, today.month)[1]))
    if cand >= today:
        return cand
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def resolve_start(freq: str, anchor_day, start_hint, today: date):
    """Turn what the user said ('from the 31st', 'starting yesterday') into (anchor_date, anchor_day)."""
    base = None
    if start_hint == "yesterday":
        base = today - timedelta(days=1)
    elif start_hint == "today":
        base = today
    elif start_hint == "tomorrow":
        base = today + timedelta(days=1)
    elif start_hint:
        base = date.fromisoformat(start_hint)
    if freq == "monthly":
        if anchor_day:
            base = _next_on_or_after(anchor_day, base or today)
        else:
            base = base or today
            anchor_day = base.day
    else:
        base = base or today
        anchor_day = base.day
    return base, anchor_day


def first_due_idx(anchor: date, anchor_day: int, freq: str, today: date) -> int:
    """Smallest k whose due date is today or later. A past start date is NOT back-filled:
    we never charge a user for instalments they didn't see being scheduled."""
    k = 0
    while due_date(anchor, anchor_day, freq, k) < today:
        k += 1
        if k > 20000:
            raise SipError("bad_start", "Start date is too far in the past.")
    return k


def _view(row, duplicate=False, note=None) -> dict:
    nxt = due_date(date.fromisoformat(row["anchor_date"]), row["anchor_day"], row["frequency"], row["next_idx"])
    return {"sip_id": row["sip_id"], "user_id": row["user_id"], "status": row["status"],
            "amount_inr": inr_str(row["amount_paise"]), "frequency": row["frequency"],
            "next_due_date": None if row["status"] == "CANCELLED" else nxt.isoformat(),
            "created_at": row["created_at"], "duplicate": duplicate, "note": note}


def create_sip(user_id, amount_paise, freq, anchor_date: date, anchor_day: int) -> dict:
    if freq not in FREQS:
        raise SipError("bad_frequency", "Frequency must be daily, weekly or monthly.")
    check_amount(amount_paise)
    db.ensure_user(user_id)
    today = clock.today()
    anchor_key = {"daily": "-", "weekly": str(anchor_date.weekday()), "monthly": str(anchor_day)}[freq]
    nat_key = f"{user_id}|{amount_paise}|{freq}|{anchor_key}"
    idx = first_due_idx(anchor_date, anchor_day, freq, today)
    with db.tx() as c:
        dup = c.execute("SELECT * FROM sips WHERE user_id=? AND nat_key=? AND status IN ('ACTIVE','PAUSED') LIMIT 1",
                        (user_id, nat_key)).fetchone()
        if dup:
            log_event("sip_duplicate_returned", sip_id=dup["sip_id"])
            return _view(dup, duplicate=True, note="You already have an identical SIP; nothing new was created.")
        sip_id = "sip_" + uuid.uuid4().hex[:8]
        now = clock.now().isoformat()
        c.execute("INSERT INTO sips(sip_id,user_id,amount_paise,frequency,anchor_date,anchor_day,next_idx,"
                  "status,nat_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'ACTIVE',?,?,?)",
                  (sip_id, user_id, amount_paise, freq, anchor_date.isoformat(), anchor_day, idx, nat_key, now, now))
        row = c.execute("SELECT * FROM sips WHERE sip_id=?", (sip_id,)).fetchone()
    note = None
    if anchor_date < today:
        note = "Start date was in the past; past instalments are not back-filled. First debit is the next scheduled date."
    log_event("sip_created", sip_id=sip_id, amount_inr=inr_str(amount_paise), frequency=freq,
              anchor_date=anchor_date.isoformat(), first_due=_view(row)["next_due_date"])
    return _view(row, note=note)


def list_sips(user_id: str, include_cancelled=False) -> list:
    c = db.connect()
    try:
        q = "SELECT * FROM sips WHERE user_id=?" + ("" if include_cancelled else " AND status!='CANCELLED'")
        rows = c.execute(q + " ORDER BY created_at", (user_id,)).fetchall()
    finally:
        c.close()
    return [_view(r) for r in rows]


def set_status(user_id: str, sip_id: str, action: str) -> dict:
    """action: pause | resume | cancel. Idempotent; CANCELLED is terminal."""
    with db.tx() as c:
        row = c.execute("SELECT * FROM sips WHERE sip_id=? AND user_id=?", (sip_id, user_id)).fetchone()
        if not row:
            raise SipError("not_found", "No such SIP for this user.", 404)
        st = row["status"]
        if st == "CANCELLED" and action != "cancel":
            raise SipError("cancelled", "This SIP is cancelled and cannot be changed.", 409)
        new, idx = st, row["next_idx"]
        if action == "pause":
            new = "PAUSED"
        elif action == "resume":
            new = "ACTIVE"
            if st == "PAUSED":   # skip what was missed while paused: no back-fill
                idx = first_due_idx(date.fromisoformat(row["anchor_date"]), row["anchor_day"],
                                    row["frequency"], clock.today())
        elif action == "cancel":
            new = "CANCELLED"
        else:
            raise SipError("bad_action", "Unknown action.")
        if new != st or idx != row["next_idx"]:
            c.execute("UPDATE sips SET status=?, next_idx=?, updated_at=? WHERE sip_id=?",
                      (new, idx, clock.now().isoformat(), sip_id))
        row = c.execute("SELECT * FROM sips WHERE sip_id=?", (sip_id,)).fetchone()
    log_event(f"sip_{action}", sip_id=sip_id, from_status=st, to_status=new)
    return _view(row)


def run_due(as_of: date) -> list:
    """Execute every instalment due on or before `as_of`. Safe to run concurrently / repeatedly:
    each instalment has key sip:<id>:<idx> and a UNIQUE(sip_id, sip_idx) constraint, and the
    ledger insert + pointer advance happen in ONE transaction."""
    out = []
    c = db.connect()
    try:
        ids = [r["sip_id"] for r in c.execute("SELECT sip_id FROM sips WHERE status='ACTIVE' ORDER BY created_at")]
    finally:
        c.close()
    for sip_id in ids:
        while True:
            c = db.connect()
            try:
                row = c.execute("SELECT * FROM sips WHERE sip_id=?", (sip_id,)).fetchone()
            finally:
                c.close()
            if row["status"] != "ACTIVE":
                break
            set_user(row["user_id"])
            idx = row["next_idx"]
            due = due_date(date.fromisoformat(row["anchor_date"]), row["anchor_day"], row["frequency"], idx)
            if due > as_of:
                break
            try:
                quote = pricing.get_quote(for_execution=True)
            except pricing.PriceUnavailable as e:
                log_event("sip_instalment_deferred", level="warning", sip_id=sip_id, idx=idx,
                          due_date=due.isoformat(), reason=str(e))
                out.append({"sip_id": sip_id, "idx": idx, "due_date": due.isoformat(), "status": "deferred"})
                break  # keep order: don't skip ahead past a deferred instalment
            key = f"sip:{sip_id}:{idx}"
            with db.tx() as c:
                cur = c.execute("SELECT status, next_idx FROM sips WHERE sip_id=?", (sip_id,)).fetchone()
                if cur["status"] != "ACTIVE" or cur["next_idx"] != idx:
                    continue  # someone else advanced / paused it; re-read
                done = c.execute("SELECT * FROM transactions WHERE sip_id=? AND sip_idx=?", (sip_id, idx)).fetchone()
                if done:
                    status, txn = "already_executed", done
                else:
                    txn = insert_transaction(c, row["user_id"], row["amount_paise"], quote, key, "sip", sip_id, idx)
                    status = "executed"
                c.execute("UPDATE sips SET next_idx=?, updated_at=? WHERE sip_id=?",
                          (idx + 1, clock.now().isoformat(), sip_id))
            if status == "executed":
                log_event("sip_instalment_executed", sip_id=sip_id, idx=idx, due_date=due.isoformat(),
                          txn_id=txn["txn_id"], amount_inr=inr_str(txn["amount_paise"]),
                          gold_grams=grams_str(txn["gold_units"]), price_stale=quote.stale)
            out.append({"sip_id": sip_id, "idx": idx, "due_date": due.isoformat(), "status": status,
                        "txn_id": txn["txn_id"], "user_id": row["user_id"]})
    return out
