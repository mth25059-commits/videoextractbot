"""
The Postgres backend: one bot's worth of SQL, two dialects.

`DATABASE_URL` decides where credits live. Empty is a SQLite file on this box;
filled in, it is Postgres — in practice Supabase — and the reason to want that is
not tidiness, it is that a VPS is rented and the credits people paid for should not
end when the rental does.

Which makes this file's job narrow and worth being fussy about: **the same SQL has
to mean the same thing to both databases.** Everything above `bot/db.py` is written
once, with `?` placeholders, and forty-odd call sites do not know which database
answered them. So what can break is not a feature — it is a dialect, silently, on
the one install that chose the other option:

- **Placeholders.** `?` is SQLite's, `%s` is psycopg's. The rewrite is one function
  and a `str.replace` version of it is wrong the first time a statement contains a
  quoted question mark, so `_pg_sql` gets the awkward strings rather than the easy
  one.
- **Types.** Postgres's `REAL` is a *four-byte* float: a balance of 12.35 read back
  as 12.350000381469727, in a ledger that is checked by adding it up. Its `INTEGER`
  stops at 2147483647, which every Telegram user id passed years ago and which
  `size_bytes` passes the first time somebody uploads a 2 GB archive. `SCHEMA` and
  `SCHEMA_PG` are therefore the same five tables written twice — so this file
  compares them column by column, and does it by *parsing* both rather than by
  trusting a comment. A column added to one and forgotten in the other is a failing
  test here instead of a missing column at 2am.
- **Two statements that are only nearly portable**, both found by reading rather
  than by breaking: `MAX(0, x)` is a scalar function in SQLite and an *aggregate* in
  Postgres, and `cur.lastrowid` does not exist there at all. Both were rewritten;
  both are asserted here.

The Postgres code path is exercised **without a Postgres**, by handing `db` a
connection object that simply is not a `sqlite3.Connection`. That is not a
simulation of the real thing — part D does that, and skips unless `DATABASE_URL` is
set — but it does prove the thing most likely to be wrong: that every helper
rewrites its placeholders, that a `dict` row is read correctly where a `sqlite3.Row`
used to be, and that the lock is not left held when something fails.

Run: python tests/test_db_pg.py
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # pragma: no cover
    pass

os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "111222333")

from bot import db                                    # noqa: E402
from bot.config import cfg                            # noqa: E402

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")


# --- reading the two schemas -------------------------------------------------

_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \(\s*(.*?)\s*\);", re.S)

#: Longest first, so `DOUBLE PRECISION` is not read as a column called PRECISION.
_TYPES = ("DOUBLE PRECISION", "BIGSERIAL", "BIGINT", "INTEGER", "REAL", "TEXT")


def _split_type(defn: str) -> tuple[str, str]:
    """`"BIGINT NOT NULL"` -> `("BIGINT", "NOT NULL")`."""
    for kind in _TYPES:
        if defn == kind or defn.startswith(kind + " "):
            return kind, defn[len(kind):].strip()
    return "", defn


def parse(ddl: str) -> dict[str, dict[str, tuple[str, str]]]:
    """
    `{table: {column: (type, constraints)}}`, in the order they are declared.

    Splitting a table body on commas is safe here and only here: no column in either
    schema has a comma inside it — no `NUMERIC(10,2)`, no `DEFAULT 'a,b'`. If one
    ever does, this parser is the thing to fix rather than to work around.
    """
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for name, body in _TABLE.findall(ddl):
        cols: dict[str, tuple[str, str]] = {}
        for line in body.split(","):
            line = " ".join(line.split())
            if not line:
                continue
            col, _, rest = line.partition(" ")
            cols[col] = _split_type(rest)
        out[name] = cols
    return out


def indexes(ddl: str) -> list[str]:
    return [" ".join(line.split()) for line in ddl.splitlines()
            if line.strip().upper().startswith("CREATE INDEX")]


LITE = parse(db.SCHEMA)
PG = parse(db.SCHEMA_PG)

#: Columns that hold something bigger than a 32-bit signed integer, or will. Telegram
#: ids passed 2^31 years ago — every account made since about 2021 has one — and
#: `size_bytes` gets there on a 2 GB upload, and every `*_at` is a Unix timestamp,
#: which runs out in 2038.
MUST_BE_64BIT = {
    "user_id", "size_bytes", "amount_paise",
    "joined_at", "last_seen", "created_at", "finished_at", "paid_at", "expires_at",
    "updated_at",
    # `deletions`: a chat id is a Telegram id and has the same range as a user id
    # (a channel's is `-100` followed by ten digits); `job_id` points at
    # `jobs.id`, which is a BIGSERIAL over there; `due_at` is another Unix timestamp.
    #
    # `deletions.message_id` is deliberately *not* in here. A message id is a
    # per-chat counter in the millions and will not see 2^31 — and this set is only
    # worth having while it means what it says.
    "chat_id", "job_id", "due_at",
}

#: Columns the code writes 0 and 1 into and then asks `WHERE banned = 0` about. A
#: Postgres `BOOLEAN` refuses that comparison, so these two stay integers in both.
FLAGS = {"banned", "charged"}

# --- a connection that is not SQLite -----------------------------------------

class FakeCursor:
    def __init__(self, rows, rowcount=1):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakePg:
    """
    Whatever `db` does when the connection is not a `sqlite3.Connection`.

    `db` decides which dialect it is speaking by asking the connection and never by
    asking `cfg` — which is what lets nine test files hand it a SQLite connection on
    a machine whose `.env` names a Postgres, and what lets this class stand in for
    psycopg without psycopg being installed. All `db` uses of a real one is
    `.execute()`, `.commit()` and `.rollback()`; all it uses of a cursor is
    `.fetchone()`, `.fetchall()` and `.rowcount`.
    """

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.seen: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.seen.append((sql, tuple(params)))
        return FakeCursor(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    @property
    def sql(self) -> str:
        """The last statement it was given."""
        return self.seen[-1][0] if self.seen else ""


def with_fake(rows=()) -> FakePg:
    """Make a fake connection the one `db` is holding, and hand it back."""
    fake = FakePg(rows)
    db._conn = fake
    return fake


def lock_is_free() -> bool:
    """
    True when `db._lock` is not held — asked after every failure path.

    A lock left held by a raising `__enter__` is not a slow bot, it is a bot that
    answers nothing at all, ever again, and never says why.
    """
    if db._lock.acquire(blocking=False):
        db._lock.release()
        return True
    return False


def part_a_placeholders():
    print("\n— A. `?` on the way out becomes `%s`, and nothing else moves —")
    check("the commonest statement in the bot",
          db._pg_sql("SELECT credits FROM users WHERE user_id = ?"),
          "SELECT credits FROM users WHERE user_id = %s")

    # The refund that stopped using MAX(0, x). Three parameters, and it has to stay
    # three: the amount appears twice because CASE cannot reuse a placeholder.
    refund = ("UPDATE users SET total_spent = CASE WHEN total_spent < ? THEN 0 "
              "ELSE total_spent - ? END WHERE user_id = ?")
    check("every parameter is rewritten, none are missed",
          db._pg_sql(refund).count("%s"), 3)
    check("and none are left behind", "?" in db._pg_sql(refund), False)
    check("the CASE survives it intact", "CASE WHEN total_spent < %s THEN 0"
          in db._pg_sql(refund), True)

    # This is the case a `sql.replace("?", "%s")` gets wrong, which is why it is a
    # scanner instead. No statement in the bot has a quoted question mark today; one
    # written next month would be a corrupted error message rather than a crash.
    quoted = "UPDATE jobs SET error = 'gone?' WHERE id = ?"
    check("a question mark inside quotes is a question mark",
          db._pg_sql(quoted), "UPDATE jobs SET error = 'gone?' WHERE id = %s")
    doubled = "UPDATE users SET first_name = 'it''s ?' WHERE user_id = ?"
    check("an escaped quote does not end the literal early",
          db._pg_sql(doubled), "UPDATE users SET first_name = 'it''s ?' WHERE user_id = %s")

    # psycopg reads the whole statement for placeholders whenever parameters are
    # passed, so a percent sign it did not put there is an error and not a percent.
    check("a percent sign is doubled so psycopg leaves it alone",
          db._pg_sql("SELECT * FROM users WHERE first_name LIKE '%r%'"),
          "SELECT * FROM users WHERE first_name LIKE '%%r%%'")
    check("and it is doubled outside a literal too",
          db._pg_sql("SELECT 100 % ? AS x"), "SELECT 100 %% %s AS x")

    # The two real statements the port actually changed.
    insert = ("INSERT INTO jobs (user_id, kind, source, status, quality, cost, "
              "charged, created_at) VALUES (?, ?, ?, 'queued', ?, ?, 1, ?) RETURNING id")
    check("the job insert keeps all six parameters", db._pg_sql(insert).count("%s"), 6)
    check("and still returns the id", db._pg_sql(insert).endswith("RETURNING id"), True)
    upsert = ("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
              "updated_at = excluded.updated_at")
    check("the price upsert is portable as written", db._pg_sql(upsert).count("%s"), 3)
    check("its ON CONFLICT is untouched",
          "ON CONFLICT(key) DO UPDATE SET value = excluded.value" in db._pg_sql(upsert), True)


def part_b_schema_parity():
    print("\n— B. the two schemas are the same five tables —")
    check("same tables, same order", list(PG), list(LITE))
    check("and that is what `db.TABLES` says they are", set(db.TABLES), set(LITE))

    for table in LITE:
        check(f"{table}: same columns, same order", list(PG.get(table, {})), list(LITE[table]))

    bad_type: list[str] = []
    bad_constraint: list[str] = []
    for table, cols in LITE.items():
        for col, (kind, rest) in cols.items():
            pg_kind, pg_rest = PG.get(table, {}).get(col, ("", ""))
            want = None
            if "AUTOINCREMENT" in rest:
                want = "BIGSERIAL"
            elif kind == "REAL":
                want = "DOUBLE PRECISION"
            elif kind == "INTEGER":
                want = "INTEGER" if col in FLAGS else \
                    ("BIGINT" if col in MUST_BE_64BIT else "INTEGER")
            else:
                want = kind
            if pg_kind != want:
                bad_type.append(f"{table}.{col}: {kind} -> {pg_kind}, wanted {want}")
            # Constraints have to match exactly; AUTOINCREMENT is the one word that is
            # spelled by the type in Postgres rather than after it.
            if rest.replace("AUTOINCREMENT", "").strip() != pg_rest.strip():
                bad_constraint.append(f"{table}.{col}: {rest!r} vs {pg_rest!r}")
    check("every money column is double precision and every id is 64-bit", bad_type, [])
    check("NOT NULL, DEFAULT and PRIMARY KEY match column for column", bad_constraint, [])

    check("no four-byte REAL anywhere in the Postgres schema",
          re.search(r"\bREAL\b", db.SCHEMA_PG) is None, True)
    check("no AUTOINCREMENT either, it is spelled BIGSERIAL there",
          "AUTOINCREMENT" in db.SCHEMA_PG, False)
    check("and the SQLite schema is left as it was",
          "BIGSERIAL" in db.SCHEMA or "DOUBLE PRECISION" in db.SCHEMA, False)
    check("the indexes are dialect-neutral, so both have the same ones",
          indexes(db.SCHEMA_PG), indexes(db.SCHEMA))
    check("nothing is dropped or replaced by either schema",
          ("DROP" in db.SCHEMA_PG.upper()) or ("DROP" in db.SCHEMA.upper()), False)
    check("both are safe to run twice",
          db.SCHEMA_PG.count("IF NOT EXISTS"), db.SCHEMA.count("IF NOT EXISTS"))


def part_c_the_postgres_path():
    print("\n— C. the Postgres path, with no Postgres —")
    fake = with_fake([{"credits": 7.5}])
    check("`one` rewrites before it asks",
          db.one("SELECT credits FROM users WHERE user_id = ?", (5,)) is not None, True)
    check("and what reached the driver has %s in it",
          fake.sql, "SELECT credits FROM users WHERE user_id = %s")
    check("with the parameters kept as a tuple", fake.seen[-1][1], (5,))

    # The one place in the whole bot that reads a column by position. A `sqlite3.Row`
    # answers row[0]; a dict raises KeyError on it, which is why `_first` exists.
    with_fake([{"credits": 7.5}])
    check("`scalar` reads the first value of a dict row",
          db.scalar("SELECT credits FROM users WHERE user_id = ?", (5,)), 7.5)
    with_fake([])
    check("no row at all falls back to the default",
          db.scalar("SELECT credits FROM users WHERE user_id = ?", (5,), 0.0), 0.0)
    with_fake([{"total": None}])
    check("and so does a row whose value is NULL — SUM over nothing",
          db.scalar("SELECT SUM(size_bytes) FROM jobs", (), 0), 0)

    fake = with_fake([{"user_id": 5}])
    check("`query` returns every row", len(db.query("SELECT * FROM users")), 1)

    fake = with_fake([{"id": 42}])
    check("`insert_id` gives back the id Postgres returned",
          db.insert_id("INSERT INTO jobs (user_id) VALUES (?) RETURNING id", (5,)), 42)
    check("it does not commit — the connection is already autocommit",
          fake.commits, 0)
    fake = with_fake([])
    try:
        db.insert_id("INSERT INTO jobs (user_id) VALUES (?) RETURNING id", (5,))
        check("an insert that returns nothing is an error, not a silent 0", False, True)
    except RuntimeError:
        check("an insert that returns nothing is an error, not a silent 0", True, True)

    fake = with_fake([{"id": 1}])
    db.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (1,))
    check("`execute` rewrites too", fake.sql,
          "UPDATE jobs SET status = 'done' WHERE id = %s")
    check("and leaves committing to autocommit", fake.commits, 0)

    print("\n— C2. transactions —")
    fake = with_fake([{"credits": 1.0}])
    with db.transaction() as conn:
        check("a Postgres transaction opens with a plain BEGIN", fake.seen[0][0], "BEGIN")
        check("what the block gets is the adapter, not the raw connection",
              type(conn).__name__, "_PgTx")
        conn.execute("UPDATE users SET credits = ? WHERE user_id = ?", (2.0, 5))
        check("whose `execute` rewrites as well — this is `credits._move`'s connection",
              fake.sql, "UPDATE users SET credits = %s WHERE user_id = %s")
    check("a clean exit commits once", fake.commits, 1)
    check("and lets the next caller in", lock_is_free(), True)

    fake = with_fake([])
    try:
        with db.transaction():
            raise ValueError("the download died")
    except ValueError:
        pass
    check("a raising block rolls back", fake.rollbacks, 1)
    check("commits nothing", fake.commits, 0)
    check("and still releases the lock", lock_is_free(), True)

    # The failure that used to be permanent: `connect()` raising inside `__enter__`
    # left the lock held, so the bot did not answer the next message either.
    real_connect = db.connect
    db.connect = lambda: (_ for _ in ()).throw(RuntimeError("supabase is not answering"))
    try:
        with db.transaction():
            check("a database that will not open should not get this far", False, True)
    except RuntimeError:
        check("a database that will not open raises", True, True)
    finally:
        db.connect = real_connect
    check("and does not take the lock with it", lock_is_free(), True)

    print("\n— C3. what the wizard asks a fresh project —")
    fake = with_fake([{"table_name": "users"}, {"table_name": "ledger"}])
    check("`missing_tables` names the ones that are not there",
          db.missing_tables(fake), ["jobs", "orders", "settings", "deletions"])
    fake = with_fake([{"table_name": name} for name in db.TABLES])
    check("and says nothing when the schema has been pasted in",
          db.missing_tables(fake), [])
    check("it asks the right catalogue for a Postgres",
          "information_schema.tables" in fake.sql, True)

    print("\n— C4. the connection string never reaches a screen —")
    url = ("postgresql://postgres.abcdefghijkl:hunter2-the-real-one"
           "@aws-0-ap-south-1.pooler.supabase.com:6543/postgres")
    object.__setattr__(cfg, "database_url", url)
    try:
        shown = db.describe()
        check("the password is not in what gets printed", "hunter2" in shown, False)
        check("nor is the project's own login", "postgres.abcdefghijkl" in shown, False)
        check("but the host and port are, so it can be recognised",
              "aws-0-ap-south-1.pooler.supabase.com:6543" in shown, True)
        check("and it says which kind it is", shown.startswith("postgres —"), True)
    finally:
        object.__setattr__(cfg, "database_url", "")
    check("with nothing set it points at the local file",
          str(cfg.db_path) in db.describe(), True)

    print("\n— C5. supabase.txt is a thing a person can follow —")
    text = db.supabase_sql()
    check("it opens with numbered clicks", "1. Open your project" in text, True)
    check("it names the button to press", "SQL Editor" in text, True)
    check("it carries the whole Postgres schema", db.SCHEMA_PG in text, True)
    check("it says running it twice is safe", "twice is safe" in text, True)
    check("and every table is in it",
          [t for t in db.TABLES if f"CREATE TABLE IF NOT EXISTS {t} " not in text], [])


def part_d_live():
    """
    The same route against a real Postgres. Skipped unless `DATABASE_URL` is set.

    This is the only part that can prove the sentence the whole option exists for —
    *credits survive the box* — because it is the only part where a second process
    could come along and read them. It runs on the VPS, and it runs against whatever
    `DATABASE_URL` names, so it works on one made-up negative user id and deletes its
    own rows on the way out. Nothing real is touched: no Telegram id is negative.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("\n— D. against a real Postgres — skipped (DATABASE_URL is not set)")
        print("     Set it and run this again on the box to check the live round trip.")
        return

    print(f"\n— D. against a real Postgres — {db.describe()} —")
    from bot import credits, queue                    # noqa: PLC0415

    db._conn = None
    object.__setattr__(cfg, "database_url", url)
    uid = -999001
    try:
        conn = db.connect()
        check("it connected and every table is there", db.missing_tables(conn), [])
        check("prepared statements are off, or the pooler drops us in a minute",
              getattr(conn, "prepare_threshold", "unset"), None)

        db.execute("DELETE FROM ledger WHERE user_id = ?", (uid,))
        db.execute("DELETE FROM jobs WHERE user_id = ?", (uid,))
        db.execute("DELETE FROM users WHERE user_id = ?", (uid,))

        user, is_new = credits.ensure(uid, "Test", None)
        check("a user can be created", is_new, True)
        check("and read back by name, not by position", user.user_id, uid)

        credits.grant(uid, 10, "test top-up", ref="test", is_topup=True)
        check("credits go in", credits.balance(uid), 10.0)
        credits.charge(uid, 1.5, "test 1080p")
        check("and come out at half-credit precision — not a four-byte float",
              credits.balance(uid), 8.5)

        # The statement that used to say MAX(0, total_spent - ?), which Postgres reads
        # as an aggregate and refuses. A refund bigger than the lifetime spend is the
        # case the floor is for.
        credits.refund(uid, 5, "test refund")
        check("a refund larger than the spend floors it at zero rather than failing",
              float(db.scalar("SELECT total_spent FROM users WHERE user_id = ?", (uid,))), 0.0)
        check("and the credits are back", credits.balance(uid), 13.5)
        check("every movement left a ledger row", len(credits.history(uid, 10)), 4)

        # The statement that used to read `cur.lastrowid`.
        job = queue.Job(user_id=uid, kind="fap", source="https://example.invalid/x",
                        quality="720p", cost=1.5)
        row_id = queue._insert_row(job)
        check("an insert hands back its new id", isinstance(row_id, int) and row_id > 0, True)
        second = queue._insert_row(job)
        check("and a different one next time", second != row_id, True)
        check("the row is really there",
              db.one("SELECT quality FROM jobs WHERE id = ?", (row_id,))["quality"], "720p")

        # A 2 GB file, which is what `size_bytes` is for and what a 32-bit column
        # would have refused.
        big = 2_147_483_648
        db.execute("UPDATE jobs SET size_bytes = ? WHERE id = ?", (big, row_id))
        check("a 2 GB size fits",
              int(db.scalar("SELECT size_bytes FROM jobs WHERE id = ?", (row_id,))), big)
    finally:
        try:
            db.execute("DELETE FROM ledger WHERE user_id = ?", (uid,))
            db.execute("DELETE FROM jobs WHERE user_id = ?", (uid,))
            db.execute("DELETE FROM users WHERE user_id = ?", (uid,))
        except Exception as exc:                       # pragma: no cover
            print(f"  !! could not clean up user {uid}: {exc}")
        object.__setattr__(cfg, "database_url", "")
        db._conn = None


def main():
    part_a_placeholders()
    part_b_schema_parity()
    part_c_the_postgres_path()
    part_d_live()
    db._conn = None
    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())




