"""These tests exist to FAIL if someone introduces a rounding error in the purchase maths."""
import pytest

from app.money import AmountError, fmt_inr, gold_units, grams_str, inr_str, inr_to_paise

PRICE = 1_250_000  # ₹12,500.00 / g in paise


def test_exact_cases():
    assert gold_units(50_000, PRICE) == 400          # ₹500 -> 0.0400 g
    assert gold_units(1_000, PRICE) == 8             # ₹10  -> 0.0008 g
    assert grams_str(gold_units(50_000, PRICE)) == "0.0400"


def test_floor_not_round():
    # ₹333.33 / ₹12,500 = 0.0266664 g. Rounding would give 0.0267; the customer must get 0.0266.
    assert gold_units(33_333, PRICE) == 266
    assert grams_str(266) == "0.0266"
    # ₹99.99 / ₹12,345.67 = 0.00809... -> 0.0080 (round-half-up would say 0.0081)
    assert gold_units(9_999, 1_234_567) == 80


def test_never_credits_more_gold_than_paid_for():
    for price in (1_234_567, 999_999, 1_250_000, 7_777_777):
        for amount in (1_000, 1_001, 33_333, 99_999, 123_457, 10_000_000):
            u = gold_units(amount, price)
            assert u * price <= amount * 10_000 < (u + 1) * price   # exact floor, no float drift


def test_no_float_artifacts():
    assert inr_to_paise("0.1") + inr_to_paise("0.2") == inr_to_paise("0.3")
    assert inr_to_paise(333.33) == 33_333
    assert inr_str(33_333) == "333.33"
    assert fmt_inr(1_250_000) == "₹12,500.00"


@pytest.mark.parametrize("bad", ["333.333", "0.001", "NaN", "Infinity", "abc"])
def test_rejects_more_than_two_decimals_or_garbage(bad):
    with pytest.raises(AmountError):
        inr_to_paise(bad)
