"""
The money path.

One property matters more than everything else in this file: **a payment credits
a user exactly once.** Two things can arrive at the same order — paysvc's push and
the user's own "I have paid" button, or two copies of the same push after a retry
— and only one of them may move credits.

paysvc is not needed to prove that. `payments._call` is the single seam where the
bot talks to it, so it is replaced by a fake here and every path through
`quote/check/cancel/reconcile` is exercised without a network. The callback
listener, by contrast, is run for real over a socket: it is the one door an
outside process knocks on, and a test that stubs the parsing tests nothing.

Run: python tests/test_payments.py
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import types
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A Windows console is cp1252 by default and every price in here has a ₹ in it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "6100000001")

# --- stub pyrogram: the money path does not import it, but the UI does --------
_pyrogram = types.ModuleType("pyrogram")
_pyrogram.Client = type("Client", (), {})


def _create(func, name="CustomFilter", **kwargs):
    obj = types.SimpleNamespace(**kwargs)
    obj.check = lambda client, update: func(obj, client, update)
    return obj


_filters = types.ModuleType("pyrogram.filters")
_filters.create = _create
_types = types.ModuleType("pyrogram.types")
# Every name `bot.keyboards` imports, plus the two the handlers annotate with.
# This list is a hard dependency, not a convenience: an import of a name that is
# missing here fails as `ImportError: cannot import name … (unknown location)`
# long before any assertion runs, so adding a `pyrogram.types` import anywhere in
# shared code means adding it here in the same commit.
for _name in ("Message", "CallbackQuery", "InlineKeyboardButton",
              "InlineKeyboardMarkup", "KeyboardButton", "ReplyKeyboardMarkup"):
    setattr(_types, _name, type(_name, (), {"__init__": lambda self, *a, **k: None}))
_pyrogram.filters, _pyrogram.types = _filters, _types
sys.modules.update({"pyrogram": _pyrogram, "pyrogram.filters": _filters,
                    "pyrogram.types": _types})

from bot import callback_server, credits, db, payments   # noqa: E402
from bot.handlers import payment as payment_ui           # noqa: E402

SECRET = "test-secret-0123456789abcdef"
PORT = 8747

# Both modules did `from .config import cfg`, so rebinding the name on the module
# is how a frozen Config gets varied for a test. The joining bonus is switched off
# here so that a balance of 20 means "the top-up, and nothing else" — the bonus
# has its own test in test_queue.py.
for _mod in (payments, callback_server, payment_ui, credits):
    _mod.cfg = replace(_mod.cfg, paysvc_secret=SECRET, paid_callback_port=PORT,
                       min_topup_rupees=20, rupees_per_credit=1,
                       free_credits_on_join=0)

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")


def fresh_db():
    """A throwaway database, never the real bot.db."""
    path = Path(tempfile.mkdtemp(prefix="terabot-pay-")) / "test.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.commit()
    db._conn = conn


# --- the fake paysvc ---------------------------------------------------------

class FakeSvc:
    """
    Stands in for the service at the one seam the bot talks through.

    It reserves a different amount per order, the way the real gateway does, so a
    test that assumed both orders were quoted ₹20 would fail here too.
    """

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.calls: list[str] = []
        self.next_paise = 1999
        self.auto_confirm = True
        self.down = False

    def __call__(self, path, body=None, *, timeout=None):
        self.calls.append(path)
        if self.down:
            raise payments.PaySvcError(payments.DOWN)
        return getattr(self, path[1:])(body or {})

    def health(self, _body):
        return {"ok": True, "autoConfirm": self.auto_confirm}

    def order(self, body):
        paise, self.next_paise = self.next_paise, self.next_paise - 7
        order = {"status": "holding", "amountPaise": paise,
                 "listedPaise": int(round(float(body["rupees"]) * 100)),
                 "reference": f"PAY-{body['orderId'][-6:]}", "paidInfo": None}
        self.orders[body["orderId"]] = order
        return {"ok": True, "orderId": body["orderId"], **order,
                "upiId": "demo@okhdfcbank", "payee": "TeraBot",
                "upiUri": f"upi://pay?pa=demo@okhdfcbank&am={paise / 100:.2f}",
                "expiresAt": (db.now() + 600) * 1000,
                "autoConfirm": self.auto_confirm,
                "qr": {"size": 3, "cells": "101010101"}}

    def check(self, body):
        order = self.orders.get(body["orderId"])
        if order is None:
            raise payments.NoSuchOrder("no such order")
        return {"ok": True, "orderId": body["orderId"], **order}

    def cancel(self, body):
        order = self.orders.get(body["orderId"])
        if order is None:
            raise payments.NoSuchOrder("no such order")
        if order["status"] == "paid":
            raise payments.PaySvcError("already paid")
        order["status"] = "cancelled"
        return {"ok": True}

    def pay(self, order_id, bank_ref="BANKREF1"):
        """The bank alert lands: what the poller would do inside paysvc."""
        order = self.orders[order_id]
        order["status"] = "paid"
        order["paidInfo"] = {"amountPaise": order["amountPaise"],
                             "bankRef": bank_ref, "matchedOn": "amount"}


svc = FakeSvc()
payments._call = svc

# --- what a rupee buys, and what the bot will accept -------------------------

def test_pricing():
    print("\npricing")
    check("₹1 = 1 credit", payments.credits_for(20), 20.0)
    check("₹250 = 250 credits", payments.credits_for(250), 250.0)
    payments.cfg = replace(payments.cfg, rupees_per_credit=2)
    check("RUPEES_PER_CREDIT=2 halves it", payments.credits_for(20), 10.0)
    payments.cfg = replace(payments.cfg, rupees_per_credit=1)

    order_id = payments.new_order_id(6100000001)
    check("an order id rides inside callback data",
          len(f"pay:check:{order_id}".encode()) <= 64, True)
    check("two order ids differ",
          payments.new_order_id(1) != payments.new_order_id(1), True)


def test_amount_parsing():
    print("\nthe amount a user types")
    good = [("20", 20), ("₹20", 20), (" 100 ", 100), ("1,500", 1500),
            ("rs 60", 60), ("Rs.60", 60), ("250/-", 250), ("20.0", 20)]
    for text, want in good:
        check(f"{text!r} -> ₹{want}", payments_parse(text), (want, ""))

    check("₹19 is under the floor", payments_parse("19")[0], 0)
    check("₹0 is under the floor", payments_parse("0")[0], 0)
    check("a negative amount is refused", payments_parse("-50")[0], 0)
    check("paise are refused", payments_parse("20.50")[0], 0)
    check("words are refused", payments_parse("twenty")[0], 0)
    check("empty is refused", payments_parse("")[0], 0)
    check("₹1 lakh is allowed", payments_parse("100000")[0], 100000)
    check("above the UPI ceiling is refused", payments_parse("100001")[0], 0)
    check("the floor is quoted in the refusal",
          "₹20" in payments_parse("5")[1], True)


def payments_parse(text):
    return payment_ui._parse_rupees(text)


# --- the heart: one payment, one grant ---------------------------------------

async def test_settle_once():
    print("\nsettle() is idempotent")
    USER = 4242
    credits.ensure(USER, "the operator", "operator")
    start = credits.balance(USER)

    quote = await payments.quote(USER, 20)
    check("quoted below the listed price", quote.amount_paise < 2000, True)
    check("credits come from the rate, not the quote", quote.credits, 20.0)
    check("row is holding", payments.get_order(quote.order_id)["status"], "holding")
    check("nothing charged yet", credits.balance(USER), start)

    first = payments.settle(quote.order_id, amount_paise=quote.amount_paise,
                            bank_ref="REF-1", source="test")
    check("the first caller gets a Settlement", first is not None, True)
    check("credits added", credits.balance(USER), start + 20.0)
    check("balance reported matches", first.new_balance, start + 20.0)
    check("row is paid", payments.get_order(quote.order_id)["status"], "paid")

    again = payments.settle(quote.order_id, bank_ref="REF-1", source="retry")
    check("a second call grants nothing", again, None)
    check("balance unmoved by the retry", credits.balance(USER), start + 20.0)

    rows = db.query("SELECT * FROM ledger WHERE ref = ?", (quote.order_id,))
    check("exactly one ledger row for the order", len(rows), 1)
    check("counted as a top-up",
          db.scalar("SELECT total_topup FROM users WHERE user_id = ?", (USER,)), 20.0)
    check("not counted as spending",
          db.scalar("SELECT total_spent FROM users WHERE user_id = ?", (USER,)), 0.0)


async def test_settle_trusts_the_row():
    print("\nthe callback cannot inflate a top-up")
    USER = 4343
    credits.ensure(USER, "Someone", None)
    quote = await payments.quote(USER, 50)

    # A callback claiming ₹9,999 was paid. The credits still come from the row.
    done = payments.settle(quote.order_id, amount_paise=999_900,
                           bank_ref="FORGED", source="test")
    check("credits are the order's, not the callback's", done.credits_added, 50.0)
    check("balance grew by the order's credits", credits.balance(USER), 50.0)

    print("\nedge cases")
    check("an unknown order settles nothing",
          payments.settle("TB-nope-1", source="test"), None)

    USER2 = 4444
    credits.ensure(USER2, "Late", None)
    late = await payments.quote(USER2, 20)
    db.execute("UPDATE orders SET status = 'cancelled' WHERE order_id = ?",
               (late.order_id,))
    done = payments.settle(late.order_id, source="test")
    check("a cancelled order still pays if the money arrived",
          done is not None and credits.balance(USER2) == 20.0, True)


# --- the pull path -----------------------------------------------------------

async def test_check_and_cancel():
    print("\ncheck()")
    USER = 5151
    credits.ensure(USER, "Puller", None)
    quote = await payments.quote(USER, 20)

    status, done = await payments.check(quote.order_id)
    check("unpaid: no settlement", (status, done), ("holding", None))
    check("balance untouched", credits.balance(USER), 0.0)

    svc.pay(quote.order_id, "BANK-77")
    status, done = await payments.check(quote.order_id)
    check("paid: settled here", (status, done is not None), ("paid", True))
    check("credits in", credits.balance(USER), 20.0)
    check("bank ref kept", payments.get_order(quote.order_id)["bank_ref"], "BANK-77")

    status, done = await payments.check(quote.order_id)
    check("checking again grants nothing", (status, done), ("paid", None))
    check("balance still 20", credits.balance(USER), 20.0)

    status, done = await payments.check("TB-never-existed")
    check("an order the bot never made", (status, done), ("unknown", None))

    print("\ncancel()")
    USER2 = 5252
    credits.ensure(USER2, "Canceller", None)
    first = await payments.quote(USER2, 20)
    cancelled, done = await payments.cancel(first.order_id)
    check("a holding order cancels", (cancelled, done), (True, None))
    check("row is cancelled", payments.get_order(first.order_id)["status"], "cancelled")
    check("nothing charged", credits.balance(USER2), 0.0)

    # The money lands in the gap between drawing the screen and the tap.
    second = await payments.quote(USER2, 20)
    svc.pay(second.order_id, "RACE-1")
    cancelled, done = await payments.cancel(second.order_id)
    check("a paid order refuses to cancel", cancelled, False)
    check("and is settled instead, once", done is not None, True)
    check("the user got their credits", credits.balance(USER2), 20.0)
    check("the order is paid, not cancelled",
          payments.get_order(second.order_id)["status"], "paid")


async def test_reconcile():
    print("\nreconcile() — what was paid while the bot was down")
    USER = 6161
    credits.ensure(USER, "Restarted", None)
    paid = await payments.quote(USER, 20)
    unpaid = await payments.quote(USER, 40)
    svc.pay(paid.order_id, "BOOT-1")

    settled = await payments.reconcile()
    check("only the paid one is settled", [s.order_id for s in settled], [paid.order_id])
    check("credits recovered without a button press", credits.balance(USER), 20.0)
    check("the unpaid order is left open",
          payments.get_order(unpaid.order_id)["status"], "holding")

    check("a second boot settles nothing again", await payments.reconcile(), [])
    check("balance unmoved", credits.balance(USER), 20.0)

    print("\npaysvc being down")
    svc.down = True
    try:
        await payments.quote(USER, 20)
        check("quote raises when paysvc is down", False, True)
    except payments.PaySvcError:
        check("quote raises when paysvc is down", True, True)
    dead = db.query("SELECT * FROM orders WHERE user_id = ? AND status = 'cancelled'",
                    (USER,))
    check("the stub row is cancelled, not left pending", len(dead), 1)
    check("reconcile stops instead of hammering it", await payments.reconcile(), [])
    check("auto_confirm is False when it cannot be asked",
          await payments.auto_confirm_enabled(ttl=0), False)
    svc.down = False
    check("and True again once it answers",
          await payments.auto_confirm_enabled(ttl=0), True)


# --- the push path, over a real socket ---------------------------------------

async def http(path, body, secret=SECRET, method="POST", raw=None):
    """One request to the callback listener. Returns (status_code, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    if raw is None:
        payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        head = (f"{method} {path} HTTP/1.1\r\n"
                f"host: 127.0.0.1\r\n"
                f"content-type: application/json\r\n"
                f"content-length: {len(payload)}\r\n")
        if secret is not None:
            head += f"x-paysvc-secret: {secret}\r\n"
        raw = head.encode() + b"\r\n" + payload
    writer.write(raw)
    await writer.drain()
    if body is None:
        # A half-written request. Closing our side makes the server's readuntil
        # fail now rather than sit out its 15-second timeout.
        writer.write_eof()
    data = await asyncio.wait_for(reader.read(-1), 10)
    writer.close()
    head, _, tail = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), tail.decode(errors="replace")


async def test_callback_server():
    print("\nthe callback listener")
    seen: list[str] = []
    boom: list[bool] = [False]

    async def on_paid(payload):
        if boom[0]:
            raise RuntimeError("database is locked")
        done = payments.settle(
            str(payload.get("orderId") or ""),
            amount_paise=int(payload.get("amountPaise") or 0) or None,
            bank_ref=str(payload.get("bankRef") or ""), source="callback")
        if done is not None:
            seen.append(done.order_id)

    server = await callback_server.serve(on_paid)
    try:
        USER = 7171
        credits.ensure(USER, "Pushed", None)
        quote = await payments.quote(USER, 20)
        good = {"orderId": quote.order_id, "amountPaise": quote.amount_paise,
                "bankRef": "PUSH-1", "matchedOn": "amount"}

        code, _ = await http("/paid", good, secret="wrong-secret")
        check("a wrong secret is rejected", code, 401)
        check("and nothing was credited", credits.balance(USER), 0.0)

        code, _ = await http("/paid", good, secret=None)
        check("no secret at all is rejected", code, 401)

        code, body = await http("/paid", good)
        check("the real secret is accepted", (code, body), (200, '{"ok":true}'))
        check("credits added", credits.balance(USER), 20.0)
        check("the handler saw it once", seen, [quote.order_id])

        code, _ = await http("/paid", good)
        check("a retried callback answers 200", code, 200)
        check("but credits only once", credits.balance(USER), 20.0)
        check("and the user is not told twice", seen, [quote.order_id])

        print("\nbad requests cannot take the bot down")
        check("GET /paid", (await http("/paid", {}, method="GET"))[0], 405)
        check("an unknown path", (await http("/nope", {}))[0], 404)
        check("/health", (await http("/health", {}, method="GET"))[0], 200)
        check("a body that is not JSON", (await http("/paid", "{not json"))[0], 400)
        check("a JSON array instead of an object",
              (await http("/paid", "[1,2,3]"))[0], 400)
        check("no orderId is accepted and ignored",
              (await http("/paid", {"amountPaise": 100}))[0], 200)
        check("a truncated request",
              (await http("/paid", None, raw=b"POST /paid HTTP/1.1\r\nhost: x\r\n"))[0],
              400)

        print("\na failure is retryable, not silent")
        boom[0] = True
        second = await payments.quote(USER, 20)
        code, _ = await http("/paid", {"orderId": second.order_id})
        check("a handler that raises answers 500 so paysvc retries", code, 500)
        boom[0] = False
        code, _ = await http("/paid", {"orderId": second.order_id})
        check("and the retry succeeds", code, 200)
        check("credits landed on the retry", credits.balance(USER), 40.0)
    finally:
        server.close()
        await server.wait_closed()


# --- the QR, and tidying up --------------------------------------------------

def test_qr_and_housekeeping():
    print("\nthe QR")
    png = payments.qr_png(3, "101010101")
    if png is None:
        print("  --   pillow is not installed, skipping the PNG check")
    else:
        head = png.getvalue()[:8]
        check("it is a PNG", head, b"\x89PNG\r\n\x1a\n")
        check("pyrogram gets a filename", png.name, "upi-qr.png")
        from PIL import Image
        image = Image.open(png)
        span = 3 + payments.QUIET_MODULES * 2          # 11 modules with the quiet zone
        scale = payments.TARGET_PX // span
        check("quiet zone included and scaled up", image.size, (span * scale,) * 2)
        check("a module is black", image.getpixel(
            (payments.QUIET_MODULES * scale, payments.QUIET_MODULES * scale)), (0, 0, 0))
        check("the quiet zone is white", image.getpixel((0, 0)), (255, 255, 255))

    check("no matrix, no PNG", payments.qr_png(0, ""), None)
    check("a matrix short of cells is refused", payments.qr_png(41, "101"), None)

    print("\nhousekeeping")
    USER = 8181
    credits.ensure(USER, "Old", None)
    db.execute("""INSERT INTO orders (order_id, user_id, rupees, credits, status,
                                      created_at)
                  VALUES ('TB-stale', ?, 20, 20, 'holding', ?)""",
               (USER, db.now() - 4 * 3600))
    open_before = len(payments.open_orders(USER))
    check("it shows as open", open_before, 1)
    check("expiring closes it", payments.expire_older_than(3 * 3600), 1)
    check("and it is out of the open list", payments.open_orders(USER), [])
    check("but it can still be settled if the money turns up",
          payments.settle("TB-stale", source="late") is not None, True)
    check("the user got the credits", credits.balance(USER), 20.0)


def test_env_placeholders():
    """
    A key left blank with a note beside it must read as blank.

    This is here rather than in a config test because of which key it bites:
    `IMAP_APP_PASSWORD=  # not yet` parsed as the string "# not yet" is *truthy*,
    so the gateway would report autoConfirm true, the pay screen would promise
    credits that land by themselves, and no inbox would be watched at all.
    """
    print("\nplaceholders in .env read as blank")
    from bot import config as bot_config

    def read(line):
        path = Path(tempfile.mkdtemp()) / ".env"
        path.write_text(line + "\n", encoding="utf-8")
        os.environ.pop("PROBE", None)
        bot_config._load_dotenv(path)
        return os.environ.get("PROBE")

    check("a trailing note does not become the value", read("PROBE=   # <-- BAKI"), "")
    check("a tab before the note counts too", read("PROBE=\t# later"), "")
    check("a real value keeps its note off", read("PROBE=abc  # mine"), "abc")
    check("a # inside a password survives", read("PROBE=pa#ssword"), "pa#ssword")
    check("quoted values are taken whole", read('PROBE="a # b"'), "a # b")
    check("an ordinary value is unharmed", read("PROBE=someone@bank"), "someone@bank")


async def main():
    fresh_db()
    test_pricing()
    test_amount_parsing()
    test_env_placeholders()
    await test_settle_once()
    await test_settle_trusts_the_row()
    await test_check_and_cancel()
    await test_reconcile()
    await test_callback_server()
    test_qr_and_housekeeping()
    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
