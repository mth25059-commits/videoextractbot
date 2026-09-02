"""
Storage — one SQLite file, four tables, WAL mode so the bot and the payment
callback can both write without locking each other out.

Credits are stored as REAL because the price list has half-credits in it
(1080p costs 1.5). Every change to a balance goes through `credits.py`, which
writes a ledger row in the same transaction — so a balance can always be
explained by adding up its history.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Iterable

from .config import cfg

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    first_name   TEXT    NOT NULL DEFAULT '',
    username     TEXT,
    credits      REAL    NOT NULL DEFAULT 0,
    joined_at    INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    banned       INTEGER NOT NULL DEFAULT 0,
    total_spent  REAL    NOT NULL DEFAULT 0,
    total_topup  REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    delta       REAL    NOT NULL,
    reason      TEXT    NOT NULL,
    ref         TEXT,
    balance     REAL    NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, id DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'queued',
    file_name   TEXT,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    quality     TEXT,
    cost        REAL    NOT NULL DEFAULT 0,
    charged     INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT    PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    rupees       REAL    NOT NULL,
    credits      REAL    NOT NULL,
    amount_paise INTEGER,
    upi_uri      TEXT,
    reference    TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',
    bank_ref     TEXT,
    created_at   INTEGER NOT NULL,
    paid_at      INTEGER,
    expires_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(cfg.db_path, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def now() -> int:
    return int(time.time())


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, tuple(params)).fetchall()


def one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    with _lock:
        return connect().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur


def scalar(sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
    row = one(sql, params)
    if row is None or row[0] is None:
        return default
    return row[0]


class transaction:
    """`with db.transaction() as conn:` — commits on clean exit, rolls back on error."""

    def __enter__(self) -> sqlite3.Connection:
        _lock.acquire()
        self.conn = connect()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            _lock.release()
        return False
