"""
The one door paysvc knocks on: "order X has been paid."

This is a ~90-line HTTP server rather than aiohttp because it answers exactly one
request shape, on the loopback interface, and adding a web framework to a Telegram
bot to receive one JSON body is not a trade worth making. It runs on the bot's own
event loop, so the handler can move credits and send the message directly.

    POST /paid                       x-paysvc-secret: <shared secret>
    {"orderId": "...", "amountPaise": 1973, "bankRef": "...", ...}

Three properties, in the order they matter:

* **It binds to 127.0.0.1.** Anything that can reach this port and knows the
  secret can hand out credits. It is not an internet-facing endpoint and there is
  no configuration to make it one.
* **The secret is compared in constant time**, and a wrong one is logged. This is
  the only authentication there is.
* **A bad request cannot take the bot down.** Every failure path answers with a
  status code and closes; nothing propagates out of the connection handler.

The push is not the only path — `payments.check()` pulls the same settlement from
paysvc — so a request lost here costs the user a wait, not their money.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Awaitable, Callable

from .config import cfg

log = logging.getLogger(__name__)

HEADER_LIMIT = 8 * 1024
BODY_LIMIT = 64 * 1024
READ_TIMEOUT = 15.0

OnPaid = Callable[[dict[str, Any]], Awaitable[None]]


def _response(code: int, reason: str, body: str = "") -> bytes:
    payload = body.encode()
    return (
        f"HTTP/1.1 {code} {reason}\r\n"
        f"content-type: application/json\r\n"
        f"content-length: {len(payload)}\r\n"
        "connection: close\r\n"
        "\r\n"
    ).encode() + payload


def _parse_head(head: bytes) -> tuple[str, str, dict[str, str]]:
    lines = head.decode("latin-1").split("\r\n")
    method, _, rest = lines[0].partition(" ")
    path = rest.rpartition(" ")[0].strip() or "/"
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return method.upper(), path.split("?")[0], headers


def _authorised(headers: dict[str, str]) -> bool:
    given = headers.get("x-paysvc-secret", "")
    return bool(cfg.paysvc_secret) and hmac.compare_digest(given, cfg.paysvc_secret)


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT)
    method, path, headers = _parse_head(head[:-4])

    length = 0
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        pass
    if length > BODY_LIMIT:
        raise ValueError(f"body of {length} bytes is far too large")

    body = b""
    if length:
        body = await asyncio.wait_for(reader.readexactly(length), READ_TIMEOUT)
    return method, path, headers, body


async def serve(on_paid: OnPaid) -> asyncio.AbstractServer:
    """
    Start listening. Returns the server so the caller can close it on shutdown.

    `on_paid` is awaited with the decoded JSON body. It owns everything that
    follows — settling the order, messaging the user — and it is expected to be
    idempotent, because paysvc retries a call the bot did not answer.
    """

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            method, path, headers, body = await _read_request(reader)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                asyncio.TimeoutError, ValueError, UnicodeDecodeError) as exc:
            log.warning("callback: malformed request from %s (%s)", peer, exc)
            writer.write(_response(400, "Bad Request", '{"ok":false}'))
            await _close(writer)
            return

        if not _authorised(headers):
            log.warning("callback: rejected an unauthenticated %s %s from %s",
                        method, path, peer)
            writer.write(_response(401, "Unauthorized", '{"ok":false,"error":"bad secret"}'))
            await _close(writer)
            return

        if path == "/health":
            writer.write(_response(200, "OK", '{"ok":true}'))
            await _close(writer)
            return

        if path != "/paid":
            writer.write(_response(404, "Not Found", '{"ok":false}'))
            await _close(writer)
            return
        if method != "POST":
            writer.write(_response(405, "Method Not Allowed", '{"ok":false}'))
            await _close(writer)
            return

        try:
            payload = json.loads(body.decode() or "{}")
            if not isinstance(payload, dict):
                raise ValueError("body is not an object")
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("callback: body was not JSON (%s)", exc)
            writer.write(_response(400, "Bad Request", '{"ok":false}'))
            await _close(writer)
            return

        # Act first, answer second. A 500 here is what makes paysvc try again, and
        # trying again is safe: `payments.settle()` is idempotent, so the retry
        # grants nothing and the handler stays quiet — at most one message per
        # order, however many times this is called.
        try:
            await on_paid(payload)
        except Exception:
            log.exception("callback: handling %s failed — paysvc will retry",
                          payload.get("orderId"))
            writer.write(_response(500, "Internal Server Error", '{"ok":false}'))
            await _close(writer)
            return

        writer.write(_response(200, "OK", '{"ok":true}'))
        await _close(writer)

    server = await asyncio.start_server(handle, "127.0.0.1", cfg.paid_callback_port,
                                        limit=HEADER_LIMIT)
    log.info("payment callback listening on 127.0.0.1:%s", cfg.paid_callback_port)
    return server


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass
