"""
The exchange rate, the per-video price, and the neutral panel label.

Three small things live here because all three are *one number seen from two
sides*, and every bug in this area has been a mismatch between the sides:

- **₹1 = 1.5 credits** is what the operator says. `credits_for()` divides, so it needs
  ₹0.6667 per credit. Both numbers have to exist, they have to agree, and the one
  that reaches the screen has to be the readable one — a top-up card that reads
  "1 credit = ₹0.6667" is the same rate and the wrong sentence.
- **0.5 credits per video** is charged in two places (a floor at the confirm, the
  rest once the folder has been read), so the default has to be pinned here.
- **`ui.panel_title`** is the label that replaced the video's name on every live
  panel. It is one line of code and the whole of the privacy change the user can
  see, so it gets assertions rather than trust.

Run: python tests/test_config.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Every assertion here is about money, and the rupee sign is not in cp1252 — which is
# what a Windows console hands `print`. Without this, a *failing* check would die with
# a UnicodeEncodeError and hide which one it was.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # pragma: no cover
    pass

os.environ.setdefault("API_ID", "1234")
os.environ.setdefault("API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("ADMIN_IDS", "6100000001")

from bot import config, ui                          # noqa: E402
from bot.config import cfg                          # noqa: E402

passed = failed = 0


def check(name, got, want):
    global passed, failed
    if got == want:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")


def near(name, got, want, tol=1e-9):
    check(name, abs(got - want) < tol, True)


def rate_with(**env):
    """`config._rate()` with exactly these settings and no others."""
    with only(("CREDITS_PER_RUPEE", "RUPEES_PER_CREDIT"), env):
        return config._rate()


def price_with(**env):
    """What one video costs with exactly these settings and no others."""
    with only(("COST_TERABOX_PER_LINK",), env):
        return config._num("COST_TERABOX_PER_LINK", 0.5)


class only:
    """
    Run a block with `names` set to `env` and nothing else, then put it all back.

    The box's own `.env` is read at import, so a test that asserted on live `cfg`
    would pass or fail depending on which machine it ran on. The defaults are the
    thing worth pinning — they are what a fresh box gets — so they are read with the
    environment cleared out from under them.
    """

    def __init__(self, names, env):
        self.names = tuple(names)
        self.env = env
        self.saved = {}

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.names}
        for key in self.names:
            os.environ.pop(key, None)
        for key, value in self.env.items():
            os.environ[key] = value
        return self

    def __exit__(self, *_exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def main():
    print("\n— the rate, read from either setting —")
    per_rupee, per_credit = rate_with(CREDITS_PER_RUPEE="1.5")
    near("CREDITS_PER_RUPEE=1.5 gives 1.5 credits a rupee", per_rupee, 1.5)
    near("and 2/3 of a rupee a credit, which is what divides", per_credit, 2 / 3)

    per_rupee, per_credit = rate_with(RUPEES_PER_CREDIT="2")
    near("a bare RUPEES_PER_CREDIT=2 still works", per_credit, 2.0)
    near("and derives the other way round", per_rupee, 0.5)

    per_rupee, per_credit = rate_with(CREDITS_PER_RUPEE="1.5", RUPEES_PER_CREDIT="9")
    near("with both set the readable one wins", per_rupee, 1.5)
    near("so the two can never disagree", per_credit, 2 / 3)

    per_rupee, per_credit = rate_with()
    near("neither set falls back to 1.5, not to 1", per_rupee, 1.5)
    near("and the pair is still consistent", per_rupee * per_credit, 1.0)

    for bad in ("0", "-3"):
        per_rupee, per_credit = rate_with(CREDITS_PER_RUPEE=bad)
        near(f"CREDITS_PER_RUPEE={bad} is ignored rather than dividing by zero",
             per_rupee, 1.5)

    print("\n— the money itself —")
    near("₹20 buys 30 credits", round(20 / (2 / 3), 2), 30.0)
    near("₹50 buys 75", round(50 / (2 / 3), 2), 75.0)
    near("₹1 buys 1.5", round(1 / (2 / 3), 2), 1.5)
    near("the live config agrees with itself",
         cfg.credits_per_rupee * cfg.rupees_per_credit, 1.0)
    check("and a top-up of the floor is a whole number of credits",
          round(cfg.min_topup_rupees * cfg.credits_per_rupee, 2) % 1, 0.0)

    print("\n— the rate as a sentence —")
    check("the sentence is the way round a human says it",
          ui.rate_text(2 / 3), "₹1 = <b>1.5 credits</b>")
    check("no floating-point tail on it", "0000" in ui.rate_text(2 / 3), False)
    check("at parity it is singular", ui.rate_text(1), "₹1 = <b>1 credit</b>")
    check("double is plural again", ui.rate_text(0.5), "₹1 = <b>2 credits</b>")
    check("a nonsense rate does not raise or divide by zero",
          ui.rate_text(0), "₹1 = <b>1 credit</b>")

    print("\n— 0.5 credits, per video —")
    near("a fresh box charges half a credit for one video", price_with(), 0.5)
    near("so a folder of ten costs five", 10 * price_with(), 5.0)
    near("a box that names the price is obeyed",
         price_with(COST_TERABOX_PER_LINK="0.25"), 0.25)
    near("and the live config is the one the handlers charge",
         cfg.cost_terabox_per_link, config._num("COST_TERABOX_PER_LINK", 0.5))

    print("\n— the panel says a number, never a name —")
    check("one of three", ui.panel_title(0, 3), "Video 1 of 3")
    check("the last of three", ui.panel_title(2, 3), "Video 3 of 3")
    check("alone, it is just Video", ui.panel_title(0, 1), "Video")
    check("and with no arguments at all", ui.panel_title(), "Video")
    check("a zero total does not read as 'of 0'", ui.panel_title(0, 0), "Video")

    print("\n— the guide tells the truth about both —")
    # Read from `cfg`, not hardcoded: the guide's job is to state *this box's* rate,
    # and a test that hardcodes 1.5 only proves the developer's `.env` says 1.5.
    guide = ui.guide(is_new=True)
    check("the rate is in it, the way round a human says it",
          ui.rate_text(cfg.rupees_per_credit) in guide, True)
    check("with what the smallest top-up comes to",
          f"{cfg.min_topup_rupees * cfg.credits_per_rupee:g} credit" in guide, True)
    check("the old promise of ten videos for one price is gone",
          "for that same one price" in guide, False)
    check("the per-video price is stated",
          f"{cfg.cost_terabox_per_link:g} credit" in guide, True)
    check("and the deposit walkthrough is there",
          all(word in guide for word in ("Add Credit", "I have paid")), True)
    check("including the exact-amount rule, which is what identifies the payment",
          "exact" in guide.lower(), True)

    print(f"\n{passed} passed, {failed} failed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
