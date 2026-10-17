"""Webhook receiver: the live path. The bridge POSTs each event here.

Response codes tell the bridge whether to retry:
- 200  accepted, or rejected for good (malformed / ignored): retrying won't help
- 401  wrong or missing X-Bridge-Token (when BRIDGE_TOKEN is configured)
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
from .models import normalize

log = logging.getLogger(__name__)


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
        body = await request.body()
        # sqlite3 is blocking: run it in a worker thread so the event loop stays free.
        return await run_in_threadpool(store, body)

    def store(body: bytes) -> JSONResponse:
        try:
            conn = db.connect(cfg.db_path, ensure=False)
        except sqlite3.Error:
            log.exception("webhook: cannot open database")
            return JSONResponse({"ok": False, "error": "storage unavailable"}, status_code=500)
        try:
            return handle_event(conn, body)
        except sqlite3.Error:
            log.exception("webhook: database error, asking the bridge to retry")
            return JSONResponse({"ok": False, "error": "storage error"}, status_code=500)
        finally:
            conn.close()

    return app


def handle_event(conn: sqlite3.Connection, body: bytes) -> JSONResponse:
    """Process one webhook body. Raises only sqlite3.Error (-> 500)."""
    try:
        event = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        log.warning("webhook: body is not JSON: %.200r", body)
        db.log_event(conn, "malformed_payload", "body is not JSON")
        return _ignored("not json")
    if not isinstance(event, dict):
        log.warning("webhook: body is not an object: %.200r", event)
        db.log_event(conn, "malformed_payload", f"body is {type(event).__name__}")
        return _ignored("not an object")

    kind = event.get("event")

    if kind == "state":
        state = str(event.get("state"))
        log.info("bridge state changed: %s", state)
        db.log_event(conn, "state", state)
        return JSONResponse({"ok": True})

    if kind != "message":
        log.info("webhook: ignoring unknown event type %r", kind)
        return _ignored("unknown event")

    msg = normalize(event.get("message"), via="webhook")
    if msg is None:
        db.log_event(conn, "malformed_payload", f"{str(event)[:200]}")
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
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.receiver_port, log_config=None)


if __name__ == "__main__":
    main()
