"""Structured JSON logs. Every line carries request_id and (when known) user_id,
so `scripts/trace_user.py <user_id>` can rebuild a user's whole journey."""
import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import get_settings

request_id_var: ContextVar = ContextVar("request_id", default=None)
user_id_var: ContextVar = ContextVar("user_id", default=None)
_logger = logging.getLogger("kuber")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": request_id_var.get(),
            "user_id": user_id_var.get(),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging() -> None:
    for h in list(_logger.handlers):
        _logger.removeHandler(h)
        h.close()
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    fmt = JsonFormatter()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    _logger.addHandler(sh)
    path = Path(get_settings().log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(path, maxBytes=20_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    _logger.addHandler(fh)


def set_user(user_id: str) -> None:
    user_id_var.set(user_id)


def log_event(event: str, level: str = "info", **fields) -> None:
    _logger.log(getattr(logging, level.upper()), event, extra={"fields": fields})
