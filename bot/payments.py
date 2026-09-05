"""
Top-ups: orders, the paysvc client, and the one function allowed to settle one.

    pay:open ──► amount ──► POST /order ──► QR on screen
                                              │
                          bank alert ──► paysvc ──► POST /paid ──► settle()
                                              │                      │
                          "I have paid" ──► POST /check ─────────────┘

`settle()` is the only way an order becomes paid, and it is called from both
directions — the push from paysvc and the pull from the user's button (and from
the boot-time reconcile). So the two things it must be are **atomic** and
**idempotent**: the order row is flipped to `paid` and the credits are added in
one SQLite transaction, guarded by `status != 'paid'` in the UPDATE itself. Two
callers racing means one of them updates zero rows and grants nothing.

Money is never derived from what a caller sends. The credits granted come from
the `credits` column written when the order was created, not from the rupees in
the callback body — a bug or a forged callback cannot inflate a top-up, only
settle one that already existed at a price the bot set.

No pyrogram in here on purpose: the money path is testable without it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import credits, db, settings
from .config import cfg

log = logging.getLogger(__name__)

TIMEOUT = 20.0          # /check asks paysvc to scan an IMAP inbox before answering


class PaySvcError(RuntimeError):
    """paysvc said no, or could not be reached. The message is shown to the user."""


class NoSuchOrder(PaySvcError):
    """paysvc has no record of this order — a `pending` one, or a pruned journal."""


@dataclass(frozen=True)
class Quote:
    """What the user has to pay, and what they get for it."""
    order_id: str
    user_id: int
    rupees: float
    credits: float
    amount_paise: int
    listed_paise: int
    upi_id: str
    payee: str
    upi_uri: str
    reference: str
    expires_at: int
    auto_confirm: bool
    qr_size: int = 0
    qr_cells: str = ""

    @property
    def amount_rupees(self) -> float:
        return self.amount_paise / 100

    @property
    def discount_paise(self) -> int:
        return max(0, self.listed_paise - self.amount_paise)

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at / 1000 - time.time()))


@dataclass(frozen=True)
class Settlement:
    order_id: str
    user_id: int
    rupees: float
    credits_added: float
    new_balance: float
    amount_paise: int
    bank_ref: str


def credits_for(rupees: float) -> float:
    """
    What a rupee amount buys, at the rate in force right now.

    Multiplies by `credits_per_rupee` rather than dividing by its reciprocal, so this
    is character-for-character the arithmetic `keyboards.topup_presets()` does when it
    prints `₹20 → 30 cr`. Two expressions that are algebraically equal are not equal
    in floating point, and the one place they must never disagree is the button
    promising an amount and the ledger granting it.

    Read through `settings`, so a rate changed from the admin panel applies to the
    very next order rather than to the next restart.
    """
    per_rupee = settings.get("credits_per_rupee") or 1
    return round(rupees * per_rupee, 2)


def new_order_id(user_id: int) -> str:
    """
    Short enough to ride inside callback data, which Telegram caps at 64 bytes.

    `pay:check:` + this is about 35 of them. The user id is in there for whoever
    reads the paysvc journal by hand — nothing trusts it; ownership is always
    re-read from the orders table.
    """
    stamp = format(int(time.time()), "x").upper()
    return f"TB-{user_id}-{stamp}{secrets.token_hex(2).upper()}"


# --- talking to paysvc -------------------------------------------------------

DOWN = ("The payment service is not answering right now. Nothing has been "
        "charged — please try again in a minute.")


def _call(path: str, body: dict[str, Any] | None = None, *,
          timeout: float = TIMEOUT) -> dict[str, Any]:
    """
    One request to paysvc. Blocking — the async helpers below run it in a thread.

    Plain urllib rather than a new dependency: this is one localhost hop with a
    kilobyte of JSON, and the bot already carries enough wheels.
    """
    if not cfg.payments_enabled:
        raise PaySvcError("Top-ups are switched off on this bot at the moment.")

    url = f"{cfg.paysvc_url}{path}"
    data = json.dumps(body or {}).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method="POST" if data is not None else "GET",
        headers={"content-type": "application/json",
                 "x-paysvc-secret": cfg.paysvc_secret},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            said = json.loads(raw).get("error") or ""
        except ValueError:
            said = ""
        if exc.code == 401:
            log.error("paysvc rejected our secret — PAYSVC_SECRET differs on the two sides")
            raise PaySvcError(DOWN) from exc
        if exc.code == 404:
            raise NoSuchOrder(said or "no such order") from exc
        log.warning("paysvc %s -> HTTP %s %s", path, exc.code, said or raw[:200])
        raise PaySvcError(said or DOWN) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("paysvc %s unreachable: %s", path, exc)
        raise PaySvcError(DOWN) from exc
    except ValueError as exc:                      # not JSON — not our service
        log.error("paysvc %s returned junk: %s", path, exc)
        raise PaySvcError(DOWN) from exc


async def health() -> dict[str, Any]:
    return await asyncio.to_thread(_call, "/health", None, timeout=8.0)


_auto_confirm: tuple[float, bool] | None = None


async def auto_confirm_enabled(ttl: float = 60.0) -> bool:
    """
    Does paysvc have a bank inbox to watch? Cached for `ttl` seconds.

    This changes what a screen is allowed to promise. False means nothing will
    ever settle on its own and a human has to confirm, so the pay screen says so
    and "I have paid" goes to the admin instead of the inbox.

    A paysvc that cannot be reached counts as False. Promising credits that will
    arrive automatically, when they cannot, is the worse of the two mistakes.
    """
    global _auto_confirm
    now = time.monotonic()
    if _auto_confirm is not None and now - _auto_confirm[0] < ttl:
        return _auto_confirm[1]
    try:
        value = bool((await health()).get("autoConfirm"))
    except PaySvcError:
        value = False
    _auto_confirm = (now, value)
    return value


# --- the orders table --------------------------------------------------------

def get_order(order_id: str) -> db.Row | None:
    return db.one("SELECT * FROM orders WHERE order_id = ?", (order_id,))


def open_orders(user_id: int | None = None) -> list[db.Row]:
    """Everything still waiting for money — what the boot-time reconcile walks."""
    if user_id is None:
        return db.query("SELECT * FROM orders WHERE status IN ('pending','holding') "
                        "ORDER BY created_at")
    return db.query("SELECT * FROM orders WHERE user_id = ? AND status IN ('pending','holding') "
                    "ORDER BY created_at", (user_id,))


def expire_older_than(seconds: int = 3 * 3600) -> int:
    """
    Close out orders nobody paid.

    Generous on purpose: paysvc keeps matching for the window plus a long grace,
    and an order marked expired here would be left showing "expired" to a user
    whose money is about to land. This only tidies the list.
    """
    cur = db.execute(
        "UPDATE orders SET status = 'expired' WHERE status IN ('pending','holding') "
        "AND created_at < ?", (db.now() - seconds,))
    return cur.rowcount or 0


async def quote(user_id: int, rupees: float) -> Quote:
    """
    Create the order, then ask paysvc for the amount and QR.

    The row goes in first, as `pending`, so a paysvc that dies mid-call leaves a
    trace rather than a payment nobody can attribute. Nothing is charged and no
    credit moves here — this is a price, not a transaction.
    """
    rupees = float(rupees)
    order_id = new_order_id(user_id)
    give = credits_for(rupees)

    db.execute(
        """INSERT INTO orders (order_id, user_id, rupees, credits, status, created_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (order_id, user_id, rupees, give, db.now()),
    )

    try:
        said = await asyncio.to_thread(
            _call, "/order", {"orderId": order_id, "rupees": rupees})
    except PaySvcError:
        db.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = ?", (order_id,))
        raise

    qr = said.get("qr") or {}
    expires_at = int(said.get("expiresAt") or 0)
    db.execute(
        """UPDATE orders SET amount_paise = ?, upi_uri = ?, reference = ?,
                             status = 'holding', expires_at = ?
           WHERE order_id = ?""",
        (int(said["amountPaise"]), said["upiUri"], said.get("reference") or "",
         int(expires_at / 1000), order_id),
    )

    return Quote(
        order_id=order_id, user_id=user_id, rupees=rupees, credits=give,
        amount_paise=int(said["amountPaise"]),
        listed_paise=int(said.get("listedPaise") or round(rupees * 100)),
        upi_id=said.get("upiId") or "", payee=said.get("payee") or "",
        upi_uri=said["upiUri"], reference=said.get("reference") or "",
        expires_at=expires_at, auto_confirm=bool(said.get("autoConfirm")),
        qr_size=int(qr.get("size") or 0), qr_cells=str(qr.get("cells") or ""),
    )


# --- the only way an order becomes paid --------------------------------------

def settle(order_id: str, *, amount_paise: int | None = None,
           bank_ref: str = "", matched_on: str = "", source: str = "") -> Settlement | None:
    """
    Mark an order paid and add its credits, in one transaction. Idempotent.

    Returns the Settlement on the call that actually did it, and None on every
    other call — an unknown order, or one that was already paid. Callers use that
    to decide whether to tell the user "credits added" or just "already done", and
    the caller that gets None must never message as if it moved money.

    The credits come from the order row, written when the order was created. The
    callback body cannot change what a top-up is worth; it can only settle it.

    A cancelled or expired order still settles. If money actually arrived, the
    user paid — refusing to credit them because a timer ran out is how a bot ends
    up owing refunds. `status != 'paid'` is the only guard, and it is inside the
    UPDATE so two racing callers cannot both win it.
    """
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            log.warning("settle: no such order %s (from %s)", order_id, source or "?")
            return None
        if row["status"] == "paid":
            return None

        cur = conn.execute(
            """UPDATE orders SET status = 'paid', paid_at = ?, bank_ref = ?,
                                 amount_paise = COALESCE(?, amount_paise)
               WHERE order_id = ? AND status != 'paid'""",
            (db.now(), bank_ref or row["bank_ref"] or "", amount_paise, order_id),
        )
        if cur.rowcount != 1:          # somebody else got there first
            return None

        give = float(row["credits"])
        new_balance = credits.grant_in(
            conn, int(row["user_id"]), give,
            f"top-up ₹{float(row['rupees']):g}", ref=order_id, is_topup=True)

    paid_paise = amount_paise or int(row["amount_paise"] or 0)
    log.info("settled %s: +%g cr for user %s (%s, ref %s)",
             order_id, give, row["user_id"], matched_on or source or "?", bank_ref or "—")
    return Settlement(
        order_id=order_id, user_id=int(row["user_id"]), rupees=float(row["rupees"]),
        credits_added=give, new_balance=new_balance,
        amount_paise=paid_paise, bank_ref=bank_ref,
    )


async def check(order_id: str) -> tuple[str, Settlement | None]:
    """
    "I have paid — please check." Asks paysvc to read the inbox right now.

    Returns (status, settlement). `settlement` is only ever non-None on the call
    that moved the credits, so the screen can tell "just now" from "already done".
    """
    row = get_order(order_id)
    if row is None:
        return "unknown", None
    if row["status"] == "paid":
        return "paid", None

    try:
        said = await asyncio.to_thread(_call, "/check", {"orderId": order_id})
    except NoSuchOrder:
        # Never reached paysvc, or its journal has been reset. Nothing to settle,
        # and nothing to panic about — the row ages out on its own.
        return "unknown", None
    status = str(said.get("status") or "unknown")
    if status != "paid":
        return status, None

    info = said.get("paidInfo") or {}
    return "paid", settle(
        order_id,
        amount_paise=int(info.get("amountPaise") or 0) or None,
        bank_ref=str(info.get("bankRef") or ""),
        matched_on=str(info.get("matchedOn") or ""),
        source="check",
    )


async def cancel(order_id: str) -> tuple[bool, Settlement | None]:
    """
    Release the amount. Returns (cancelled, settlement).

    `cancelled` is False when the order turns out to have been paid after all —
    the money landed between drawing the screen and the tap. `settlement` is
    non-None only when *this* call was the one that settled it, which is how the
    caller knows whether it may say "credits added".
    """
    try:
        await asyncio.to_thread(_call, "/cancel", {"orderId": order_id})
    except PaySvcError as exc:
        if "already paid" in str(exc).lower():
            # Settling is the only right answer; cancelling would strand a real
            # payment, and the user is owed the credits either way.
            _, done = await check(order_id)
            return False, done
        log.info("cancel %s: paysvc said %s", order_id, exc)
    row = get_order(order_id)
    if row is not None and row["status"] == "paid":
        return False, None
    db.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = ? "
               "AND status != 'paid'", (order_id,))
    return True, None


# --- drawing the QR ----------------------------------------------------------

QUIET_MODULES = 4       # the spec's quiet zone; readers genuinely enforce it
TARGET_PX = 720         # big enough to scan off a phone screen held at arm's length


def qr_png(size: int, cells: str) -> Any | None:
    """
    Paint paysvc's QR grid into a PNG, or None if Pillow is not installed.

    The grid is encoded once, in the gateway, and only drawn here. A second QR
    encoder on this side could disagree with the first, and the failure mode of a
    QR that encodes the wrong amount is a payment nobody can match.
    """
    if not size or len(cells) < size * size:
        return None
    try:
        from PIL import Image
    except ImportError:
        log.warning("pillow is not installed — sending the payment screen without a QR")
        return None

    import io

    span = size + QUIET_MODULES * 2
    grid = Image.new("L", (span, span), 255)
    pixels = grid.load()
    for row in range(size):
        base = row * size
        for col in range(size):
            if cells[base + col] == "1":
                pixels[col + QUIET_MODULES, row + QUIET_MODULES] = 0

    scale = max(1, TARGET_PX // span)
    # NEAREST, not the default: a smoothed QR is a QR with soft module edges,
    # which is exactly what makes a camera fail to lock on.
    image = grid.resize((span * scale, span * scale), Image.NEAREST).convert("RGB")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    buffer.name = "upi-qr.png"      # pyrogram takes the filename from the stream
    return buffer


# --- picking up what was missed ----------------------------------------------

async def reconcile() -> list[Settlement]:
    """
    At boot: settle anything that was paid while the bot was down.

    The push from paysvc is lost if nobody is listening, so every open order is
    re-checked once on startup. Without this, a payment made during a restart
    would sit there until the user thought to press the button — for money, "the
    user will notice" is not a recovery plan.
    """
    settled: list[Settlement] = []
    rows = open_orders()
    if not rows:
        return settled

    log.info("reconciling %d open order(s) with paysvc", len(rows))
    for row in rows:
        try:
            status, done = await check(row["order_id"])
        except PaySvcError as exc:
            log.warning("reconcile stopped at %s: %s", row["order_id"], exc)
            break                       # paysvc is down; the next boot will retry
        if done is not None:
            settled.append(done)
        elif status == "unknown":
            log.info("paysvc has no record of %s — leaving it open", row["order_id"])
    expire_older_than()
    return settled
