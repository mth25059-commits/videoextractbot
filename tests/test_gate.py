"""
The text-handler gate.

Pyrogram runs one message handler per group and then stops. Four handlers want
private text (ZIP password, top-up amount, admin give-credit, a batch of links),
so if they all share `filters.private & filters.text` the first one registered
eats every message and the other three are dead code. `bot/handlers/_gate.py`
prevents that by moving the "is this mine?" question into the filter.

This file holds that property: **for any one user, at most one gated handler can
match, and it is the one whose mode is set.** Run: python tests/test_gate.py
"""
import asyncio
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "6100000001")


# --- stub pyrogram.filters.create, faithfully -------------------------------
# The real one builds a one-off class whose __call__ is the check function and
# whose kwargs become attributes, then returns an instance. Mirroring that is
# what makes this test meaningful rather than a test of the stub.
def _create(func, name="CustomFilter", **kwargs):
    obj = types.SimpleNamespace(**kwargs)
    obj.check = lambda client, update: func(obj, client, update)
    obj.name = name
    return obj


_filters = types.ModuleType("pyrogram.filters")
_filters.create = _create
_pyrogram = types.ModuleType("pyrogram")
_pyrogram.filters = _filters
sys.modules.update({"pyrogram": _pyrogram, "pyrogram.filters": _filters})

from bot import state                    # noqa: E402
from bot.handlers import _gate           # noqa: E402

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")


def msg(user_id):
    """The only two attributes the filter touches."""
    if user_id is None:
        return types.SimpleNamespace(from_user=None)
    return types.SimpleNamespace(from_user=types.SimpleNamespace(id=user_id))


def matches(flt, user_id):
    return asyncio.run(flt.check(None, msg(user_id)))


# The four real gates, exactly as the handlers declare them.
ZIP = _gate.in_mode("await_zip_password")
GIVE = _gate.in_mode("admin_give_user", "admin_give_amount")
PAY = _gate.in_mode("await_amount")
SOON = _gate.in_mode("await_soon_link")
ALL = [("zip", ZIP), ("give", GIVE), ("pay", PAY), ("soon", SOON)]

USER = 777


def who_matches(user_id=USER):
    return [name for name, flt in ALL if matches(flt, user_id)]


def main():
    print("\n— nothing in progress —")
    state.clear_mode(USER)
    check("no mode: no handler claims it", who_matches(), [])
    check("owns() with no mode", _gate.owns(USER, {"await_zip_password"}), False)

    print("\n— one mode at a time, exactly one claim —")
    for mode, expected in [("await_zip_password", ["zip"]),
                           ("admin_give_user", ["give"]),
                           ("admin_give_amount", ["give"]),
                           ("await_amount", ["pay"]),
                           ("await_soon_link", ["soon"])]:
        state.set_mode(USER, mode)
        check(f"{mode} -> {expected}", who_matches(), expected)

    print("\n— a mode is never shared —")
    state.set_mode(USER, "await_zip_password", zip_path="/tmp/a.zip")
    check("zip claims", who_matches(), ["zip"])
    state.set_mode(USER, "admin_give_amount", target=42)
    check("set_mode replaces: zip is now inert", matches(ZIP, USER), False)
    check("and give took over", who_matches(), ["give"])
    check("payload survives the gate", state.get_mode(USER)[1], {"target": 42})

    print("\n— other people, and no people —")
    state.set_mode(USER, "await_zip_password")
    check("a different user is unaffected", who_matches(999), [])
    check("channel post / no from_user", matches(ZIP, None), False)

    print("\n— after the handler clears it —")
    state.clear_mode(USER)
    check("cleared: nothing claims it again", who_matches(), [])

    print("\n— misuse —")
    try:
        _gate.in_mode()
        check("in_mode() with no modes raises", "no error", "ValueError")
    except ValueError:
        check("in_mode() with no modes raises", "ValueError", "ValueError")

    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
