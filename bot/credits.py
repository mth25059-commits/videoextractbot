"""
Credits — the only module allowed to change a balance.

Two rules the rest of the bot depends on:

  1. Nothing is charged up front. A job *reserves* credits when it is accepted
     and is only *charged* once the file has actually landed in the user's chat.
     If the download dies, the reservation is released and the user keeps the
     credit. Charging on submit is how a paid bot collects refund requests.

  2. Every movement writes a ledger row inside the same transaction as the
     balance update, so a balance can always be reconstructed from its history.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db
from .config import cfg


@dataclass
class User:
    user_id: int
    first_name: str
    username: str | None
    credits: float
    joined_at: int
    last_seen: int
    banned: bool
    total_spent: float
    total_topup: float

    @property
    def handle(self) -> str:
        return f"@{self.username}" if self.username else f"id{self.user_id}"


def _row_to_user(row) -> User:
    return User(
        user_id=row["user_id"],
        first_name=row["first_name"],
        username=row["username"],
        credits=float(row["credits"]),
        joined_at=row["joined_at"],
        last_seen=row["last_seen"],
        banned=bool(row["banned"]),
        total_spent=float(row["total_spent"]),
        total_topup=float(row["total_topup"]),
    )


def get(user_id: int) -> User | None:
    row = db.one("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return _row_to_user(row) if row else None


def ensure(user_id: int, first_name: str, username: str | None) -> tuple[User, bool]:
    """Fetch or create a user. Returns (user, is_new). New users get the joining bonus."""
    ts = db.now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET first_name = ?, username = ?, last_seen = ? WHERE user_id = ?",
                (first_name or row["first_name"], username, ts, user_id),
            )
            fresh = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return _row_to_user(fresh), False

        bonus = float(cfg.free_credits_on_join)
        conn.execute(
            """INSERT INTO users (user_id, first_name, username, credits, joined_at, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, first_name or "", username, bonus, ts, ts),
        )
        if bonus:
            conn.execute(
                """INSERT INTO ledger (user_id, delta, reason, ref, balance, created_at)
                   VALUES (?, ?, 'joining bonus', NULL, ?, ?)""",
                (user_id, bonus, bonus, ts),
            )
        fresh = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return _row_to_user(fresh), True


def balance(user_id: int) -> float:
    return float(db.scalar("SELECT credits FROM users WHERE user_id = ?", (user_id,), 0.0))


def _move(conn, user_id: int, delta: float, reason: str, ref: str | None) -> float:
    """Apply a signed change and log it. Caller owns the transaction."""
    row = conn.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such user: {user_id}")
    new_balance = round(float(row["credits"]) + delta, 2)
    if new_balance < 0:
        raise InsufficientCredits(needed=-delta, available=float(row["credits"]))
    conn.execute("UPDATE users SET credits = ? WHERE user_id = ?", (new_balance, user_id))
    conn.execute(
        """INSERT INTO ledger (user_id, delta, reason, ref, balance, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, delta, reason, ref, new_balance, db.now()),
    )
    return new_balance


class InsufficientCredits(Exception):
    def __init__(self, needed: float, available: float):
        self.needed = needed
        self.available = available
        super().__init__(f"needs {needed:g} credits, has {available:g}")


def charge(user_id: int, amount: float, reason: str, ref: str | None = None) -> float:
    """Take credits away. Raises InsufficientCredits rather than going negative."""
    if amount <= 0:
        return balance(user_id)
    with db.transaction() as conn:
        new_balance = _move(conn, user_id, -abs(amount), reason, ref)
        conn.execute(
            "UPDATE users SET total_spent = total_spent + ? WHERE user_id = ?",
            (abs(amount), user_id),
        )
        return new_balance


def refund(user_id: int, amount: float, reason: str, ref: str | None = None) -> float:
    """Give credits back after a failed job. Also un-counts them from total_spent."""
    if amount <= 0:
        return balance(user_id)
    with db.transaction() as conn:
        new_balance = _move(conn, user_id, abs(amount), reason, ref)
        conn.execute(
            "UPDATE users SET total_spent = MAX(0, total_spent - ?) WHERE user_id = ?",
            (abs(amount), user_id),
        )
        return new_balance


def grant(user_id: int, amount: float, reason: str, ref: str | None = None,
          is_topup: bool = False) -> float:
    """Admin gift or a settled payment. `is_topup` counts it toward total_topup."""
    with db.transaction() as conn:
        return grant_in(conn, user_id, amount, reason, ref, is_topup)


def grant_in(conn, user_id: int, amount: float, reason: str, ref: str | None = None,
             is_topup: bool = False) -> float:
    """
    `grant`, but inside a transaction the caller already opened.

    A top-up has to mark the order paid and add the credits atomically — if those
    were two transactions, a crash between them either pays for nothing or credits
    twice. SQLite has no nested BEGIN, so the caller owns the transaction and
    passes its connection in here.
    """
    new_balance = _move(conn, user_id, abs(amount), reason, ref)
    if is_topup:
        conn.execute(
            "UPDATE users SET total_topup = total_topup + ? WHERE user_id = ?",
            (abs(amount), user_id),
        )
    return new_balance


def can_afford(user_id: int, amount: float) -> bool:
    return balance(user_id) + 1e-9 >= amount


def history(user_id: int, limit: int = 10) -> list[db.sqlite3.Row]:
    return db.query(
        "SELECT * FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
