import contextlib
import os
import sqlite3

from . import clock
from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  user_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);

-- The ledger. One row per executed purchase (one-time or SIP instalment). Append-only.
CREATE TABLE IF NOT EXISTS transactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  txn_id TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL REFERENCES users(user_id),
  kind TEXT NOT NULL CHECK(kind IN ('one_time','sip')),
  amount_paise INTEGER NOT NULL CHECK(amount_paise > 0),
  price_paise_per_gram INTEGER NOT NULL CHECK(price_paise_per_gram > 0),
  gold_units INTEGER NOT NULL CHECK(gold_units >= 0),
  price_stale INTEGER NOT NULL DEFAULT 0,
  sip_id TEXT, sip_idx INTEGER,
  idempotency_key TEXT NOT NULL,
  request_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(user_id, idempotency_key),
  UNIQUE(sip_id, sip_idx));   -- DB-level guarantee: one instalment can never be charged twice
CREATE INDEX IF NOT EXISTS idx_txn_user ON transactions(user_id, id);

CREATE TABLE IF NOT EXISTS sips(
  sip_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(user_id),
  amount_paise INTEGER NOT NULL,
  frequency TEXT NOT NULL CHECK(frequency IN ('daily','weekly','monthly')),
  anchor_date TEXT NOT NULL,
  anchor_day INTEGER NOT NULL,
  next_idx INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','PAUSED','CANCELLED')),
  nat_key TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sip_user ON sips(user_id);

CREATE TABLE IF NOT EXISTS pending_actions(
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_pending_user ON pending_actions(user_id, status);

CREATE TABLE IF NOT EXISTS price_cache(
  id INTEGER PRIMARY KEY CHECK(id = 1), price_paise INTEGER NOT NULL, fetched_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS chat_idem(
  user_id TEXT NOT NULL, idem_key TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(user_id, idem_key));
"""


def connect() -> sqlite3.Connection:
    path = get_settings().db_path
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextlib.contextmanager
def tx():
    """BEGIN IMMEDIATE = take the write lock up front, so check-then-insert is race-free."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def ensure_user(user_id: str) -> None:
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id, created_at) VALUES(?,?)",
                  (user_id, clock.now().isoformat()))
