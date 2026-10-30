"""Conversation layer: message -> intent -> reply (+ optional pending action the user must confirm).

Money-moving actions (purchase, SIP creation) are two-step: the bot proposes, the user says yes.
The confirmation executes with idempotency key `pending:<id>`, so a repeated "yes" can't charge twice.
"""
import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal

from . import clock, db, llm, pricing, purchases, sips
from . import intent as I
from .config import get_settings
from .logs import log_event
from .money import AmountError, fmt_inr, grams_str, gold_units, inr_str

MENU = ("I can help with digital gold: tell you today's price, work out what an amount buys, "
        "buy a one-time amount, or set up a SIP (daily / weekly / monthly).")
NUDGE = "Want to start small? Say e.g. “buy ₹100 of gold” (minimum ₹10) or “SIP of ₹500 monthly”."
DISCLAIMER = "This is general information, not personalised investment advice."


def _r(reply, action="none", **data):
    return {"reply": reply, "action": action, "data": data}


def is_hinglish(t: str) -> bool:
    return bool(re.search(r"\b(bhai|sona|chahiye|nahi|le lo|lena|kharid|abhi|mujhe|karo|hai|ka)\b", t.lower()))


# ---------- pending (confirmation) state ----------
def get_pending(user_id):
    c = db.connect()
    try:
        row = c.execute("SELECT * FROM pending_actions WHERE user_id=? AND status='pending' "
                        "ORDER BY created_at DESC LIMIT 1", (user_id,)).fetchone()
    finally:
        c.close()
    if not row:
        return None
    age = (clock.now() - datetime.fromisoformat(row["created_at"])).total_seconds()
    if age > get_settings().pending_ttl_s:
        _set_pending(row["id"], "expired")
        log_event("pending_expired", pending_id=row["id"])
        return None
    return {"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"])}


def _set_pending(pid, status):
    with db.tx() as c:
        c.execute("UPDATE pending_actions SET status=? WHERE id=?", (status, pid))


def create_pending(user_id, kind, payload):
    blob = json.dumps(payload, sort_keys=True)
    with db.tx() as c:
        same = c.execute("SELECT id FROM pending_actions WHERE user_id=? AND status='pending' AND kind=? AND payload=?",
                         (user_id, kind, blob)).fetchone()
        if same:
            return same["id"], False
        c.execute("UPDATE pending_actions SET status='superseded' WHERE user_id=? AND status='pending'", (user_id,))
        pid = "pnd_" + uuid.uuid4().hex[:10]
        c.execute("INSERT INTO pending_actions(id,user_id,kind,payload,status,created_at) VALUES(?,?,?,?,?,?)",
                  (pid, user_id, kind, blob, "pending", clock.now().isoformat()))
    log_event("pending_created", pending_id=pid, kind=kind, payload=payload)
    return pid, True


def _quote():
    try:
        return pricing.get_quote(for_execution=False)
    except pricing.PriceUnavailable:
        return None


def _price_line(q):
    if not q:
        return "I can't fetch the live gold price right now"
    s = f"{fmt_inr(q.price_paise)}/g"
    return s + (" (last known price, may be slightly out of date)" if q.stale else "")


def _amount_error(a: I.Amount) -> str:
    s = get_settings()
    return {
        "non_positive": "The amount has to be more than ₹0, so I haven't done anything. "
                        f"Please send a rupee amount between {fmt_inr(s.min_paise)} and {fmt_inr(s.max_paise)}.",
        "too_small": f"The minimum is {fmt_inr(s.min_paise)} per purchase, so I haven't done anything.",
        "too_large": f"That's above the {fmt_inr(s.max_paise)} per-purchase limit, so I haven't done anything. "
                     "Please choose a smaller amount.",
        "too_many_decimals": "Rupee amounts can have at most 2 decimal places (e.g. ₹333.33). "
                             "Nothing has been charged — please resend the amount.",
    }[a.error]


# ---------- advice text (LLM if available, else vetted templates) ----------
def _advice_template(t: str, kind: str) -> str:
    if kind == "COMPARE":
        return ("Gold and an FD do different jobs. An FD pays a fixed, known interest (taxable) — good for money you "
                "need on a date. Gold pays no interest and its price can fall as well as rise, but many people hold "
                "a modest amount to diversify and hedge against inflation. It doesn't have to be either/or.")
    if kind == "GOLD_EDU":
        return ("Digital gold lets you buy gold online in small rupee amounts (from ₹10) and track what you own in "
                "grams, priced per gram. Prices move with global markets, so the value can go up or down.")
    if is_hinglish(t):
        return ("Sach bolun toh gold ka perfect timing koi nahi bata sakta — price upar-neeche hota rehta hai. "
                "Zyada log ise portfolio ka chhota hissa rakhte hain aur thoda-thoda karke kharidte hain. "
                "Aapke goals aur time horizon pe depend karta hai.")
    if re.search(r"\bsafe", t):
        return ("Your mom has a point: gold has long been seen as a store of value, especially in uncertain times. "
                "But “safest” overstates it — its price can fall, or stay flat for years. It's best as one part of a "
                "mix, not the whole plan.")
    return ("Nobody can reliably time gold: its price moves with global rates, the dollar and sentiment, and it can "
            "fall as well as rise. Many people keep a modest share as a diversifier and buy gradually rather than "
            "all at once. Whether it fits you depends on your goals and time horizon.")


_BAD_LLM = re.compile(r"(i have (bought|purchased|booked|confirmed)|purchase (is )?confirmed|guaranteed|"
                      r"will (definitely|certainly) (rise|go up)|system prompt)", re.I)


def _advice(message: str, kind: str, q) -> str:
    ctx = f"Current price: {fmt_inr(q.price_paise)}/g." if q else "Live price unavailable."
    text = llm.complete(llm.ADVICE_SYSTEM, f"{ctx}\nUser message: {message}")
    if text and not _BAD_LLM.search(text):
        return text
    if text:
        log_event("llm_output_rejected", reason="guardrail")
    return _advice_template(message.lower(), kind)


# ---------- handlers ----------
def h_buy(u, msg, p, pending):
    a = p.amount
    if a.error:
        return _r(_amount_error(a), "rejected", error=a.error)
    q = _quote()
    pid, created = create_pending(u, "purchase", {"amount_paise": a.paise})
    est = ""
    if q:
        est = f" At {fmt_inr(q.price_paise)}/g that's about {grams_str(gold_units(a.paise, q.price_paise))} g " \
              "(the final grams are fixed when you confirm)."
    head = "Got it" if created else "That's already waiting for your confirmation"
    return _r(f"{head} — buy {fmt_inr(a.paise)} of digital gold.{est} Reply “yes” to confirm or “no” to cancel.",
              "pending_purchase", pending_id=pid, amount_inr=inr_str(a.paise), duplicate=not created)


def h_invest_ambiguous(u, msg, p, pending):
    a = p.amount
    if a.error:
        return _r(_amount_error(a), "rejected", error=a.error)
    pid, created = create_pending(u, "purchase", {"amount_paise": a.paise})
    return _r(f"Nice. I can only invest in digital gold, so here's the plan: buy {fmt_inr(a.paise)} of gold now. "
              "Reply “yes” to go ahead, or tell me if you'd rather spread it — e.g. “SIP of "
              f"{fmt_inr(a.paise)} monthly”.", "pending_purchase", pending_id=pid, amount_inr=inr_str(a.paise))


def h_calc(u, msg, p, pending):
    a = p.amount
    if a.error:
        return _r(_amount_error(a), "rejected", error=a.error)
    q = _quote()
    if not q:
        return _r("I can't fetch the gold price right now, so I won't guess a number. Please try again in a minute.",
                  "none", price_available=False)
    g = grams_str(gold_units(a.paise, q.price_paise))
    pid, _ = create_pending(u, "purchase", {"amount_paise": a.paise})
    stale = " (using the last known price)" if q.stale else ""
    return _r(f"{fmt_inr(a.paise)} buys about {g} g of gold at {fmt_inr(q.price_paise)}/g{stale}. "
              "Want me to buy it? Reply “yes” to confirm.", "pending_purchase",
              pending_id=pid, gold_grams=g, price_inr_per_gram=inr_str(q.price_paise))


def h_price(u, msg, p, pending):
    q = _quote()
    if not q:
        return _r("I can't fetch the live gold price right now. Please try again shortly.", "none")
    return _r(f"Gold is {_price_line(q)}. {NUDGE}")


def h_advice(u, msg, p, pending):
    q = _quote()
    return _r(f"{_advice(msg, p.intent, q)}\n\n{NUDGE}\n{DISCLAIMER}", "none")


def h_gold_bare(u, msg, p, pending):
    q = _quote()
    return _r(f"Gold! Today it's {_price_line(q)}. What would you like to do — learn how digital gold works, "
              f"see what an amount buys, buy some, or start a SIP? {NUDGE}", "none")


def h_buy_no_amount(u, msg, p, pending):
    return _r("Sure — how much would you like to buy, in rupees? (₹10 minimum). For example: “buy ₹500 of gold”.", "none")


def h_gram_request(u, msg, p, pending):
    q = _quote()
    cost = ""
    if q:
        paise = int((p.grams * q.price_paise).to_integral_value())
        cost = f" At today's price, {p.grams} g would cost roughly {fmt_inr(paise)}."
    return _r("I take purchases as a rupee amount, not grams." + cost + " Tell me how many rupees you'd like to invest.", "none")


def h_injection(u, msg, p, pending):
    return _r("I can't help with that. I don't change my rules or share internal instructions, and purchases "
              "always need a real payment. If you'd like gold, tell me an amount in rupees.", "refused")


def h_free(u, msg, p, pending):
    return _r("Gold purchases are always paid for — I can't confirm or credit anything for free. "
              "If you'd like to buy, tell me a rupee amount (₹10 minimum).", "refused")


def h_off_topic(u, msg, p, pending):
    extra = ""
    if re.search(r"\b(itr|tax|income tax)\b", msg.lower()):
        extra = " For ITR dates, the Income Tax Department's official website is the reliable source."
    elif re.search(r"\b(cab|taxi|uber|ola|flight|hotel)\b", msg.lower()):
        extra = " I can't book travel."
    return _r("That's outside what I can help with." + extra + " " + MENU, "none")


def h_nothing_pending(u, msg, p, pending):
    return _r("There's nothing waiting for your confirmation right now. " + MENU, "none")


def h_empty(u, msg, p, pending):
    return _r("Say something like “what's the gold price?” or “buy ₹100 of gold”. " + MENU, "none")


def h_unknown(u, msg, p, pending):
    label = llm.classify(msg)
    if label in ("GOLD_ADVICE", "COMPARE", "GOLD_EDU"):
        p.intent = label
        return h_advice(u, msg, p, pending)
    if label == "PRICE_QUERY":
        return h_price(u, msg, p, pending)
    return _r("I'm not sure what you'd like to do. " + MENU, "none")


def h_confirm(u, msg, p, pending):
    kind, pl, pid = pending["kind"], pending["payload"], pending["id"]
    if kind == "purchase":
        try:
            rc = purchases.buy(u, pl["amount_paise"], idem_key=f"pending:{pid}")
        except pricing.PriceUnavailable:
            log_event("confirm_deferred_price_unavailable", pending_id=pid)
            return _r("I can't get a verified gold price right now, so I haven't charged anything. "
                      "Your request is still saved — reply “yes” again in a minute.", "deferred", pending_id=pid)
        _set_pending(pid, "confirmed")
        total = purchases.portfolio(u)
        dup = " (This was already completed — you were not charged again.)" if rc["duplicate"] else ""
        return _r(f"Done ✅ Bought {rc['gold_grams']} g of digital gold for ₹{rc['amount_inr']} at "
                  f"{fmt_inr(int(Decimal(rc['price_inr_per_gram']) * 100))}/g. Ref {rc['txn_id']}. You now hold {total['total_gold_grams']} g.{dup} "
                  "Want to make it a habit? Try “SIP of ₹100 weekly”.", "purchase_executed", receipt=rc)
    if kind == "sip":
        res = sips.create_sip(u, pl["amount_paise"], pl["freq"], date.fromisoformat(pl["anchor_date"]), pl["anchor_day"])
        _set_pending(pid, "confirmed")
        extra = f" {res['note']}" if res.get("note") else ""
        return _r(f"Your {res['frequency']} SIP of ₹{res['amount_inr']} is {res['status'].lower()}. "
                  f"Next debit: {res['next_due_date']}. You can say “pause my SIP” or “cancel my SIP” any time.{extra}",
                  "sip_created", sip=res)
    return _r("Nothing to confirm.", "none")


def h_decline(u, msg, p, pending):
    _set_pending(pending["id"], "cancelled")
    log_event("pending_cancelled", pending_id=pending["id"])
    return _r("No problem — cancelled, nothing was charged.", "cancelled")


def h_sip_create(u, msg, p, pending):
    a = p.amount
    if a.error:
        return _r(_amount_error(a), "rejected", error=a.error)
    if not p.freq:
        return _r(f"How often should the {fmt_inr(a.paise)} SIP run — daily, weekly or monthly?", "none")
    try:
        anchor, day = sips.resolve_start(p.freq, p.anchor_day, p.start_hint, clock.today())
        idx = sips.first_due_idx(anchor, day, p.freq, clock.today())
    except (ValueError, sips.SipError):
        return _r("I couldn't understand that start date. Try “from the 5th”, “starting tomorrow” or a date like 2026-11-05.", "none")
    first = sips.due_date(anchor, day, p.freq, idx)
    payload = {"amount_paise": a.paise, "freq": p.freq, "anchor_date": anchor.isoformat(), "anchor_day": day}
    pid, created = create_pending(u, "sip", payload)
    when = {"daily": "every day", "weekly": f"every week on {first.strftime('%A')}",
            "monthly": f"every month on the {day}{'th' if day > 3 else ['','st','nd','rd'][day]}"
                       + (" (last day of shorter months)" if day > 28 else "")}[p.freq]
    past = " Your start date has passed; I don't back-fill past instalments, so the first debit is the next date." \
        if anchor < clock.today() else ""
    head = "Got it" if created else "That SIP is already waiting for your confirmation"
    return _r(f"{head} — SIP of {fmt_inr(a.paise)} {when}. First debit: {first.isoformat()}.{past} "
              "Reply “yes” to confirm or “no” to cancel.", "pending_sip", pending_id=pid,
              first_due_date=first.isoformat(), duplicate=not created)


def h_sip_no_amount(u, msg, p, pending):
    return _r("Happy to set up a SIP. How much per instalment (₹10 minimum) and how often — daily, weekly or "
              "monthly? e.g. “SIP of ₹500 monthly”.", "none")


def h_sip_manage(u, msg, p, pending):
    allowed = {"pause": ("ACTIVE",), "resume": ("PAUSED",), "cancel": ("ACTIVE", "PAUSED")}[p.manage]
    mine = sips.list_sips(u)
    if p.sip_id:
        cands = [s for s in mine if s["sip_id"] == p.sip_id]
    else:
        cands = [s for s in mine if s["status"] in allowed]
    verb = {"pause": "paused", "resume": "resumed", "cancel": "cancelled"}[p.manage]
    if not cands:
        if not mine:
            return _r("You don't have any SIPs yet. Want to start one? e.g. “SIP of ₹500 monthly”.", "none")
        return _r(f"None of your SIPs can be {verb} right now (statuses: "
                  + ", ".join(f"{s['sip_id']} {s['status'].lower()}" for s in mine) + ").", "none")
    if len(cands) > 1:
        opts = "; ".join(f"{s['sip_id']}: ₹{s['amount_inr']} {s['frequency']}" for s in cands)
        return _r(f"You have {len(cands)} SIPs ({opts}). Which one? Say e.g. “{p.manage} {cands[0]['sip_id']}”.",
                  "clarify", options=[s["sip_id"] for s in cands])
    s = sips.set_status(u, cands[0]["sip_id"], p.manage)
    extra = " (Say “cancel my SIP” to end it permanently.)" if p.manage == "pause" and re.search(r"\bstop\b", msg.lower()) else ""
    return _r(f"Your {s['frequency']} SIP of ₹{s['amount_inr']} ({s['sip_id']}) is now {verb}.{extra}",
              f"sip_{p.manage}", sip=s)


HANDLERS = {
    "BUY": h_buy, "INVEST_AMBIGUOUS": h_invest_ambiguous, "CALC": h_calc, "PRICE_QUERY": h_price,
    "GOLD_ADVICE": h_advice, "COMPARE": h_advice, "GOLD_EDU": h_advice, "GOLD_BARE": h_gold_bare,
    "BUY_NO_AMOUNT": h_buy_no_amount, "GRAM_REQUEST": h_gram_request, "INJECTION": h_injection,
    "FREE_REQUEST": h_free, "OFF_TOPIC": h_off_topic, "EMPTY": h_empty, "NOTHING_PENDING": h_nothing_pending, "UNKNOWN": h_unknown,
    "CONFIRM": h_confirm, "DECLINE": h_decline, "SIP_CREATE": h_sip_create,
    "SIP_NO_AMOUNT": h_sip_no_amount, "SIP_MANAGE": h_sip_manage,
}


def handle_chat(user_id: str, message: str) -> dict:
    message = (message or "")[:1000]
    db.ensure_user(user_id)
    log_event("chat_received", message=message[:200])
    pending = get_pending(user_id)
    p = I.parse(message, has_pending=pending is not None)
    log_event("intent_classified", intent=p.intent, has_pending=pending is not None,
              amount_paise=p.amount.paise if p.amount else None, amount_error=p.amount.error if p.amount else None,
              freq=p.freq)
    try:
        out = HANDLERS[p.intent](user_id, message, p, pending)
    except AmountError as e:
        out = _r(e.message, "rejected", error=e.code)
    except sips.SipError as e:
        out = _r(e.message, "rejected", error=e.code)
    out["intent"] = p.intent
    log_event("chat_replied", intent=p.intent, action=out["action"], reply=out["reply"][:160])
    return out
