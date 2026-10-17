"""Webhook receiver: the live path. The bridge POSTs each event here.

Response codes tell the bridge whether to retry:
- 200  accepted, or rejected for good (malformed / ignored): retrying won't help
- 401  wrong or missing X-Bridge-Token (when BRIDGE_TOKEN is configured)
- 413  body larger than MAX_WEBHOOK_BYTES: final, the bridge doesn't retry it
- 500  our own failure (e.g. database locked): retry is safe because inserts are deduped

This path never moves a cursor. Only the backfill worker does, so anything the receiver
missed while it was down is re-fetched from history by gap-fill.
"""
import hmac
import json
import logging
import sqlite3

import uvicorn
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import db
from .config import Config, load_config
from .log import setup_logging
from .models import DEFAULT_FUTURE_TOLERANCE, dated_in_future, normalize_with_reason
from .privacy import safe_meta

log = logging.getLogger(__name__)

# One message event is a few KB; anything far bigger is a bug or an attack, not a message.
MAX_WEBHOOK_BYTES = 1024 * 1024


class _TooLarge(Exception):
    pass


async def _read_capped(request: Request, cap: int) -> bytes:
    """Request body, refusing more than `cap` bytes without buffering the rest.
    Content-Length is checked first, but chunked or lying requests are counted as they stream."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise _TooLarge
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > cap:
            raise _TooLarge
    return bytes(body)


def _ignored(reason: str) -> JSONResponse:
    return JSONResponse({"ok": False, "ignored": reason}, status_code=200)


def create_app(cfg: Config) -> FastAPI:
    # Create the schema once at startup; per-request connections skip the check.
    db.connect(cfg.db_path).close()

    app = FastAPI(title="WhatsApp collector receiver")

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/webhook")
    async def webhook(request: Request):
        if cfg.bridge_token:
            sent = request.headers.get("X-Bridge-Token", "")
            if not hmac.compare_digest(sent.encode(), cfg.bridge_token.encode()):
                log.warning("webhook rejected: bad or missing X-Bridge-Token")
                return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            body = await _read_capped(request, MAX_WEBHOOK_BYTES)
        except _TooLarge:
            log.warning("webhook rejected: body larger than %d bytes", MAX_WEBHOOK_BYTES)
            return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
        # sqlite3 is blocking: run it in a worker thread so the event loop stays free.
        return await run_in_threadpool(store, body)

    def store(body: bytes) -> JSONResponse:
        try:
            conn = db.connect(cfg.db_path, ensure=False)
        except sqlite3.Error:
            log.exception("webhook: cannot open database")
            return JSONResponse({"ok": False, "error": "storage unavailable"}, status_code=500)
        try:
            return handle_event(conn, body, future_tolerance=cfg.future_tolerance,
                                log_identifiers=cfg.log_identifiers)
        except sqlite3.Error:
            log.exception("webhook: database error, asking the bridge to retry")
            return JSONResponse({"ok": False, "error": "storage error"}, status_code=500)
        finally:
            conn.close()

    return app


def handle_event(conn: sqlite3.Connection, body: bytes, *,
                 future_tolerance: float = DEFAULT_FUTURE_TOLERANCE,
                 log_identifiers: str = "masked") -> JSONResponse:
    """Process one webhook body. Raises only sqlite3.Error (-> 500).

    Rejected payloads are described by reason + safe metadata (privacy.safe_meta), never by
    their content: neither the log nor account_events.detail may hold message text.
    """
    try:
        event = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        detail = f"body is not JSON ({len(body)} bytes)"
        log.warning("webhook: %s", detail)
        db.log_event(conn, "malformed_payload", detail)
        return _ignored("not json")
    if not isinstance(event, dict):
        detail = f"body is {type(event).__name__}"
        log.warning("webhook: %s", detail)
        db.log_event(conn, "malformed_payload", detail)
        return _ignored("not an object")

    kind = event.get("event")

    if kind == "state":
        state = str(event.get("state"))
        log.info("bridge state changed: %s", state)
        db.log_event(conn, "state", state)
        return JSONResponse({"ok": True})

    if kind != "message":
        shown = kind if isinstance(kind, str) and len(kind) <= 40 else type(kind).__name__
        log.info("webhook: ignoring unknown event type %r", shown)
        return _ignored("unknown event")

    raw = event.get("message")
    msg, reason = normalize_with_reason(raw, via="webhook", future_tolerance=future_tolerance,
                                        log_identifiers=log_identifiers)
    if msg is None:
        meta = safe_meta(raw, log_identifiers)
        if dated_in_future(raw, future_tolerance):
            db.log_event(conn, "future_timestamp", f"{reason}; {meta}")
            return _ignored("future timestamp")
        db.log_event(conn, "malformed_payload", f"{reason}; {meta}")
        return _ignored("malformed message")

    if msg.chat_type == "direct":
        # Never stored: we collect only groups/channels (minimum needed).
        return _ignored("direct chat")

    # A chat not yet in targets is still a chat the account is a member of (it received
    # the message), so we keep it; the next membership sync turns it into a target.
    target = db.get_target(conn, msg.chat_id)
    if target is not None and not target["enabled"]:
        return _ignored("target disabled")

    inserted = db.insert_messages(conn, [msg.as_row()])
    if inserted:
        log.info("webhook: stored %s in %s", msg.message_id, msg.chat_id)
    else:
        log.info("webhook: duplicate %s in %s", msg.message_id, msg.chat_id)
    return JSONResponse({"ok": True, "inserted": inserted})


def main() -> None:
    cfg = load_config()
    setup_logging("receiver", cfg.log_dir, cfg.log_level)
    log.info("receiver listening on %s:%d, db=%s", cfg.host, cfg.receiver_port, cfg.db_path)
    if not cfg.bridge_token:
        log.warning("BRIDGE_TOKEN is empty: /webhook accepts unauthenticated requests "
                    "(acceptable on loopback for local development only)")
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.receiver_port, log_config=None)


if __name__ == "__main__":
    main()
