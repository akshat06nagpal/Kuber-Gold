"""All money maths is integer maths.

  rupees  -> integer paise            (2 dp, exact)
  gold    -> integer 1/10000 gram     (4 dp, exact)

grams = amount / price. In integer units:
  units = amount_paise * 10_000 // price_paise_per_gram
Floor (not round): the customer is never credited with gold they did not pay for.
"""
from decimal import Decimal, InvalidOperation

from .config import get_settings

UNITS_PER_GRAM = 10_000


class AmountError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def inr_to_paise(value) -> int:
    """Parse a rupee amount with AT MOST 2 decimals. Never silently rounds user money."""
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        raise AmountError("invalid", "That doesn't look like a valid amount.")
    if not d.is_finite():
        raise AmountError("invalid", "That doesn't look like a valid amount.")
    if d != d.quantize(Decimal("0.01")):
        raise AmountError("too_many_decimals", "Rupee amounts can have at most 2 decimal places.")
    return int(d * 100)


def check_amount(paise: int) -> None:
    s = get_settings()
    if paise <= 0:
        raise AmountError("non_positive", "Amount must be greater than zero.")
    if paise < s.min_paise:
        raise AmountError("too_small", f"Minimum purchase is {fmt_inr(s.min_paise)}.")
    if paise > s.max_paise:
        raise AmountError("too_large", f"Maximum per purchase is {fmt_inr(s.max_paise)}.")


def gold_units(amount_paise: int, price_paise_per_gram: int) -> int:
    if amount_paise <= 0 or price_paise_per_gram <= 0:
        raise ValueError("amount and price must be positive")
    return (amount_paise * UNITS_PER_GRAM) // price_paise_per_gram


def fmt_inr(paise: int) -> str:
    """'₹12,500.00' (display)."""
    return "₹" + f"{Decimal(paise).scaleb(-2):,.2f}"


def inr_str(paise: int) -> str:
    """'12500.00' (API)."""
    return f"{Decimal(paise).scaleb(-2):.2f}"


def grams_str(units: int) -> str:
    """'0.0400' (API and display)."""
    return f"{Decimal(units).scaleb(-4):.4f}"
