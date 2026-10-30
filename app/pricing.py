"""Gold price source with a deliberate failure policy.

When the source is unreachable:
  * quotes for INFORMATION may use the last good price if it is < PRICE_INFO_MAX_AGE_S old,
    and are labelled stale.
  * EXECUTION (charging money) may only use a price < PRICE_EXEC_MAX_AGE_S old.
    Otherwise we refuse (PriceUnavailable): nothing is charged, pending requests stay
    pending, SIP instalments stay due and are retried on the next tick.
We never guess a price.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import httpx

from . import clock, db
from .config import get_settings
from .logs import log_event
from .money import inr_to_paise


class PriceUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Quote:
    price_paise: int       # paise per gram
    as_of: datetime
    stale: bool


class MockProvider:
    """Hard-coded price. `outage` lets tests / the demo simulate the source going down."""
    outage = False

    def fetch(self) -> int:
        if self.outage:
            raise ConnectionError("simulated price-source outage")
        return inr_to_paise(get_settings().mock_price_inr)


MOCK = MockProvider()


def _fetch_http() -> int:
    url = get_settings().price_url
    if not url:
        raise RuntimeError("PRICE_URL not set")
    last = None
    for _ in range(2):  # one retry
        try:
            r = httpx.get(url, timeout=3.0)
            r.raise_for_status()
            price = Decimal(str(r.json()["price_per_gram_inr"]))
            return int((price * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def _fetch() -> int:
    return MOCK.fetch() if get_settings().price_mode == "mock" else _fetch_http()


def _cached():
    c = db.connect()
    try:
        return c.execute("SELECT price_paise, fetched_at FROM price_cache WHERE id=1").fetchone()
    finally:
        c.close()


def get_quote(for_execution: bool = False) -> Quote:
    now = clock.now()
    s = get_settings()
    cached = _cached()
    try:
        paise = _fetch()
        if paise <= 0:
            raise ValueError("non-positive price")
        if cached and abs(paise - cached["price_paise"]) * 5 > cached["price_paise"]:
            # >20% jump vs the last good price: treat as bad data, not as truth
            log_event("price_rejected_outlier", level="warning", new=paise, last=cached["price_paise"])
            raise ValueError("outlier price")
        with db.tx() as c:
            c.execute("INSERT INTO price_cache(id, price_paise, fetched_at) VALUES(1,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET price_paise=excluded.price_paise, fetched_at=excluded.fetched_at",
                      (paise, now.isoformat()))
        return Quote(paise, now, False)
    except Exception as e:  # noqa: BLE001
        log_event("price_fetch_failed", level="warning", error=str(e)[:200], for_execution=for_execution)
    if not cached:
        raise PriceUnavailable("price source unreachable and no previous price available")
    as_of = datetime.fromisoformat(cached["fetched_at"])
    age = (now - as_of).total_seconds()
    limit = s.price_exec_max_age_s if for_execution else s.price_info_max_age_s
    if age > limit:
        log_event("price_too_stale", level="warning", age_s=int(age), limit_s=limit, for_execution=for_execution)
        raise PriceUnavailable(f"last known price is {int(age)}s old (limit {limit}s)")
    log_event("price_stale_used", level="warning", age_s=int(age), for_execution=for_execution)
    return Quote(cached["price_paise"], as_of, True)
