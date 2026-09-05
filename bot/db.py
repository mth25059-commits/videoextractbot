"""
Storage — five tables, and two ways to hold them.

By default that is one SQLite file with WAL on, so the bot and the payment
callback can both write without locking each other out. Set `DATABASE_URL` and the
same five tables live in Postgres instead — which in practice means Supabase, and
which exists for exactly one reason: a VPS is rented, and credits people paid for
should not end when the rental does.

Which one is in use is decided once, in `connect()`, by whether `cfg.database_url`
is set. Everything above this module is written against four functions — `query`,
`one`, `execute`, `scalar` — and one context manager, `transaction`, and none of
them change shape between the two. No caller knows which database it is talking
to, and that is deliberate: forty-odd call sites of raw SQL are a thing you port
once, in here, or forever, out there.

The three places the two genuinely differ:

  * **Placeholders.** SQLite writes `?`, Postgres writes `%s`. Every SQL string in
    this bot is written `?` and rewritten on the way out — see `_pg_sql`.
  * **Types.** `SCHEMA` and `SCHEMA_PG` are the same five tables twice, because
    Postgres's `REAL` is *single* precision (credits need double) and its `INTEGER`
    is 32-bit — a Telegram id is bigger than that, and so is a 2 GB file. They have
    to be kept in step by hand, so `tests/test_db_pg.py` compares them column by
    column: a drift is a failing test rather than a missing column at 2am.
  * **Rows.** SQLite hands back `sqlite3.Row`, Postgres a plain `dict`. Both are
    read `row["name"]`, which is why the call sites did not move; only `scalar()`
    reads by position, and only it had to be told the difference.

Credits are stored as double-precision floats because the price list has
half-credits in it (1080p costs 1.5). Every change to a balance goes through
`credits.py`, which writes a ledger row in the same transaction — so a balance can
always be explained by adding up its history.

One writer at a time is assumed, and true: this module serializes every statement
behind `_lock`, and the bot is the only process that touches the database (paysvc
is Node and owns nothing but its own JSON journal). Two bots pointed at one
Postgres would race in `credits._move`, which reads a balance and writes it back —
don't. Two *hosts*, one after the other, is the case the Postgres path is for, and
is fine.

`settings` is the one table nothing else in here writes: it holds prices the admin
has changed since install, and `settings.py` is the only reader. See that module
for why a price cannot live in `cfg`.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Mapping

from .config import cfg

log = logging.getLogger(__name__)

_lock = threading.RLock()
_conn: Any = None

#: A row as every call site reads one: by column name. `sqlite3.Row` and the
#: `dict` psycopg hands back are both this, which is the whole reason the Postgres
#: backend touched no caller. Used as a type hint where `sqlite3.Row` used to be.
Row = Mapping[str, Any]

#: Every table `connect()` guarantees. The setup wizard checks this list against a
#: Supabase project before it accepts the connection string, so it is a list and
#: not a comment.
TABLES = ("users", "ledger", "jobs", "orders", "settings")

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

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""

#: The same five tables for Postgres. Three type changes, and every one of them is
#: a bug that would only show up in production:
#:
#:   * `INTEGER` -> `BIGINT` for ids, byte counts and timestamps. Postgres's
#:     `INTEGER` stops at 2147483647; Telegram user ids passed that years ago, and
#:     `size_bytes` passes it the first time somebody uploads a 2 GB archive.
#:   * `REAL` -> `DOUBLE PRECISION` for money. Postgres's `REAL` is a 4-byte float
#:     with about 7 digits of precision — enough to make a balance of 12.35 read
#:     back as 12.350000381469727, and this ledger is checked by adding it up.
#:   * `AUTOINCREMENT` -> `BIGSERIAL`, which is the same idea spelled the other way.
#:
#: `banned` and `charged` stay `INTEGER` rather than becoming `BOOLEAN`: the code
#: writes 0 and 1 into them and asks `WHERE banned = 0`, which a Postgres boolean
#: column refuses. Keep this in step with `SCHEMA` above — `tests/test_db_pg.py`
#: compares the two, column by column, without needing a Postgres to do it.
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users (
    user_id      BIGINT PRIMARY KEY,
    first_name   TEXT   NOT NULL DEFAULT '',
    username     TEXT,
    credits      DOUBLE PRECISION NOT NULL DEFAULT 0,
    joined_at    BIGINT NOT NULL,
    last_seen    BIGINT NOT NULL,
    banned       INTEGER NOT NULL DEFAULT 0,
    total_spent  DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_topup  DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ledger (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    delta       DOUBLE PRECISION NOT NULL,
    reason      TEXT   NOT NULL,
    ref         TEXT,
    balance     DOUBLE PRECISION NOT NULL,
    created_at  BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, id DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    kind        TEXT   NOT NULL,
    source      TEXT   NOT NULL DEFAULT '',
    status      TEXT   NOT NULL DEFAULT 'queued',
    file_name   TEXT,
    size_bytes  BIGINT NOT NULL DEFAULT 0,
    quality     TEXT,
    cost        DOUBLE PRECISION NOT NULL DEFAULT 0,
    charged     INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  BIGINT NOT NULL,
    finished_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT   PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    rupees       DOUBLE PRECISION NOT NULL,
    credits      DOUBLE PRECISION NOT NULL,
    amount_paise BIGINT,
    upi_uri      TEXT,
    reference    TEXT,
    status       TEXT   NOT NULL DEFAULT 'pending',
    bank_ref     TEXT,
    created_at   BIGINT NOT NULL,
    paid_at      BIGINT,
    expires_at   BIGINT
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at BIGINT NOT NULL
);
"""

def _sqlite(conn: Any) -> bool:
    """
    Which flavour a connection is.

    Asked of the connection and never of `cfg`, so that a test which hands this
    module a SQLite connection behaves like SQLite even on a machine whose `.env`
    names a Postgres. Every suite in `tests/` does exactly that.
    """
    return isinstance(conn, sqlite3.Connection)


def _first(row: Any) -> Any:
    """The first column of a row, whichever flavour handed it over."""
    if isinstance(row, sqlite3.Row):
        return row[0]
    return next(iter(row.values()), None)


_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _pg_sql(sql: str) -> str:
    """
    A `?`-style statement as psycopg wants it: `%s` placeholders, `%` doubled.

    Quoted text is left alone, because a `?` inside a string literal is a question
    mark and not a parameter — which a bare `str.replace` gets wrong the first time
    anyone writes `SET status = 'paid?'`. `%` is doubled everywhere *including*
    literals: psycopg reads the whole statement for placeholders whenever params
    are passed, and a lone `%` in there is an error rather than a percent sign.
    """
    out: list[str] = []
    at = 0
    for match in _LITERAL.finditer(sql):
        out.append(sql[at:match.start()].replace("%", "%%").replace("?", "%s"))
        out.append(match.group(0).replace("%", "%%"))
        at = match.end()
    out.append(sql[at:].replace("%", "%%").replace("?", "%s"))
    return "".join(out)


def _connect_sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _connect_postgres(url: str) -> Any:
    """
    The Supabase (or any Postgres) connection, with the two traps already dodged.

    Raises `RuntimeError` with something a person can act on: this runs at boot and
    inside the setup wizard, and "psycopg.OperationalError" on a black screen tells
    the operator nothing about which of the four things went wrong.
    """
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:                    # pragma: no cover
        raise RuntimeError(
            "DATABASE_URL is set but psycopg is not installed. Run:\n"
            "    pip install 'psycopg[binary]'\n"
            "(it is in requirements.txt, so `pip install -r requirements.txt` "
            "does it too)"
        ) from exc

    conn = psycopg.connect(url, autocommit=True, row_factory=dict_row,
                           connect_timeout=15, application_name="terabot")
    # Supabase's pooler is pgbouncer in transaction mode: a prepared statement made
    # on one backend is gone by the next statement, and psycopg starts preparing
    # anything it has seen five times. Left on, this bot works for about a minute
    # and then fails on its single most common query.
    conn.prepare_threshold = None
    # autocommit is on because `query`/`one` do not commit, and an uncommitted
    # SELECT would otherwise leave the connection idle-in-transaction for as long
    # as the bot runs — holding a snapshot open against a database somebody else
    # vacuums. Writes are still atomic: `execute` is one statement, and
    # `transaction` opens a real BEGIN.
    try:
        conn.execute(SCHEMA_PG)
    except Exception:
        # A role without CREATE rights, or a project where the schema was pasted in
        # by hand. Neither is fatal if the tables are already there, so say so and
        # let the check below be the judge.
        log.warning("could not apply the schema; checking whether it is already there",
                    exc_info=True)
    absent = missing_tables(conn)
    if absent:
        raise RuntimeError(
            "The database answered, but these tables are not in it: "
            + ", ".join(absent) + ".\n"
            "Open your Supabase project -> SQL Editor -> New query, paste the "
            "contents of supabase.txt, press Run, then start the bot again."
        )
    return conn


def connect() -> Any:
    """
    The one connection, opened on first use.

    Returns a `sqlite3.Connection`, or a psycopg one when `DATABASE_URL` is set.
    Nothing outside this module should care which — all of it is behind the four
    helpers below.
    """
    global _conn
    with _lock:
        if _conn is None:
            url = (cfg.database_url or "").strip()
            if url:
                _conn = _connect_postgres(url)
            else:
                cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
                _conn = _connect_sqlite()
        return _conn


def missing_tables(conn: Any = None) -> list[str]:
    """
    Which of `TABLES` are not there. Empty means the database is ready to be used.

    Takes an open connection so `_connect_postgres` can ask before `connect()` has
    finished, and so the setup wizard can test a connection string it has not
    committed to yet.
    """
    if conn is None:
        conn = connect()
    if _sqlite(conn):
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        have = {row["name"] for row in rows}
    else:
        rows = conn.execute("SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = current_schema()").fetchall()
        have = {row["table_name"] for row in rows}
    return [name for name in TABLES if name not in have]


def describe() -> str:
    """
    Where the credits live, in one line fit for a screen — with no password in it.

    Exists for the same reason `egress.describe` does: this string goes on the
    wizard's review page, into a log line and onto the admin panel, and a Postgres
    URL carries the database password in the middle of it.
    """
    url = (cfg.database_url or "").strip()
    if not url:
        return f"local file — {cfg.db_path}"
    host = re.sub(r"^[a-zA-Z+]+://(?:[^@/]*@)?", "", url).split("/")[0].split("?")[0]
    return f"postgres — {host}" if host else "postgres"


def supabase_sql() -> str:
    """
    The Postgres schema as a file to paste, with the clicks written at the top.

    The setup wizard writes this to `supabase.txt` and reads the numbered lines out
    loud, because "run the migrations" is not an instruction and the person
    installing this has a browser open, not a psql prompt.
    """
    return (
        "-- videoextractbot — database setup\n"
        "--\n"
        "-- 1. Open your project on supabase.com\n"
        "-- 2. Left sidebar -> SQL Editor -> New query\n"
        "-- 3. Paste everything below this line, then press Run\n"
        "-- 4. Go back to the setup wizard and press Enter\n"
        "--\n"
        "-- Running it twice is safe. Every line says IF NOT EXISTS, so nothing is\n"
        "-- dropped and no credit anybody already has is touched.\n"
        + SCHEMA_PG
    )


def now() -> int:
    return int(time.time())


def query(sql: str, params: Iterable[Any] = ()) -> list[Row]:
    with _lock:
        conn = connect()
        if _sqlite(conn):
            return conn.execute(sql, tuple(params)).fetchall()
        return conn.execute(_pg_sql(sql), tuple(params)).fetchall()


def one(sql: str, params: Iterable[Any] = ()) -> Row | None:
    with _lock:
        conn = connect()
        if _sqlite(conn):
            return conn.execute(sql, tuple(params)).fetchone()
        return conn.execute(_pg_sql(sql), tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> Any:
    """One statement, committed. Returns the cursor, which callers read `rowcount` of."""
    with _lock:
        conn = connect()
        if not _sqlite(conn):
            return conn.execute(_pg_sql(sql), tuple(params))      # autocommit
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur


def insert_id(sql: str, params: Iterable[Any] = ()) -> int:
    """
    Run an `INSERT … RETURNING id` and give back that id.

    `cur.lastrowid` would do it in SQLite and does not exist in Postgres.
    `RETURNING` is in both — SQLite has had it since 3.35 and Ubuntu ships far
    newer — so this is one code path rather than a branch.

    The row is read *before* the commit on purpose: SQLite refuses to commit with a
    statement still mid-flight and raises "cannot commit transaction - SQL
    statements in progress", which is a bug that only appears once there is a row
    to return.
    """
    with _lock:
        conn = connect()
        is_lite = _sqlite(conn)
        cur = conn.execute(sql, tuple(params)) if is_lite \
            else conn.execute(_pg_sql(sql), tuple(params))
        row = cur.fetchone()
        if is_lite:
            conn.commit()
    if row is None:
        raise RuntimeError(f"insert returned no id: {sql.strip()[:60]}…")
    return int(_first(row))


def scalar(sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
    row = one(sql, params)
    if row is None:
        return default
    value = _first(row)
    return default if value is None else value


class _PgTx:
    """
    What a `with db.transaction() as conn:` block is handed when the database is
    Postgres: the connection, with `?` still working.

    It exists so that `credits._move`, `credits.grant_in` and `payments.settle` —
    all of which take a connection and write `?` SQL into it — did not have to be
    touched or made flavour-aware. All they use of a connection is `.execute()`,
    and all they use of what that returns is `.fetchone()` and `.rowcount`, which
    the psycopg cursor already has.
    """

    def __init__(self, conn: Any):
        self._conn = conn

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Any:
        return self._conn.execute(_pg_sql(sql), tuple(params))


class transaction:
    """`with db.transaction() as conn:` — commits on clean exit, rolls back on error."""

    def __enter__(self) -> Any:
        _lock.acquire()
        try:
            self.conn = connect()
            if _sqlite(self.conn):
                # IMMEDIATE takes the write lock now instead of upgrading to it
                # halfway through, which is where SQLITE_BUSY comes from.
                self.conn.execute("BEGIN IMMEDIATE")
                return self.conn
            self.conn.execute("BEGIN")
            return _PgTx(self.conn)
        except BaseException:
            # A database that will not open must not also leave the lock held —
            # that turns one failed statement into a bot that answers nothing.
            _lock.release()
            raise

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            _lock.release()
        return False

