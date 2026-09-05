"""
Per-user in-flight state.

Telegram callback data is capped at 64 bytes, so a URL can never travel inside a
button. Instead a pending action is parked here under a short random token and
the button only carries the token.

Deliberately in memory, not the database: these are half-finished interactions.
A restart should drop them, not resurrect a payment screen whose QR has expired.
Entries older than TTL are swept on every access so a busy day cannot grow this
without bound.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

TTL_SECONDS = 30 * 60

Mode = Literal["terabox", "zip", "await_links", "await_amount", "await_zip_password",
               "await_fap_link", "admin_give_user", "admin_give_amount"]


@dataclass
class Pending:
    """One parked interaction — a batch waiting for confirmation, a prompt, a quality pick."""
    token: str
    user_id: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def age(self) -> float:
        return time.time() - self.created_at

    @property
    def expired(self) -> bool:
        return self.age > TTL_SECONDS


_pending: dict[str, Pending] = {}
_mode: dict[int, tuple[str, dict[str, Any], float]] = {}
_cancelled: set[str] = set()


def _sweep() -> None:
    dead = [t for t, p in _pending.items() if p.expired]
    for token in dead:
        _pending.pop(token, None)
        _cancelled.discard(token)
    cutoff = time.time() - TTL_SECONDS
    for user_id in [u for u, (_, _, ts) in _mode.items() if ts < cutoff]:
        _mode.pop(user_id, None)


def park(user_id: int, kind: str, **payload: Any) -> Pending:
    """Store a pending action and return it. `.token` is what goes in the button."""
    _sweep()
    token = secrets.token_urlsafe(8)
    entry = Pending(token=token, user_id=user_id, kind=kind, payload=payload)
    _pending[token] = entry
    return entry


def take(token: str, user_id: int | None = None) -> Pending | None:
    """Fetch and remove. Returns None if unknown, expired, or owned by someone else."""
    _sweep()
    entry = _pending.get(token)
    if entry is None or entry.expired:
        return None
    if user_id is not None and entry.user_id != user_id:
        return None
    _pending.pop(token, None)
    return entry


def peek(token: str, user_id: int | None = None) -> Pending | None:
    """Same checks as `take`, but leaves the entry in place."""
    _sweep()
    entry = _pending.get(token)
    if entry is None or entry.expired:
        return None
    if user_id is not None and entry.user_id != user_id:
        return None
    return entry


# --- "what is this user in the middle of typing?" ----------------------------

def set_mode(user_id: int, mode: str, **payload: Any) -> None:
    _mode[user_id] = (mode, payload, time.time())


def get_mode(user_id: int) -> tuple[str, dict[str, Any]] | None:
    _sweep()
    entry = _mode.get(user_id)
    if entry is None:
        return None
    mode, payload, _ = entry
    return mode, payload


def clear_mode(user_id: int) -> None:
    _mode.pop(user_id, None)


# --- cancellation -----------------------------------------------------------

def cancel(token: str) -> None:
    """Flag a running job. Workers poll `is_cancelled` between chunks."""
    _cancelled.add(token)


def is_cancelled(token: str) -> bool:
    return token in _cancelled


def clear_cancel(token: str) -> None:
    _cancelled.discard(token)


def stats() -> dict[str, int]:
    _sweep()
    return {"pending": len(_pending), "in_mode": len(_mode), "cancelled": len(_cancelled)}
