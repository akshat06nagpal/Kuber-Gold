"""Deterministic message understanding. Rules decide WHAT the user wants and parse amounts;
the LLM is only consulted afterwards for wording (or to classify a message the rules can't place).
Rules run in a fixed order -- the order is the policy (e.g. injection is checked before anything else)."""
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import List, Optional

from .config import get_settings

# ---------- patterns ----------
INJECTION = re.compile(
    r"(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instruction|prompt|rule|guideline|polic)"
    r"|\b(system|developer|hidden|initial)\s*(prompt|message|instruction)"
    r"|developer mode|\bjailbreak|\bdan mode|\byou are now\b|\bact as\b|\bpretend (to be|you)"
    r"|(reveal|print|show|repeat|leak)\b.{0,20}\b(prompt|instructions)")
FREE = re.compile(r"\b(for free|free of cost|free of charge|without (paying|payment|money)|no charge|zero cost|"
                  r"muft|mufat|bina paise|free me|free mein)\b")
GOLD = re.compile(r"\b(gold|sona|sone|swarna|24\s?k|bullion|sgb)\b")
OTHER_ASSET = re.compile(r"\b(fd|fds|fixed deposits?|ppf|nps|mutual funds?|stocks?|shares?|equity|nifty|sensex|"
                         r"crypto|bitcoin|real estate|silver|etf|savings account|rd)\b")
BUY_VERB = re.compile(r"\b(buy|purchase|invest|order|get me|le lo|lelo|lena|le le|kharid\w*|khareed\w*)\b")
INVEST_WORD = re.compile(r"\b(invest\w*|buy|purchase|le lo|lelo|lena|kharid\w*|khareed\w*)\b")
QUESTION = re.compile(r"\b(should|good|worth it|safe|safest|sahi|better|best|advice|recommend|suggest|"
                      r"is it|is gold|right now)\b|ya nahi|ya nahin|\bkya (main|mujhe|lena|le)\b")
HOWMUCH = re.compile(r"\b(how much|how many|kitna|kitne|kitni)\b")
PRICE_WORDS = re.compile(r"\b(price|rate|bhav|bhaav|kimat|keemat|daam|cost)\b")
EDU_WORDS = re.compile(r"\b(what is|what's|how does|how do|explain|kya hai|kaise|tell me about|works?)\b")
SIP_WORD = re.compile(r"\b(sip|sips|systematic|recurring|auto[- ]?invest|auto[- ]?buy|standing instruction)\b")
SIP_START = re.compile(r"\b(start|set up|setup|create|begin|new|open|schedule|want)\b")
PAUSE = re.compile(r"\b(pause|hold|stop|suspend|rok)\b")
RESUME = re.compile(r"\b(resume|restart|unpause|continue|reactivate)\b")
CANCEL = re.compile(r"\b(cancel|delete|terminate|close|end|remove|band)\b")
CONFIRM = re.compile(r"^(yes|y|yeah|yep|yup|confirm|confirmed|ok|okay|sure|go ahead|do it|proceed|haan|han|ha|"
                     r"kar do|karo|theek hai)\W*$")
DECLINE = re.compile(r"^(no|n|nope|cancel|nahi|nahin|stop|dont|don't|abort|leave it)\W*$")
FREQ = {
    "daily": re.compile(r"\b(daily|every ?day|each day|per day|a day|roz|rozana|har din)\b"),
    "weekly": re.compile(r"\b(weekly|every ?week|each week|per week|a week|hafte|haftawar|har hafte)\b"),
    "monthly": re.compile(r"\b(monthly|every ?month|each month|per month|a month|har mahine|mahine)\b"),
}
SIP_ID = re.compile(r"\b(sip_[0-9a-f]{8})\b")
ORDINAL = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b")
ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
START_HINT = re.compile(r"\b(yesterday|today|tomorrow)\b")
GRAMS = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s?(?:g|gm|gms|gram|grams|gr|kg)\b")
MASK = re.compile(r"\b\d+\s*(?:days?|weeks?|months?|years?|yrs?|%|percent)\b|\b\d{4}-\d{2}-\d{2}\b")
NUM = re.compile(
    r"(?<![\w.])(?P<neg>-\s?)?(?:(?:₹|rs\.?|inr)\s?)?(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?P<unit>\s?(?:k|lakh|lakhs|lac|crore|cr))?\b")
UNIT_MULT = {"k": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "crore": 10_000_000, "cr": 10_000_000}


@dataclass
class Amount:
    raw: str
    value: Decimal
    paise: Optional[int] = None
    error: Optional[str] = None   # non_positive | too_many_decimals | too_small | too_large


@dataclass
class Parsed:
    intent: str
    amount: Optional[Amount] = None
    grams: Optional[Decimal] = None
    freq: Optional[str] = None
    anchor_day: Optional[int] = None
    start_hint: Optional[str] = None
    sip_id: Optional[str] = None
    manage: Optional[str] = None   # pause | resume | cancel


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def extract_amount(t: str) -> Optional[Amount]:
    t = MASK.sub(" ", GRAMS.sub(" ", t))
    s = get_settings()
    m = NUM.search(t)
    if not m:
        return None
    num = m.group("num").replace(",", "").rstrip(".")
    unit = (m.group("unit") or "").strip()
    value = Decimal(num) * UNIT_MULT.get(unit, 1)
    if m.group("neg"):
        value = -value
    a = Amount(raw=m.group(0).strip(), value=value)
    if value <= 0:
        a.error = "non_positive"
    elif value != value.quantize(Decimal("0.01")):
        a.error = "too_many_decimals"
    else:
        a.paise = int(value * 100)
        if a.paise < s.min_paise:
            a.error = "too_small"
        elif a.paise > s.max_paise:
            a.error = "too_large"
    return a


def detect_freq(t: str) -> Optional[str]:
    for name, rx in FREQ.items():
        if rx.search(t):
            return name
    return None


def parse(message: str, has_pending: bool = False) -> Parsed:
    t = norm(message)
    if not t:
        return Parsed("EMPTY")
    if INJECTION.search(t):
        return Parsed("INJECTION")
    if CONFIRM.match(t) or DECLINE.match(t):
        if not has_pending:
            return Parsed("NOTHING_PENDING")
        return Parsed("CONFIRM" if CONFIRM.match(t) else "DECLINE")

    gold = bool(GOLD.search(t))
    other = bool(OTHER_ASSET.search(t))
    buyv = bool(BUY_VERB.search(t))
    amt = extract_amount(t)
    gm = GRAMS.search(t)
    freq = detect_freq(t)
    sip_word = bool(SIP_WORD.search(t))

    # --- SIP management (pause / resume / cancel an existing SIP)
    if sip_word and not SIP_START.search(t):
        manage = "resume" if RESUME.search(t) else "cancel" if CANCEL.search(t) else "pause" if PAUSE.search(t) else None
        if manage:
            sid = SIP_ID.search(t)
            return Parsed("SIP_MANAGE", manage=manage, sip_id=sid.group(1) if sid else None)

    if FREE.search(t) and (gold or buyv):
        return Parsed("FREE_REQUEST")

    # --- SIP creation
    if sip_word or (freq and amt and (gold or buyv)):
        if amt is None:
            if EDU_WORDS.search(t):
                return Parsed("GOLD_EDU")
            return Parsed("SIP_NO_AMOUNT", freq=freq)
        od = ORDINAL.search(t)
        anchor_day = int(od.group(1)) if od and 1 <= int(od.group(1)) <= 31 else None
        iso = ISO_DATE.search(t)
        hint = START_HINT.search(t)
        return Parsed("SIP_CREATE", amount=amt, freq=freq, anchor_day=anchor_day,
                      start_hint=iso.group(1) if iso else (hint.group(1) if hint else None))

    # --- "how much gold for N rupees"
    if amt and HOWMUCH.search(t) and (gold or "rupee" in t or "rs" in t or "₹" in t):
        return Parsed("CALC", amount=amt)

    # --- gold vs something else
    if gold and other:
        return Parsed("COMPARE")

    # --- quantity-in-grams request: we sell by rupee amount
    if gm and buyv and not amt:
        return Parsed("GRAM_REQUEST", grams=Decimal(gm.group(1)))
    if gm and buyv and gold:
        return Parsed("GRAM_REQUEST", grams=Decimal(gm.group(1)))

    # --- purchase with an amount
    if amt and gold and buyv and not QUESTION.search(t):
        return Parsed("BUY", amount=amt)
    if amt and not gold and not other and INVEST_WORD.search(t):
        return Parsed("INVEST_AMBIGUOUS", amount=amt)
    if amt and other and buyv:
        return Parsed("OFF_TOPIC")

    if gold and PRICE_WORDS.search(t) and not amt:
        return Parsed("PRICE_QUERY")
    if gold and QUESTION.search(t):
        return Parsed("GOLD_ADVICE")
    if gold and buyv:
        return Parsed("BUY_NO_AMOUNT")
    if gold and len(t.split()) <= 3:
        return Parsed("GOLD_BARE")
    if gold:
        return Parsed("GOLD_EDU")
    if re.search(r"\b(invest\w*|savings?|paisa|paise|money)\b", t):
        return Parsed("UNKNOWN")
    return Parsed("OFF_TOPIC")
