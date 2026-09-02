"""
Configuration — every tunable lives in .env, nothing is hardcoded here.

Import `cfg` and read attributes off it. Anything missing that the bot cannot
run without raises at import time with a message naming the variable, so a bad
deploy fails on the first second instead of on the first user.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader. No dependency, and it will not clobber real env vars."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    pass


def _req(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


def _num(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _ids(name: str) -> tuple[int, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    out = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"{name} contains a non-numeric id: {chunk!r}") from exc
    return tuple(out)


def _list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    return tuple(c.strip() for c in raw.split(",") if c.strip())


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # telegram
    api_id: int
    api_hash: str
    bot_token: str
    admin_ids: tuple[int, ...]
    log_chat_id: int | None

    # credits
    free_credits_on_join: float
    min_topup_rupees: int
    rupees_per_credit: float
    cost_terabox_per_link: float
    cost_zip_upto_1gb: float
    cost_zip_upto_2gb: float
    max_links_per_batch: int

    # workers
    max_concurrent_jobs: int
    max_upload_mb: int
    work_dir: Path

    # payments
    paysvc_url: str
    paysvc_secret: str
    paid_callback_port: int
    payment_window_minutes: int

    # a menu button that only says "not available yet" — no download, no charge
    show_soon_button: bool = False
    soon_button_label: str = "🔥  Fap"

    # misc
    proxies: tuple[str, ...] = field(default=())

    @property
    def db_path(self) -> Path:
        return ROOT / "data" / "bot.db"

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    @property
    def payments_enabled(self) -> bool:
        return bool(self.paysvc_secret)


def load() -> Config:
    work_dir = Path(os.environ.get("WORK_DIR", "./downloads").strip() or "./downloads")
    if not work_dir.is_absolute():
        work_dir = ROOT / work_dir

    cfg = Config(
        api_id=int(_req("API_ID")),
        api_hash=_req("API_HASH"),
        bot_token=_req("BOT_TOKEN"),
        admin_ids=_ids("ADMIN_IDS"),
        log_chat_id=(_ids("LOG_CHAT_ID") or (None,))[0],
        free_credits_on_join=_num("FREE_CREDITS_ON_JOIN", 2),
        min_topup_rupees=_int("MIN_TOPUP_RUPEES", 20),
        rupees_per_credit=_num("RUPEES_PER_CREDIT", 1),
        cost_terabox_per_link=_num("COST_TERABOX_PER_LINK", 1),
        cost_zip_upto_1gb=_num("COST_ZIP_UPTO_1GB", 2),
        cost_zip_upto_2gb=_num("COST_ZIP_UPTO_2GB", 4),
        max_links_per_batch=_int("MAX_LINKS_PER_BATCH", 10),
        max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 3),
        max_upload_mb=_int("MAX_UPLOAD_MB", 2000),
        work_dir=work_dir,
        paysvc_url=os.environ.get("PAYSVC_URL", "http://127.0.0.1:4400").rstrip("/"),
        paysvc_secret=os.environ.get("PAYSVC_SECRET", "").strip(),
        paid_callback_port=_int("PAID_CALLBACK_PORT", 8081),
        payment_window_minutes=_int("PAYMENT_WINDOW_MINUTES", 10),
        show_soon_button=_bool("SHOW_SOON_BUTTON", False),
        soon_button_label=os.environ.get("SOON_BUTTON_LABEL", "🔥  Fap").strip() or "🔥  Fap",
        proxies=_list("PROXIES"),
    )

    if not cfg.admin_ids:
        raise ConfigError("ADMIN_IDS is empty — set at least your own user id.")

    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg


cfg = load()
