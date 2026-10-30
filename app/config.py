"""Settings are read from the environment on every call so tests can override them."""
import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    db_path: str
    log_path: str
    price_mode: str            # "mock" | "http"
    mock_price_inr: str        # INR per gram, used when price_mode == "mock"
    price_url: Optional[str]   # used when price_mode == "http"; must return {"price_per_gram_inr": ...}
    price_exec_max_age_s: int  # a cached price older than this is NOT good enough to charge money
    price_info_max_age_s: int  # ...but is fine for answering "what's the price" (labelled stale)
    llm_base_url: str
    llm_api_key: Optional[str]
    llm_model: str
    min_paise: int
    max_paise: int
    dup_window_s: int          # keyless duplicate purchase guard
    pending_ttl_s: int         # how long a chat confirmation stays valid
    scheduler_enabled: bool
    scheduler_interval_s: int


def get_settings() -> Settings:
    e = os.getenv
    return Settings(
        db_path=e("DB_PATH", "data/kuber.db"),
        log_path=e("LOG_PATH", "logs/app.jsonl"),
        price_mode=e("PRICE_MODE", "mock"),
        mock_price_inr=e("MOCK_PRICE_INR_PER_GRAM", "12500.00"),
        price_url=e("PRICE_URL") or None,
        price_exec_max_age_s=int(e("PRICE_EXEC_MAX_AGE_S", "60")),
        price_info_max_age_s=int(e("PRICE_INFO_MAX_AGE_S", "1800")),
        llm_base_url=e("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
        llm_api_key=e("LLM_API_KEY") or None,
        llm_model=e("LLM_MODEL", "gemini-3.6-flash"),
        min_paise=int(e("MIN_PURCHASE_INR", "10")) * 100,
        max_paise=int(e("MAX_PURCHASE_INR", "100000")) * 100,
        dup_window_s=int(e("DUP_WINDOW_S", "60")),
        pending_ttl_s=int(e("PENDING_TTL_S", "900")),
        scheduler_enabled=e("SCHEDULER_ENABLED", "1") == "1",
        scheduler_interval_s=int(e("SCHEDULER_INTERVAL_S", "60")),
    )
