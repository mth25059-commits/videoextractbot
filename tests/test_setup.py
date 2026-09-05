"""
The setup wizard: the parts that can be tested without a person typing.

The wizard's whole job is to be the only thing between `git clone` and a running
bot, which makes its failure mode specific and nasty: it writes an `.env` that
looks filled in and is subtly wrong, and the bug surfaces later as "the bot
randomly does not work". So this suite is aimed at the silent-wrongness cases
rather than at the conversation.

Four of them, and each is a real way this could go wrong:

- **A value that does not survive the round trip.** Both readers of `.env` — the
  bot's `config._load_dotenv` and paysvc's `loadEnv` — end a value at a ` #`, so a
  proxy password containing " #" would be cut in half and the proxy would fail
  authentication with no clue why. `quote()` decides when quoting is needed, and
  the test is a round trip through `read()` for values chosen to be awkward.

- **A key the wizard writes that `.env.example` does not explain.** `.env` is
  produced by filling in the example, so a key missing from the template lands in
  an appended block with no comment above it. Part D compares the two lists, which
  turns "somebody added a question and forgot the example" into a failing test.

- **A re-run that loses an answer.** `from_env` → `env()` → `read()` has to be the
  identity for everything the wizard owns, or pressing Enter twelve times would
  quietly change something. Part C asserts it, including the two awkward cases:
  numbered cookies, and `RUPEES_PER_CREDIT` as the old upside-down spelling.

- **A check that raises instead of reporting.** Every function in `checks.py`
  returns a `Result`, because a wizard that dies on a dead proxy is worse than one
  that says the proxy is dead. Part E calls the ones that need no network and
  points the rest at things that are definitely broken.

Part F runs the real FamApp parser over the scrubbed fixture — the same code paysvc
uses in production — and skips when node or paysvc's packages are absent.

Nothing here writes to the repo. Every file this suite touches is under a
`tempfile.TemporaryDirectory`, and `checks.prepare()` only ever puts placeholders
into `os.environ`.

Run: python tests/test_setup.py
"""
import builtins
import contextlib
import getpass
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # pragma: no cover
    pass

from setup import ask, checks, envfile, service            # noqa: E402

checks.prepare()                                            # before anything imports bot

from setup import wizard                                    # noqa: E402

passed = failed = skipped = 0


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


def skip(name, why):
    global skipped
    skipped += 1
    print(f"  --   {name}  ({why})")


def section(title):
    print(f"\n{title}\n{'─' * len(title)}")


def accepts(fn, raw, want=None):
    """A validator that takes the value, and returns it cleaned."""
    try:
        got = fn(raw)
    except ask.Invalid as exc:
        return f"refused: {exc}"
    return got if want is None else got


def refuses(fn, raw):
    """True when the validator complains — which is what it is there for."""
    try:
        fn(raw)
    except ask.Invalid:
        return True
    return False


@contextlib.contextmanager
def answering(*keystrokes):
    """
    A person at the keyboard, replaced by a list. Anything the prompt prints is
    swallowed, so the test output stays readable.
    """
    replies = list(keystrokes)

    def reader(_prompt=""):
        if not replies:
            raise EOFError
        return replies.pop(0)

    real_input, real_getpass, real_stdout = builtins.input, getpass.getpass, sys.stdout
    # `ask.ask` looks both of these up when it is called, not when it was defined.
    builtins.input = reader
    getpass.getpass = reader
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        builtins.input, getpass.getpass = real_input, real_getpass
        sys.stdout = real_stdout


# --------------------------------------------------------------------------- #
def part_a_validators():
    section("A · shapes accepted and refused")

    # Every credential-shaped string in this file is invented. The shapes are what
    # is under test, and a real token or api_id in a tracked file is a real token in
    # a public repo — the api_id especially, which cannot be rotated at all.
    TOKEN = "1234567890:AAFexampleTokenFromBotFatherNotReal"

    # A token pasted out of a chat bubble very often brings whitespace with it, and
    # refusing that would be a wizard blaming the operator for a paste.
    check("token, spaces forgiven",
          accepts(ask.bot_token, " 1234567890 : AAFexampleTokenFromBotFatherNotReal "),
          TOKEN)
    truthy("token, no colon refused", refuses(ask.bot_token, "1234567890AAFexample"))
    truthy("token, too short refused", refuses(ask.bot_token, "884:AAFB"))
    truthy("token, api_hash pasted in refused", refuses(ask.bot_token, "a" * 32))
    check("bot id read off the token", ask.bot_id_of(TOKEN), "1234567890")

    check("admin ids, one", accepts(ask.telegram_ids, " 6100000001 "), "6100000001")
    check("admin ids, several and de-duplicated",
          accepts(ask.telegram_ids, "111, 222 111  333"), "111,222,333")
    truthy("a @username is refused", refuses(ask.telegram_ids, "@operator"))
    truthy("a name is refused", refuses(ask.telegram_ids, "operator"))

    check("api_id digits", accepts(ask.api_id, " 12345678 "), "12345678")
    truthy("api_id with letters refused", refuses(ask.api_id, "123456ab"))
    check("api_hash lowercased", accepts(ask.api_hash, "AB" * 16), "ab" * 16)
    truthy("api_hash of 31 refused", refuses(ask.api_hash, "a" * 31))

    check("upi id", accepts(ask.upi_id, " operator@fam "), "operator@fam")
    check("upi id, a phone number", accepts(ask.upi_id, "9876543210@ybl"),
          "9876543210@ybl")
    truthy("upi id with no handle refused", refuses(ask.upi_id, "operator"))
    truthy("an email is not a upi id", refuses(ask.upi_id, "a@b.com "))

    check("app password, Google's spacing stripped",
          accepts(ask.app_password, "abcd efgh ijkl mnop"), "abcdefghijklmnop")
    truthy("a short password refused", refuses(ask.app_password, "hunter2"))

    check("proxy, scheme filled in", accepts(ask.proxy, "1.2.3.4:8080"),
          "http://1.2.3.4:8080")
    check("proxy, credentials kept", accepts(ask.proxy, "http://u:p@1.2.3.4:8080"),
          "http://u:p@1.2.3.4:8080")
    check("proxy, socks5 kept", accepts(ask.proxy, "socks5://1.2.3.4:1080"),
          "socks5://1.2.3.4:1080")
    truthy("proxy with no port refused", refuses(ask.proxy, "1.2.3.4"))
    truthy("proxy port 99999 refused", refuses(ask.proxy, "1.2.3.4:99999"))

    truthy("a non-postgres url refused", refuses(ask.postgres_url, "mysql://a:b@h/db"))
    truthy("a bare word refused", refuses(ask.postgres_url, "supabase"))
    check("the pooler string accepted",
          accepts(ask.postgres_url,
                  "postgresql://postgres.abc:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres"),
          "postgresql://postgres.abc:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres")

    check("numbers, ₹ and commas forgiven", accepts(ask.number, "₹1,000"), "1000")
    check("half credits survive", accepts(ask.number, "1.5"), "1.5")
    truthy("a word is not a number", refuses(ask.number, "one and a half"))
    check("time of day", accepts(ask.hhmm, "18:30"), "18:30")
    truthy("25:00 refused", refuses(ask.hhmm, "25:00"))

    # Masking is not decoration: these strings go on a review page somebody may
    # screenshot to ask for help.
    check("a short secret is hidden whole", ask.mask("abc123"), "••••••")
    check("a long secret keeps its ends", ask.mask("Y1abcdefghijklmnop"), "Y1ab…mnop")
    check("nothing set reads as nothing set", ask.mask(""), "(not set)")
    check("a token keeps its public half",
          ask.mask_token("1234567890:AAFexampleTokenFromBotFatherNotReal"),
          "1234567890:AAF…eal")


# --------------------------------------------------------------------------- #
def part_a2_pressing_enter():
    section("A2 · what Enter does")

    # Enter takes the default and puts it through the validator like a typed answer.
    # `ask_choice` is why this matters: it shows `1` and means `local`, so an
    # unvalidated default returns the digit — and the digit is not the key, so the
    # caller silently takes the other branch of the biggest question in the wizard.
    with answering(""):
        got = ask.ask_choice("Where should credits live?",
                             [("local", "this box"), ("supabase", "postgres")],
                             default="local")
    check("Enter on a choice returns the key, not the number", got, "local")
    with answering(""):
        got = ask.ask_choice("Where should credits live?",
                             [("local", "this box"), ("supabase", "postgres")],
                             default="supabase")
    check("and it honours which one was the default", got, "supabase")
    with answering("2"):
        got = ask.ask_choice("Where should credits live?",
                             [("local", "this box"), ("supabase", "postgres")],
                             default="local")
    check("a typed number is the key too", got, "supabase")

    with answering(""):
        check("Enter keeps an installed value",
              ask.ask("Admin id", default="6100000001",
                      validate=ask.telegram_ids), "6100000001")
    with answering(""):
        check("Enter on a price keeps it",
              ask.ask_number("₹1 buys", default=1.5), 1.5)
    with answering("2.5"):
        check("and a typed price replaces it",
              ask.ask_number("₹1 buys", default=1.5), 2.5)
    # The ceiling is a typo guard: 50 typed where 0.5 was meant is a hundredfold
    # price rise nobody notices until a user complains. It must re-ask, not clamp.
    with answering("500", "2"):
        got = ask.ask_number("Price", default=1.0, maximum=50.0)
    check("over the ceiling is re-asked, not clamped", got, 2.0)

    # An installed value that no longer validates must be asked about, not written
    # back out unread — and pressing Enter again must not loop forever on it.
    with answering("", "1.2.3.4:8080"):
        check("a bad installed value is re-asked",
              ask.ask("Proxy", default="1.2.3.4:99999", validate=ask.proxy),
              "http://1.2.3.4:8080")
    with answering("", ""):
        check("and Enter twice on one falls through to blank",
              ask.ask("Proxy", default="1.2.3.4:99999", validate=ask.proxy,
                      allow_blank=True), "")

    with answering(""):
        check("blank where blank is allowed is an answer",
              ask.ask("UPI id", validate=ask.upi_id, allow_blank=True), "")
    with answering("", "operator@fam"):
        check("blank where it is not allowed is refused",
              ask.ask("UPI id", validate=ask.upi_id), "operator@fam")

    with answering(""):
        truthy("Enter on a y/N question is no", not ask.ask_yes_no("really?", False))
    with answering(""):
        truthy("Enter on a Y/n question is yes", ask.ask_yes_no("really?", True))
    with answering("maybe", "n"):
        truthy("anything else is re-asked", not ask.ask_yes_no("really?", True))

    # Ctrl-C and end-of-input both have to arrive as Cancelled, or the installer's
    # last word to the operator is a traceback.
    try:
        with answering():
            ask.ask("Anything")
        got = "no exception"
    except ask.Cancelled:
        got = "cancelled"
    check("end of input is Cancelled, not EOFError", got, "cancelled")


# --------------------------------------------------------------------------- #
def part_b_the_env_file():
    section("B · .env written and read back")

    # Every one of these has bitten somebody in some project. The ` #` case is the
    # dangerous one, because it truncates rather than fails.
    awkward = {
        "PLAIN": "hello",
        "HASH_INSIDE": "pass#word",                  # kept: no space before the #
        "HASH_AFTER_SPACE": "pass #word",            # would be cut without quoting
        "TRAILING_SPACE": "value ",
        "LEADING_QUOTE": '"quoted"',
        "EQUALS_AND_SEMIS": "ndus=Y1abc;path=/;x=1",
        "COMMA": "a,b,c",
        "EMPTY": "",
        "URL": "postgresql://u:p%40ss@h.example.com:6543/postgres?sslmode=require",
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        envfile.write(path, "\n".join(f"{k}={envfile.quote(v)}"
                                      for k, v in awkward.items()) + "\n")
        back = envfile.read(path)
        for key, value in awkward.items():
            check(f"round trip · {key}", back.get(key, "<missing>"), value)

        if os.name != "nt":
            check("mode is 600", oct(path.stat().st_mode & 0o777), "0o600")
        else:
            skip("mode is 600", "Windows has no POSIX mode")

        # A plain value must NOT be quoted — the file is read by a person with nano
        # and a wall of unnecessary quotes is noise.
        text = path.read_text(encoding="utf-8")
        truthy("a plain value is not quoted", "PLAIN=hello\n" in text)
        truthy("a value with ' #' is quoted", 'HASH_AFTER_SPACE="pass #word"' in text)
        truthy("a value with a bare # is left alone", "HASH_INSIDE=pass#word\n" in text)

        # Backups: numbered, so generation 1 is always the first .env that worked.
        first = envfile.backup(path)
        path.write_text("CHANGED=1\n", encoding="utf-8")
        second = envfile.backup(path)
        check("first backup", first.name if first else None, "env-before-1.bak")
        check("second backup", second.name if second else None, "env-before-2.bak")
        check("backup 1 kept the original", envfile.read(first).get("PLAIN"), "hello")
        check("nothing to back up returns None",
              envfile.backup(Path(tmp) / "absent.env"), None)

    section("B1 · the wizard's reader and the bot's reader agree")
    # Three programs read this file: `envfile.read` here, `config._load_dotenv` in
    # the bot, and `loadEnv` in paysvc/server.js. PAYSVC_SECRET and
    # IMAP_APP_PASSWORD are read by two of them, and a secret they disagree about is
    # a callback rejected for a bad signature with nothing in either log to say why.
    # This asserts the two Python ones; the Node one takes off one matched pair the
    # same way, which is where that rule came from.
    from bot import config                                       # noqa: PLC0415
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        envfile.write(path, "".join(f"ZZ_{k}={envfile.quote(v)}\n"
                                    for k, v in awkward.items()))
        config._load_dotenv(path)                    # setdefault, so ZZ_* keys only
        mine = envfile.read(path)
        for key in awkward:
            check(f"both readers agree · {key}",
                  os.environ.get(f"ZZ_{key}", "<missing>"),
                  mine.get(f"ZZ_{key}", "<missing>"))

    section("B2 · filling in .env.example, comments intact")
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    rendered = envfile.render(template, {"BOT_TOKEN": "1:secret", "ADMIN_IDS": "42",
                                         "TERABOX_COOKIE": "Y1abc"})
    truthy("the value is replaced", "\nBOT_TOKEN=1:secret\n" in rendered)
    truthy("the example's own comment survives",
           "From @BotFather" in rendered)
    truthy("an untouched key keeps its shipped value",
           "MAX_CONCURRENT_JOBS=6" in rendered)
    check("no key is duplicated",
          len(re.findall(r"(?m)^BOT_TOKEN=", rendered)), 1)
    check("the comment count is unchanged",
          len(re.findall(r"(?m)^#", rendered)),
          len(re.findall(r"(?m)^#", template)))

    # A key the template does not have must be appended, not dropped.
    extra = envfile.render(template, {"BRAND_NEW_KEY": "x"})
    truthy("an unknown key is appended", "\nBRAND_NEW_KEY=x\n" in extra)
    truthy("and is labelled as appended", "ADDED BY THE SETUP WIZARD" in extra)

    section("B3 · cookies are numbered, never comma-joined")
    keys = envfile.cookie_keys(["one", "two", "three"])
    check("the first has no number", keys["TERABOX_COOKIE"], "one")
    check("the second is _2", keys["TERABOX_COOKIE_2"], "two")
    check("the third is _3", keys["TERABOX_COOKIE_3"], "three")
    check("unused slots are blanked", keys["TERABOX_COOKIE_4"], "")
    check("six slots are written", len(keys), 6)
    # A cookie value legitimately contains '=' , ';' and sometimes ','. Splitting a
    # joined list on commas would shred one cookie into two broken halves, and the
    # failure would read as an expired account.
    nasty = ["ndus=Y1abc;path=/,x", "ndus=Y2def"]
    check("read back exactly", envfile.cookies_from(envfile.cookie_keys(nasty)), nasty)
    check("blank slots are not returned as cookies",
          envfile.cookies_from({"TERABOX_COOKIE": "", "TERABOX_COOKIE_2": "b"}), ["b"])


# --------------------------------------------------------------------------- #
def part_c_a_second_run():
    section("C · a re-run keeps every answer")

    # This is the identity that makes `python -m setup` safe to run again:
    #   what is installed  ->  Answers  ->  .env  ->  what is installed
    # If it is not the identity, pressing Enter twelve times changes something, and
    # the operator has no way to know which thing.
    a = wizard.Answers(
        bot_token="1234567890:AAFexampleTokenFromBotFatherNotReal",
        admin_ids="6100000001,42",
        api_id="12345678",
        api_hash="ab" * 16,
        upi_id="operator@fam",
        upi_payee_name="Operator",
        imap_user="operator@gmail.com",
        imap_app_password="abcdefghijklmnop",
        paysvc_secret="s3cret-token",
        cookies=["ndus=Y1abc;path=/,x", "ndus=Y2def"],
        proxies=["http://u:p@1.2.3.4:8080", "socks5://5.6.7.8:1080"],
        credits_per_rupee=1.5,
        cost_terabox_per_link=0.5,
        cost_zip_upto_1gb=2.0,
        cost_zip_upto_2gb=4.0,
        cost_fap_480=1.0,
        cost_fap_720=1.5,
        cost_fap_1080=2.0,
        database_url="postgresql://postgres.abc:pw@aws-0-ap-south-1.pooler."
                     "supabase.com:6543/postgres",
        fap_api="https://resolver.example.com/api",
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        envfile.write(path, envfile.render(template, a.env()))
        again = wizard.from_env(envfile.read(path))

    for attr in ("bot_token", "admin_ids", "api_id", "api_hash", "upi_id",
                 "upi_payee_name", "imap_user", "imap_app_password", "imap_sender",
                 "paysvc_secret", "cookies", "proxies", "database_url", "fap_api",
                 *wizard.PRICES.values()):
        check(f"survives a round trip · {attr}",
              getattr(again, attr), getattr(a, attr))
    truthy("top-ups still on", again.payments_on)
    truthy("auto-confirm still on", again.auto_confirm_on)

    # And the two ways a re-run could go wrong that are not simple equality.
    blank = wizard.from_env({})
    check("nothing installed reads as the shipped rate",
          blank.credits_per_rupee, wizard.Answers().credits_per_rupee)
    check("no cookies is an empty list, not [''])", blank.cookies, [])
    truthy("top-ups off when there is no UPI id", not blank.payments_on)
    check("the FamApp sender has a default", blank.imap_sender, "no-reply@famapp.in")

    # A hand-edited price that is not a number must be a question with a sensible
    # default, not a traceback on line one of the wizard.
    junk = wizard.from_env({"COST_FAP_720": "one and a half"})
    check("garbage price falls back to the shipped one",
          junk.cost_fap_720, wizard.Answers().cost_fap_720)

    # The upside-down spelling, which older installs have. Read only when the new
    # key is absent — the same precedence bot/config.py applies.
    old = wizard.from_env({"RUPEES_PER_CREDIT": "2"})
    check("RUPEES_PER_CREDIT=2 means ₹1 buys 0.5", old.credits_per_rupee, 0.5)
    both = wizard.from_env({"RUPEES_PER_CREDIT": "2", "CREDITS_PER_RUPEE": "3"})
    check("the new key wins when both are there", both.credits_per_rupee, 3.0)
    check("RUPEES_PER_CREDIT=0 is ignored, not divided by",
          wizard.from_env({"RUPEES_PER_CREDIT": "0"}).credits_per_rupee,
          wizard.Answers().credits_per_rupee)

    section("C2 · the review page renders without asking anything")
    # `show` is called for all twelve lines before a single question is asked, so a
    # blank Answers has to be printable. A crash here would land on the one screen
    # the operator is meant to read.
    empty = wizard.Answers()
    check("twelve questions, twelve renderers", len(wizard.QUESTIONS), wizard.TOTAL)
    for n, q in enumerate(wizard.QUESTIONS, 1):
        truthy(f"line {n:>2} · {q.label}", isinstance(q.show(empty), str))
        truthy(f"line {n:>2} · {q.label} has a label", bool(q.label))
    # And a filled one must not put a secret on that page.
    truthy("the token is masked on the review",
           a.bot_token not in wizard.QUESTIONS[0].show(a))
    truthy("the api_hash is masked on the review",
           a.api_hash not in wizard.QUESTIONS[7].show(a))
    truthy("a cookie is not printed on the review",
           a.cookies[0] not in wizard.QUESTIONS[5].show(a))
    truthy("a proxy password is not printed on the review",
           ":p@" not in wizard.QUESTIONS[6].show(a))


# --------------------------------------------------------------------------- #
def part_d_every_key_is_documented():
    section("D · every key the wizard writes is explained in .env.example")

    # .env is produced by filling in the example, so a key the example does not have
    # lands in an appended block with no comment above it. That is how an operator
    # ends up with a setting nobody can explain a month later.
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"(?m)^([A-Z][A-Z0-9_]*)=", template))
    written = set(wizard.Answers().env())
    missing = sorted(written - documented)
    check("nothing the wizard writes is undocumented", missing, [])
    check("all six cookie slots are in the example",
          sorted(k for k in documented if k.startswith("TERABOX_COOKIE")),
          sorted(envfile.cookie_keys([])))

    # The other half of the same list: a price the admin panel can edit that the
    # wizard cannot set would be editable from Telegram only, and the operator asked for
    # both.
    from bot import settings                                    # noqa: PLC0415
    panel = {name.upper() for name in settings.EDITABLE}
    check("the panel and the wizard edit the same prices",
          sorted(panel - set(wizard.PRICES)), [])
    check("and the wizard sets nothing the panel cannot",
          sorted(set(wizard.PRICES) - panel), [])


# --------------------------------------------------------------------------- #
def part_e_checks_report_instead_of_raising():
    section("E · a broken thing is reported, never raised")

    # The contract for every function in checks.py: it returns a Result. A wizard
    # that dies on a dead proxy is worse than one that says the proxy is dead, and
    # the operator has no traceback to read anyway.
    truthy("prepare() made bot importable",
           bool(os.environ.get("API_ID") and os.environ.get("BOT_TOKEN")))
    from bot import config                                       # noqa: PLC0415
    check("and it is the placeholder, not a real token",
          config.cfg.bot_token, checks.PLACEHOLDERS["BOT_TOKEN"])
    truthy("no placeholder could be mistaken for a credential",
           all("placeholder" in v or set(v) <= set("0") or v == "1"
               for v in checks.PLACEHOLDERS.values()))

    # ffmpeg is the one check with a real answer on this laptop either way — the
    # point is that it answers rather than throwing.
    got = checks.ffmpeg()
    truthy("ffmpeg answers", isinstance(got, checks.Result))
    print(f"       {'✓' if got.ok else '✖'} {got.detail}")

    got = checks.node()
    truthy("node answers", isinstance(got, checks.Result))
    print(f"       {'✓' if got.ok else '✖'} {got.detail}")

    # Port 1 on loopback refuses instantly, so this is the dead-service case without
    # a network round trip.
    got = checks.paysvc("http://127.0.0.1:1")
    truthy("a dead paysvc is reported, not raised", isinstance(got, checks.Result))
    check("and it is not ok", got.ok, False)
    truthy("and it says what to do about it", bool(got.hint))

    got = checks.database("postgresql://u:p@127.0.0.1:1/postgres")
    truthy("a dead database is reported, not raised", isinstance(got, checks.Result))
    check("and it is not ok", got.ok, False)

    got = checks.cookie("ndus=obviously-not-a-real-session", 1)
    truthy("a wrong cookie is reported, not raised", isinstance(got, checks.Result))
    check("and it is not ok", got.ok, False)

    # A proxy on a closed local port: dead, and its password must not come back out
    # in the detail string — that string goes on the review page.
    got = checks.proxy("http://user:sup3rsecret@127.0.0.1:1")
    truthy("a dead proxy is reported, not raised", isinstance(got, checks.Result))
    check("and it is not ok", got.ok, False)
    truthy("and the password is not in what it says",
           "sup3rsecret" not in f"{got.detail} {got.hint}")

    got = checks.fampay(Path("/definitely/not/here.eml"), "no-reply@famapp.in")
    check("an absent bank alert is not ok", got.ok, False)

    section("E2 · the boot unit is templated, not shipped as-is")
    template = service.TEMPLATE.read_text(encoding="utf-8")
    app_dir = Path("/srv/videoextractbot")
    unit = service.render_unit(template, app_dir, "operator")
    truthy("the directory is this box's", str(app_dir) in unit)
    truthy("nothing still says /opt/terabot", "/opt/terabot" not in unit)
    truthy("it runs as the login that installed it", "User=operator" in unit)
    truthy("and the group too", "Group=operator" in unit)
    truthy("no comment still says ubuntu", "`ubuntu`" not in unit)
    # ExecStop is not decoration: run.sh stop signals the process group and waits
    # STOP_WAIT seconds, and that wait is what lets the queue refund jobs in flight.
    truthy("ExecStop is there", "ExecStop=" in unit)
    truthy("and it goes through run.sh", re.search(r"ExecStop=.*run\.sh stop", unit)
           is not None)
    truthy("RemainAfterExit, so ExecStop is ever reached",
           "RemainAfterExit=yes" in unit)
    truthy("a login is chosen for us", bool(service.default_user()))
    truthy("the unit has its own name, not terabot's",
           service.UNIT == "videoextractbot.service")

    section("E3 · --dir has to be a bot tree, and the right one")
    import setup.__main__ as entry                             # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        # Asserted outside the `answering` block, always: it replaces stdout, so a
        # `check` inside one prints into a buffer nobody reads — including its
        # failures.
        with answering():
            # Not a bot tree at all: refused before anything is asked, so an EOF
            # here would mean the guard tried to prompt.
            code = entry.main(["--dir", tmp])
        check("a directory with no .env.example is refused", code, 2)

        # A tree that has the file but is not the one this `setup` lives in: `.env`
        # would be written here and read over there, and `cfg.db_path` is fixed to
        # the code tree, so every check would tick against the wrong database.
        (Path(tmp) / ".env.example").write_text("BOT_TOKEN=\n", encoding="utf-8")
        with answering("n"):
            code = entry.main(["--dir", tmp])
        check("a tree that is not this one asks first, and n means no", code, 2)

        with answering():
            # EOF at that question is Ctrl-D, which must read as "no" and not as a
            # traceback out of the installer's own entry point.
            code = entry.main(["--dir", tmp])
        check("and Ctrl-D there is a stop, not a crash", code, 130)


# --------------------------------------------------------------------------- #
def part_f_the_real_famapp_parser():
    section("F · the bank alert, through the code paysvc actually runs")

    fixture = ROOT / "paysvc" / "test" / "fixtures" / "famapp-received.eml"
    if not fixture.exists():
        skip("the scrubbed fixture parses", "no fixture in this tree")
        return
    if not shutil.which("node"):
        skip("the scrubbed fixture parses", "node is not installed")
        return
    if not (ROOT / "paysvc" / "node_modules").is_dir():
        skip("the scrubbed fixture parses", "paysvc has no node_modules")
        return

    got = checks.fampay(fixture, "no-reply@famapp.in")
    check("the fixture passes", got.ok, True)
    if not got.ok:
        print(f"         {got.detail}\n         {got.hint}")
    else:
        print(f"       ✓ {got.detail}")

    # The sender check is the half that makes the parse mean anything: a From line
    # is attacker-controlled text, the DKIM signature is not. Point it at the wrong
    # domain and it has to refuse.
    wrong = checks.fampay(fixture, "no-reply@not-famapp.example")
    check("a mail signed by someone else is refused", wrong.ok, False)
    truthy("and it says how to save the mail properly", bool(wrong.hint))

    # A file that is not a mail at all: the parser must report, not throw.
    with tempfile.TemporaryDirectory() as tmp:
        junk = Path(tmp) / "pasted.txt"
        junk.write_text("You received Rs 20 from someone\n", encoding="utf-8")
        got = checks.fampay(junk, "no-reply@famapp.in")
        check("a pasted body with no headers is refused", got.ok, False)


# --------------------------------------------------------------------------- #
def main():
    print("\nsetup wizard — the parts that need no typing")
    part_a_validators()
    part_a2_pressing_enter()
    part_b_the_env_file()
    part_c_a_second_run()
    part_d_every_key_is_documented()
    part_e_checks_report_instead_of_raising()
    part_f_the_real_famapp_parser()
    tail = f"\n{passed} passed, {failed} failed"
    print(f"{tail}, {skipped} skipped\n" if skipped else f"{tail}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
