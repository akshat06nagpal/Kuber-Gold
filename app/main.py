import asyncio
import contextlib
import json
import time
import uuid
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import chat, clock, db, pricing, purchases, sips
from .config import get_settings
from .logs import log_event, request_id_var, set_user, setup_logging
from .money import AmountError, check_amount, inr_to_paise

UID = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")


class ChatIn(BaseModel):
    user_id: str = UID
    message: str = Field(max_length=4000)


class PurchaseIn(BaseModel):
    user_id: str = UID
    amount_inr: Decimal


class SipIn(BaseModel):
    user_id: str = UID
    amount_inr: Decimal
    frequency: Literal["daily", "weekly", "monthly"]
    start_date: Optional[date] = None
    day_of_month: Optional[int] = Field(default=None, ge=1, le=31)


class UserIn(BaseModel):
    user_id: str = UID


class RunDueIn(BaseModel):
    as_of: Optional[date] = None


class OutageIn(BaseModel):
    on: bool


async def _scheduler():
    while True:
        await asyncio.sleep(get_settings().scheduler_interval_s)
        try:
            res = await asyncio.to_thread(sips.run_due, clock.today())
            if res:
                log_event("scheduler_tick", processed=len(res))
        except Exception as e:  # noqa: BLE001
            log_event("scheduler_error", level="error", error=str(e)[:200])


@contextlib.asynccontextmanager
async def lifespan(app):
    setup_logging()
    db.init_db()
    task = asyncio.create_task(_scheduler()) if get_settings().scheduler_enabled else None
    log_event("app_started", price_mode=get_settings().price_mode, llm=bool(get_settings().llm_api_key))
    yield
    if task:
        task.cancel()


app = FastAPI(title="Kuber Gold", lifespan=lifespan)


class RequestContext(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or "req_" + uuid.uuid4().hex[:12]
        request_id_var.set(rid)
        t0 = time.time()
        resp = await call_next(request)
        resp.headers["X-Request-ID"] = rid
        log_event("http_request", method=request.method, path=request.url.path, status=resp.status_code,
                  ms=int((time.time() - t0) * 1000))
        return resp


app.add_middleware(RequestContext)


@app.exception_handler(AmountError)
async def _amount_err(request, exc: AmountError):
    return JSONResponse({"error": exc.code, "message": exc.message}, status_code=422)


@app.exception_handler(sips.SipError)
async def _sip_err(request, exc: sips.SipError):
    return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.http)


@app.exception_handler(pricing.PriceUnavailable)
async def _price_err(request, exc):
    return JSONResponse({"error": "price_unavailable", "retryable": True,
                         "message": "Gold price is temporarily unavailable. Nothing was charged. Please retry shortly."},
                        status_code=503, headers={"Retry-After": "30"})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat")
def post_chat(body: ChatIn, idempotency_key: Optional[str] = Header(default=None)):
    set_user(body.user_id)
    if idempotency_key:
        db.ensure_user(body.user_id)
        c = db.connect()
        try:
            hit = c.execute("SELECT response FROM chat_idem WHERE user_id=? AND idem_key=?",
                            (body.user_id, idempotency_key)).fetchone()
        finally:
            c.close()
        if hit:
            log_event("chat_idempotent_replay", idempotency_key=idempotency_key)
            return json.loads(hit["response"])
    out = chat.handle_chat(body.user_id, body.message)
    out["request_id"] = request_id_var.get()
    if idempotency_key:
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO chat_idem(user_id,idem_key,response,created_at) VALUES(?,?,?,?)",
                      (body.user_id, idempotency_key, json.dumps(out), clock.now().isoformat()))
    return out


@app.post("/purchases")
def post_purchase(body: PurchaseIn, idempotency_key: Optional[str] = Header(default=None)):
    set_user(body.user_id)
    paise = inr_to_paise(body.amount_inr)
    check_amount(paise)
    return purchases.buy(body.user_id, paise, idem_key=idempotency_key)


@app.get("/users/{user_id}/ledger")
def get_ledger(user_id: str):
    set_user(user_id)
    return {"user_id": user_id, "transactions": purchases.ledger(user_id)}


@app.get("/users/{user_id}/portfolio")
def get_portfolio(user_id: str):
    set_user(user_id)
    return purchases.portfolio(user_id)


@app.post("/sips")
def post_sip(body: SipIn):
    set_user(body.user_id)
    paise = inr_to_paise(body.amount_inr)
    check_amount(paise)
    hint = body.start_date.isoformat() if body.start_date else None
    anchor, day = sips.resolve_start(body.frequency, body.day_of_month, hint, clock.today())
    return sips.create_sip(body.user_id, paise, body.frequency, anchor, day)


@app.get("/users/{user_id}/sips")
def get_sips(user_id: str):
    set_user(user_id)
    return {"user_id": user_id, "sips": sips.list_sips(user_id, include_cancelled=True)}


@app.post("/sips/{sip_id}/{action}")
def sip_action(sip_id: str, action: Literal["pause", "resume", "cancel"], body: UserIn):
    set_user(body.user_id)
    return sips.set_status(body.user_id, sip_id, action)


@app.post("/admin/run-due-sips")
def run_due(body: RunDueIn = RunDueIn()):
    """Scheduler trigger (also runs automatically every SCHEDULER_INTERVAL_S). `as_of` lets you
    simulate future dates for demos/tests; instalments are idempotent so re-running is harmless."""
    res = sips.run_due(body.as_of or clock.today())
    return {"processed": res}


@app.post("/admin/price-outage")
def price_outage(body: OutageIn):
    """Demo/test switch (mock price source only): simulate the price source being down."""
    pricing.MOCK.outage = body.on
    log_event("admin_price_outage_set", on=body.on)
    return {"outage": body.on}
