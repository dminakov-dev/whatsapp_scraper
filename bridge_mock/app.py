"""Mock bridge HTTP app: the bridge contract from collector/models.py, with fault injection.

Faults are drawn once per request so their rates are independent and reproducible
from the seed. /health has no random faults: it only reflects the account state
(auth_fail), like the real bridge's client state.
"""
import asyncio
import json
import logging
import random
import time
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .settings import MockSettings
from .world import World

log = logging.getLogger("bridge_mock")


def _malformed_payloads(valid_msg: dict) -> list:
    """Broken webhook bodies the receiver must survive."""
    return [
        {"event": "message"},                                        # no message
        {"event": "message", "message": {"id": "x"}},                # missing fields
        {"event": "message", "message": {**valid_msg, "timestamp": valid_msg["timestamp"] * 1000}},
        {"event": "message", "message": "not an object"},
        {"event": "who_knows", "data": 1},                           # unknown event
        [],                                                          # not an object at all
        "this is not json",                                          # sent as raw text
    ]


def create_app(settings: MockSettings, world: World | None = None) -> FastAPI:
    world = world or World(settings.seed, settings.window, settings.history)
    faults = settings.faults
    fault_rng = random.Random(settings.seed + 1)
    stats = {"webhook_sent": 0, "webhook_failed": 0, "duplicates": 0, "malformed": 0}

    async def post_webhook(client: httpx.AsyncClient, body) -> None:
        headers = {"Content-Type": "application/json"}
        if settings.bridge_token:
            headers["X-Bridge-Token"] = settings.bridge_token
        content = body if isinstance(body, str) else json.dumps(body)
        try:
            resp = await client.post(settings.webhook_url, content=content, headers=headers)
            stats["webhook_sent"] += 1
            if resp.status_code >= 400:
                log.info("webhook answered %s", resp.status_code)
        except httpx.HTTPError as exc:
            # Like the real bridge: an event pushed while the receiver is down is lost.
            # Backfill gap-fill is what recovers it from history.
            stats["webhook_failed"] += 1
            log.warning("webhook delivery failed, event lost: %s", type(exc).__name__)

    async def emit_once(client: httpx.AsyncClient) -> None:
        if faults.auth_fail:
            return  # an unpaired account receives nothing
        for msg in world.emit(int(time.time())):
            await post_webhook(client, {"event": "message", "message": msg})
            if fault_rng.random() < faults.duplicate:
                stats["duplicates"] += 1
                await post_webhook(client, {"event": "message", "message": msg})
            if fault_rng.random() < faults.malformed:
                stats["malformed"] += 1
                await post_webhook(client, fault_rng.choice(_malformed_payloads(msg)))

    async def emitter() -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                await asyncio.sleep(settings.emit_interval)
                try:
                    await emit_once(client)
                except Exception:
                    log.exception("emitter iteration failed")

    async def send_state(state: str) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await post_webhook(client, {"event": "state", "state": state})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(emitter()) if settings.emit_interval > 0 else None
        log.info("mock bridge up: %d chats, window=%d, webhook=%s",
                 len(world.chats), settings.window, settings.webhook_url)
        yield
        if task:
            task.cancel()

    app = FastAPI(title="WhatsApp bridge mock", lifespan=lifespan)
    app.state.world = world
    app.state.faults = faults
    app.state.stats = stats
    app.state.emit_once = emit_once

    async def injected_fault() -> JSONResponse | None:
        if faults.auth_fail:
            return JSONResponse({"error": "session unpaired"}, status_code=401)
        r = fault_rng.random()
        if r < faults.fail_5xx:
            return JSONResponse({"error": "bridge unavailable"}, status_code=503)
        r -= faults.fail_5xx
        if r < faults.fail_409:
            return JSONResponse({"error": "client busy, try again"}, status_code=409)
        r -= faults.fail_409
        if r < faults.fail_429:
            return JSONResponse({"error": "rate limited"}, status_code=429,
                                headers={"Retry-After": str(faults.retry_after)})
        r -= faults.fail_429
        if r < faults.slow:
            await asyncio.sleep(faults.slow_seconds)
        return None

    @app.get("/health")
    async def health():
        if faults.auth_fail:
            return JSONResponse({"ok": False, "state": "UNPAIRED"}, status_code=401)
        return {"ok": True, "state": "CONNECTED"}

    @app.get("/chats")
    async def chats():
        if (fault := await injected_fault()) is not None:
            return fault
        return {"chats": [{"chat_id": c.chat_id, "name": c.name} for c in world.chats.values()]}

    @app.get("/chats/{chat_id}/messages")
    async def messages(chat_id: str, since: int = Query(0, ge=0),
                       limit: int = Query(50, ge=1, le=500)):
        if (fault := await injected_fault()) is not None:
            return fault
        chat = world.chats.get(chat_id)
        if chat is None or chat.dead:
            return JSONResponse({"error": "chat not found"}, status_code=404)
        msgs, oldest, truncated = world.history(chat_id, since, limit)
        return {"messages": msgs, "oldest_available": oldest, "truncated": truncated}

    # --- test/demo controls (bound to 127.0.0.1 like everything else) ------------------

    @app.get("/_admin/faults")
    async def get_faults():
        return asdict(faults)

    @app.post("/_admin/faults")
    async def set_faults(request: Request):
        changes = await request.json()
        was_unpaired = faults.auth_fail
        for key, value in changes.items():
            if hasattr(faults, key):
                setattr(faults, key, type(getattr(faults, key))(value))
        if faults.auth_fail != was_unpaired:
            await send_state("UNPAIRED" if faults.auth_fail else "CONNECTED")
        log.info("faults updated: %s", changes)
        return asdict(faults)

    @app.get("/_admin/stats")
    async def get_stats():
        return {**stats, "messages": {c.chat_id: len(c.messages) for c in world.chats.values()}}

    return app
