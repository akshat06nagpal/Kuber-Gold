"""Single source of 'now'. India has no DST so a fixed IST offset is exact."""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))
_override: Optional[datetime] = None


def now() -> datetime:
    return _override or datetime.now(IST)


def today() -> date:
    return now().date()


def set_override(dt: Optional[datetime]) -> None:
    global _override
    _override = dt
