"""
Force-join: the bot answers nobody until they are in every channel you name.

`FORCE_JOIN` is empty on a fresh clone and the gate is then entirely absent — no
handler, no API call, no behaviour at all. Switched on, one handler in group -1
stands in front of every private message and every callback, and this file holds
the five things that have to stay true about it:

* **Every spelling of a channel people actually paste means the same channel**, and
  the two that cannot be checked are refused rather than shown as a join button
  that leads nowhere.
* **A status is one plain word.** `str.lstrip` takes a set of characters, not a
  prefix, so the obvious way to turn `ChatMemberStatus.ADMINISTRATOR` into a word
  eats the leading `a` and an admin reads as not joined. That is a regression test
  below, not a hypothetical.
* **A yes is cached for five minutes and a no is never cached.** The moment after
  somebody joins is exactly when they press ✅, and answering that from a stale no
  would tell them they had not joined.
* **An error means joined.** Thrown out of a channel, `get_chat_member` raises for
  every user at once; calling that "not joined" would lock everybody out of a bot
  that works, paying users included.
* **The gate stops propagation, and `join:ok` escapes it.** A gate that swallowed
  its own verify button would be a locked door with the key inside.

Run: python tests/test_join.py
"""
import asyncio
import dataclasses
import os
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A Windows console is cp1252, where every box-drawing rule and ✅ in this file would
# raise mid-line. The bot's home is Linux and UTF-8; this is for a dry run on a laptop.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ["ADMIN_IDS"] = "6100000001"
os.environ["FORCE_JOIN"] = ""            # the gate is off until a test turns it on

ADMIN = 6100000001
USER = 777

# --------------------------------------------------------------------------- #
# A stub pyrogram, with a fixed list of names.
#
# `bot/keyboards.py` needs four types and `bot/handlers/join.py` needs the two
# handler classes plus `StopPropagation`. The list being fixed is the point: a new
# `pyrogram.types` import in shared code fails here loudly rather than passing with
# a silently missing attribute.
# --------------------------------------------------------------------------- #
class Btn:
    def __init__(self, text="", url=None, callback_data=None, **kw):
        self.text, self.url, self.callback_data = text, url, callback_data


class Markup:
    def __init__(self, rows=None, **kw):
        self.rows = rows or []


class Key:
    def __init__(self, text="", **kw):
        self.text = text


class Keys:
    def __init__(self, rows=None, **kw):
        self.rows = rows or []


class StopPropagation(Exception):
    """Pyrogram's own: raised out of a handler to stop every later group."""


class Handler:
    """`MessageHandler(callback, filters)` — only the callback is exercised."""

    def __init__(self, callback, filters=None):
        self.callback, self.filters = callback, filters


class MessageHandler(Handler):
    pass


class CallbackQueryHandler(Handler):
    pass


class UserNotParticipant(Exception):
    """Pyrogram's name for "definitely not in the channel" — matched by name."""


class ChatAdminRequired(Exception):
    """The bot is not an admin there, so Telegram refuses the question entirely."""

_types = types.ModuleType("pyrogram.types")
_types.InlineKeyboardButton = Btn
_types.InlineKeyboardMarkup = Markup
_types.KeyboardButton = Key
_types.ReplyKeyboardMarkup = Keys
_types.CallbackQuery = object
_types.Message = object

_filters = types.ModuleType("pyrogram.filters")
_filters.private = "filters.private"      # only ever compared, never called

_handlers = types.ModuleType("pyrogram.handlers")
_handlers.MessageHandler = MessageHandler
_handlers.CallbackQueryHandler = CallbackQueryHandler

_pyrogram = types.ModuleType("pyrogram")
_pyrogram.Client = object
_pyrogram.StopPropagation = StopPropagation
_pyrogram.filters = _filters
_pyrogram.types = _types
_pyrogram.handlers = _handlers
sys.modules.update({"pyrogram": _pyrogram, "pyrogram.types": _types,
                    "pyrogram.filters": _filters, "pyrogram.handlers": _handlers})

from bot import joingate, keyboards as kb, state, ui        # noqa: E402
from bot.handlers import join                               # noqa: E402

REAL_CFG = joingate.cfg

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")


def truthy(name, got):
    check(name, bool(got), True)


def section(title):
    print(f"\n{title}")
    print("─" * min(len(title), 72))

# --------------------------------------------------------------------------- #
# Fakes for the things the handler talks to.
# --------------------------------------------------------------------------- #
class FakeClient:
    """
    Counts its calls, because "without a single API call" is the assertion for both
    the gate-off and the admin path, and a count is the only way to say it.

    `statuses` maps a channel ref to a status word, an exception to raise, or a
    `(status, is_member)` pair for the restricted case. Anything unnamed is a
    plain `member`.
    """

    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.calls = []
        self.sent = []

    async def get_chat_member(self, ref, user_id):
        self.calls.append((ref, user_id))
        value = self.statuses.get(ref, "member")
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return types.SimpleNamespace(status=value[0], is_member=value[1])
        return types.SimpleNamespace(status=value, is_member=None)

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeMessage:
    def __init__(self, user_id=USER, name="Rita", *, edit_fails=False):
        self.from_user = (None if user_id is None else
                          types.SimpleNamespace(id=user_id, first_name=name,
                                                username="rita"))
        self.replies = []
        self.edits = []
        self._edit_fails = edit_fails

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw.get("reply_markup")))

    async def edit_text(self, text, **kw):
        if self._edit_fails:
            raise RuntimeError("MESSAGE_NOT_MODIFIED")
        self.edits.append((text, kw.get("reply_markup")))

class FakeCQ:
    def __init__(self, data, user_id=USER, name="Rita", *, edit_fails=False):
        self.data = data
        self.from_user = types.SimpleNamespace(id=user_id, first_name=name,
                                               username="rita")
        self.message = FakeMessage(user_id, name, edit_fails=edit_fails)
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append((text, show_alert))


class FakeApp:
    def __init__(self):
        self.handlers = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


class FakeCredits:
    """
    Stands in for `bot.credits`, which would otherwise want a database.

    `ensure` is what the gate calls, and the reason it does is worth keeping: a
    gated user has to exist in the database before they join, or the operator's
    "somebody new arrived" count only ever shows the ones who did.
    """

    def __init__(self):
        self.calls = []

    def ensure(self, user_id, first_name="", username=""):
        self.calls.append((user_id, first_name, username))
        user = types.SimpleNamespace(user_id=user_id, first_name=first_name,
                                     credits=2.0)
        return user, len(self.calls) == 1


def gate_on(*entries):
    """Turn the gate on for these channels, with an empty cache."""
    joingate.cfg = dataclasses.replace(REAL_CFG, force_join=tuple(entries))
    joingate.clear_cache()
    state.clear_mode(USER)


def run(coro):
    """Run one handler. True when it stopped the rest of the bot."""
    try:
        asyncio.run(coro)
        return False
    except StopPropagation:
        return True

# --------------------------------------------------------------------------- #
def part_a_what_force_join_accepts():
    section("A · every spelling of a channel means the same channel")

    # The three ways a public channel arrives — typed, typed without the @, and
    # pasted straight out of Telegram — are one channel, so they parse to one entry.
    for raw in ("@myupdates", "myupdates", "https://t.me/myupdates",
                " http://t.me/myupdates ", "telegram.me/myupdates/"):
        one = joingate._one(raw)
        check(f"{raw!r} → ref", one and one.ref, "@myupdates")
        check(f"{raw!r} → link", one and one.link, "https://t.me/myupdates")
        check(f"{raw!r} → label", one and one.label, "@myupdates")

    # A private channel: the id is the only thing membership can be checked against,
    # the invite link the only thing that can go on a button. Both halves or nothing.
    private = joingate._one("-1001234567890|https://t.me/+AbCdEfGh")
    check("a private channel is checked by id", private and private.ref, -1001234567890)
    check("and joined by its invite link", private and private.link,
          "https://t.me/+AbCdEfGh")
    check("a hash makes no label, so it is named plainly",
          private and private.label, "Channel")
    check("spaces round the pipe are forgiven",
          joingate._one(" -100123 | https://t.me/+Ab ").ref, -100123)

    # Refused, all four for the same reason: there is no way to both check them and
    # offer a button, so a button would be a dead end.
    check("an id with no invite link is skipped", joingate._one("-1001234567890"), None)
    check("an invite link with no id is skipped",
          joingate._one("https://t.me/+AbCdEfGh"), None)
    check("an old joinchat link too", joingate._one("t.me/joinchat/AbCdEf"), None)
    check("and blank is nothing at all", joingate._one("   "), None)

    section("A2 · the configured list")
    gate_on("@one_channel", "@one_channel", " two_channel ", "-100999")
    got = joingate.configured()
    check("duplicates collapse and the unusable is dropped", len(got), 2)
    check("order is the order it was written",
          [c.ref for c in got], ["@one_channel", "@two_channel"])
    truthy("the gate is on", joingate.is_on())
    gate_on()
    check("nothing configured is no channels", joingate.configured(), ())
    check("and the gate is off", joingate.is_on(), False)

# --------------------------------------------------------------------------- #
def part_b_a_status_is_one_word():
    section("B · a status is one plain lowercase word")

    class Enum:
        """Pyrogram's ChatMemberStatus: `.value` is the word, `str()` is not."""

        def __init__(self, value):
            self.value = value

        def __str__(self):
            return f"ChatMemberStatus.{self.value.upper()}"

    check("a plain word survives", joingate.status_name("member"), "member")
    check("an enum's value is used", joingate.status_name(Enum("administrator")),
          "administrator")
    check("and a stringified enum is cut at the dot",
          joingate.status_name("ChatMemberStatus.ADMINISTRATOR"), "administrator")
    check("case and space do not matter", joingate.status_name("  OWNER "), "owner")

    # The regression. `"administrator".lstrip("chatmemberstatus.")` is
    # `"dministrator"`, because lstrip takes a *set of characters*: every leading
    # character that appears in that string is eaten, and `a` is in it. An admin then
    # failed the membership test and the bot gated its own operator.
    truthy("an administrator is joined", joingate.joined("administrator"))
    truthy("as an enum too", joingate.joined(Enum("administrator")))
    truthy("and stringified", joingate.joined("ChatMemberStatus.ADMINISTRATOR"))
    for word in ("member", "owner", "creator"):
        truthy(f"{word} is joined", joingate.joined(word))
    for word in ("left", "banned", "kicked", "restricted", "something_new"):
        check(f"{word} is not joined", joingate.joined(word), False)
    check("and neither is nothing at all", joingate.joined(None), False)

    # The wizard's stricter question. A bot that is only a *member* of a channel
    # cannot call get_chat_member at all — Telegram answers ChatAdminRequired — and
    # the gate's fail-open then lets everybody through while looking like it works.
    # That mistake can only be caught at install time, which is why ADMIN exists.
    for word in ("administrator", "owner", "creator"):
        truthy(f"{word} can ask who else is in the channel", word in joingate.ADMIN)
    check("a plain member cannot", "member" in joingate.ADMIN, False)
    truthy("every admin word is also a joined word", joingate.ADMIN <= joingate.IN)

# --------------------------------------------------------------------------- #
def part_c_one_channel_at_a_time():
    section("C · one channel, and what each answer means")

    gate_on("@myupdates")
    one = joingate.configured()[0]

    def ask(value):
        client = FakeClient({"@myupdates": value})
        return asyncio.run(joingate.in_channel(client, one, USER))

    truthy("a member is in", ask("member"))
    truthy("an administrator is in", ask("administrator"))
    check("somebody who left is not", ask("left"), False)
    check("and somebody banned is not", ask("banned"), False)
    check("UserNotParticipant is the one no we trust",
          ask(UserNotParticipant("not in chat")), False)

    # Fail-open. Every one of these means the operator has a problem — the bot was
    # removed, the channel went private, a flood wait landed — and none of them is
    # this user's fault. Answering "not joined" would lock every user out at once.
    truthy("thrown out of the channel lets the user through",
           ask(ChatAdminRequired("bot is not an admin")))
    truthy("a flood wait lets the user through", ask(RuntimeError("FLOOD_WAIT_42")))
    truthy("so does anything else", ask(ValueError("who knows")))

    # A restricted member is muted, not gone. Their status is `restricted`, so asking
    # the status alone would gate them for good: the join button cannot undo a mute,
    # and ✅ would never turn green. `is_member` is the whole answer for them.
    truthy("a muted member is still in", ask(("restricted", True)))
    check("one whose restriction removed them is not", ask(("restricted", False)), False)
    check("an explicit is_member=False is believed over the status",
          ask(("member", False)), False)

    section("C2 · the pass cache remembers a yes and never a no")
    gate_on("@one_channel", "@two_channel")

    client = FakeClient()
    check("in both channels means nothing to join",
          asyncio.run(joingate.missing(client, USER)), [])
    check("one call per channel, no more", len(client.calls), 2)
    truthy("the pass is remembered", joingate.cached_pass(USER))
    check("a second message asks Telegram nothing",
          asyncio.run(joingate.missing(client, USER)), [])
    check("still two calls", len(client.calls), 2)

    # A no is never cached, and asking again is the point: the second call is what
    # somebody pressing ✅ ten seconds after joining depends on.
    gate_on("@one_channel", "@two_channel")
    client = FakeClient({"@two_channel": "left"})
    out = asyncio.run(joingate.missing(client, USER))
    check("the one still to join comes back", [c.ref for c in out], ["@two_channel"])
    check("nothing was remembered", joingate.cached_pass(USER), False)
    asyncio.run(joingate.missing(client, USER))
    check("so the next message asks again", len(client.calls), 4)

    # And leaving a channel costs access again, even inside the five minutes: the pass
    # is dropped the moment a fresh check fails.
    gate_on("@one_channel")
    client = FakeClient()
    asyncio.run(joingate.missing(client, USER))
    truthy("a pass is in hand", joingate.cached_pass(USER))
    client.statuses["@one_channel"] = "left"
    out = asyncio.run(joingate.missing(client, USER, use_cache=False))
    check("a fresh check sees they left", [c.ref for c in out], ["@one_channel"])
    check("and the pass is gone", joingate.cached_pass(USER), False)

    section("C3 · five minutes, an admin, and a bot that gates nobody")
    check("the pass lasts five minutes", joingate.PASS_TTL, 300)
    joingate.remember(USER)
    joingate._passed[USER] = time.time() - 1        # rewind past the expiry
    check("an expired pass is no pass", joingate.cached_pass(USER), False)
    check("and is not kept around", USER in joingate._passed, False)

    # Never gated, and never a single API call for it: an operator who has not joined
    # their own channel would otherwise be unable to reach the panel that fixes it.
    gate_on("@one_channel")
    client = FakeClient({"@one_channel": "left"})
    check("an admin walks straight through",
          asyncio.run(joingate.missing(client, ADMIN)), [])
    check("without asking Telegram anything", client.calls, [])

    gate_on()
    client = FakeClient({"@one_channel": "left"})
    check("with FORCE_JOIN empty everybody walks through",
          asyncio.run(joingate.missing(client, USER)), [])
    check("also without an API call", client.calls, [])

# --------------------------------------------------------------------------- #
def part_d_the_card():
    section("D · the card, its buttons, and what is on the screen")

    gate_on("@one_channel", "-100999|https://t.me/+AbCdEfGh")
    channels = joingate.configured()
    markup = kb.join_gate(channels)
    check("a row per channel, plus the verify button", len(markup.rows),
          len(channels) + 1)
    for n, c in enumerate(channels):
        row = markup.rows[n]
        check(f"row {n + 1} is one button", len(row), 1)
        check(f"row {n + 1} opens the channel", row[0].url, c.link)
        check(f"row {n + 1} is not a callback", row[0].callback_data, None)
        truthy(f"row {n + 1} names it", c.label in row[0].text)
    last = markup.rows[-1]
    check("the verify button is alone on the last row", len(last), 1)
    check("and it is the one callback the gate answers itself",
          last[0].callback_data, "join:ok")
    check("a url button cannot carry a callback", last[0].url, None)

    text = ui.join_gate(channels, "Rita")
    truthy("the card greets them by name", "Rita" in text)
    for c in channels:
        truthy(f"the card names {c.label}", c.label in text)
        truthy(f"and links {c.label}", c.link in text)
    truthy("two channels are 'these channels'", "these channels" in text)
    truthy("one channel is 'this channel'",
           "this channel" in ui.join_gate(channels[:1]))
    truthy("no name is not an empty greeting", "  " not in ui.join_gate(channels)[:40])

    # A first name is user-supplied text going into an HTML message. Unescaped, a
    # name with a `<` in it breaks the parse and Telegram refuses the whole card —
    # which would leave a gated user staring at nothing.
    nasty = ui.join_gate(channels, "Ri<ta & co")
    truthy("a name's angle bracket is escaped", "Ri&lt;ta" in nasty)
    truthy("and its ampersand", "&amp; co" in nasty)

    alert = ui.join_still_missing(channels[:1])
    truthy("the alert names the one that is left", channels[0].label in alert)
    truthy("two left says how many", "2 still to join" in
           ui.join_still_missing(channels))
    check("an alert fits in Telegram's 200-character limit",
          max(len(ui.join_still_missing(channels)),
              len(ui.join_still_missing(channels[:1]))) <= 200, True)

# --------------------------------------------------------------------------- #
def part_e_the_choke_point():
    section("E · one handler in front of the whole bot")

    # Off means absent. Registering a gate that lets everybody through would be
    # harmless, but a bot that is not gating anybody has no business owning a
    # handler group.
    gate_on()
    app = FakeApp()
    join.register(app)
    check("nothing is registered when FORCE_JOIN is empty", app.handlers, [])

    gate_on("@one_channel", "@two_channel")
    app = FakeApp()
    join.register(app)
    check("two handlers, a message one and a callback one", len(app.handlers), 2)
    check("both in group -1, ahead of every other group",
          [group for _, group in app.handlers], [-1, -1])
    kinds = [type(h).__name__ for h, _ in app.handlers]
    check("the message handler is first", kinds, ["MessageHandler",
                                                 "CallbackQueryHandler"])
    check("and it only looks at private chats", app.handlers[0][0].filters,
          _filters.private)
    on_message = app.handlers[0][0].callback
    on_callback = app.handlers[1][0].callback

    join.credits = fake = FakeCredits()

    section("E2 · a message from somebody who has not joined")
    gate_on("@one_channel", "@two_channel")
    client = FakeClient({"@two_channel": "left"})
    message = FakeMessage()
    state.set_mode(USER, "await_zip_password")
    truthy("the rest of the bot does not run", run(on_message(client, message)))
    check("one card, and only one", len(message.replies), 1)
    truthy("it is the join card", "One step first" in message.replies[0][0])
    check("it names only what is left",
          "@two_channel" in message.replies[0][0]
          and "@one_channel" not in message.replies[0][0], True)
    check("the card carries the buttons", len(message.replies[0][1].rows), 2)
    check("the user is in the database before they join", fake.calls,
          [(USER, "Rita", "rita")])
    check("and any half-finished flow is dropped", state.get_mode(USER), None)
    check("nothing they sent was deleted", message.edits, [])

    section("E3 · and from everybody the gate is not for")
    gate_on("@one_channel")
    fake.calls.clear()
    client = FakeClient()
    message = FakeMessage()
    check("somebody in the channel is not stopped",
          run(on_message(client, message)), False)
    check("and is shown nothing", message.replies, [])
    check("the gate does not register them either — /start does that",
          fake.calls, [])

    client = FakeClient({"@one_channel": "left"})
    message = FakeMessage(ADMIN, "Operator")
    check("an admin is not stopped", run(on_message(client, message)), False)
    check("shown nothing", message.replies, [])
    check("and cost nothing to check", client.calls, [])

    client = FakeClient({"@one_channel": "left"})
    post = FakeMessage(None)
    check("a channel post is not a person", run(on_message(client, post)), False)
    check("so nobody is asked about it", client.calls, [])
    check("and no card is put up", post.replies, [])

    section("E4 · a button pressed from behind the gate")
    gate_on("@one_channel")
    client = FakeClient({"@one_channel": "left"})
    cq = FakeCQ("pay:open")
    truthy("the flow behind it does not run", run(on_callback(client, cq)))
    check("they get told why, as an alert", len(cq.answers), 1)
    truthy("and it is an alert, not a toast", cq.answers[0][1])
    check("the card replaces what was on screen", len(cq.message.edits), 1)
    truthy("with the join card", "One step first" in cq.message.edits[0][0])

    client = FakeClient()
    cq = FakeCQ("pay:open")
    check("a joined user's button is left alone",
          run(on_callback(client, cq)), False)
    check("with nothing said", cq.answers, [])
    check("and nothing edited", cq.message.edits, [])

    section("E5 · ✅ I've joined")
    # The escape button, answered in group -1 so the gate cannot swallow it. And
    # `use_cache=False`, which is the whole point of this press: a stale pass — or a
    # stale refusal — is the one answer that is certainly wrong ten seconds after
    # somebody joined.
    gate_on("@one_channel")
    joingate.remember(USER)                  # a pass from before they left
    client = FakeClient({"@one_channel": "left"})
    cq = FakeCQ("join:ok")
    truthy("the press stops here", run(on_callback(client, cq)))
    check("Telegram was asked again despite the cached pass", len(client.calls), 1)
    truthy("they are told which channel is still open",
           "@one_channel" in cq.answers[0][0])
    truthy("as an alert they cannot miss", cq.answers[0][1])
    check("the card is refreshed", len(cq.message.edits), 1)
    check("and no keyboard was installed", client.sent, [])

    gate_on("@one_channel")
    fake.calls.clear()
    client = FakeClient()
    cq = FakeCQ("join:ok")
    truthy("a joined user's press stops here too", run(on_callback(client, cq)))
    truthy("with a tick", "Verified" in cq.answers[0][0])
    check("not as an alert — nothing to read", cq.answers[0][1], False)
    check("they are in the database now", fake.calls, [(USER, "Rita", "rita")])
    check("the gate card becomes the menu", len(cq.message.edits), 1)
    truthy("which welcomes them", "Welcome" in cq.message.edits[0][0])
    truthy("and offers the services", cq.message.edits[0][1].rows)
    check("the typing keyboard is installed, once", len(client.sent), 1)
    check("to them", client.sent[0][0], USER)
    truthy("the pass is now cached", joingate.cached_pass(USER))

    # Twice in a row without joining anything: the second edit is byte-identical, so
    # Telegram refuses it. The alert has already been sent; there is nothing else to
    # do and certainly nothing to crash about.
    gate_on("@one_channel")
    client = FakeClient({"@one_channel": "left"})
    cq = FakeCQ("join:ok", edit_fails=True)
    truthy("a refused edit is not an error", run(on_callback(client, cq)))
    check("the alert still went out", len(cq.answers), 1)

    # Fail-open, seen from the user's chair: the operator has removed the bot from
    # the channel, so nobody can be checked — and the bot keeps working for everybody
    # instead of locking the whole userbase out at once.
    gate_on("@one_channel")
    client = FakeClient({"@one_channel": ChatAdminRequired("bot is not an admin")})
    message = FakeMessage()
    check("a broken channel does not stop anybody",
          run(on_message(client, message)), False)
    check("and shows no card", message.replies, [])


# --------------------------------------------------------------------------- #
def main():
    print(__doc__.strip().splitlines()[0])
    try:
        part_a_what_force_join_accepts()
        part_b_a_status_is_one_word()
        part_c_one_channel_at_a_time()
        part_d_the_card()
        part_e_the_choke_point()
    finally:
        joingate.cfg = REAL_CFG
        joingate.clear_cache()
        state.clear_mode(USER)
    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
