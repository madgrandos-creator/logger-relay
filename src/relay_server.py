#!/usr/bin/env python3
"""
relay_server.py - aiohttp-based WebSocket relay that:
  - safely handles Render health checks (HEAD /)
  - accepts POST /log from a PHP sender (authorized via X-Auth-Token)
  - decouples incoming POSTs from websocket sending using an asyncio queue
  - uses uvloop if available for better async performance
  - provides graceful shutdown and robust error handling

Important:
  - This file expects the secret to be available as the env var RELAY_SHARED_SECRET.
  - If not set, it falls back to the literal below so it matches your PHP sender example.
  - DO NOT commit a real secret into a public repo. Prefer Render secrets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
from typing import Set

from aiohttp import web, WSMsgType

# ---------- Configuration ----------
# Default fallback secret (MUST match your PHP sender if you use the literal).
# You can & should set RELAY_SHARED_SECRET in Render's "Environment" -> "Secrets".
SECRET_TOKEN = os.getenv(
    "RELAY_SHARED_SECRET",
    "NYXVJcB4xFk-ZG7QeBY-4S9A8TdM5zwvYhUKkB10kLU5bBaYbgEieLVRUEuBifHE",
)

# Queue size controls how many incoming POSTs we buffer before rejecting.
BROADCAST_QUEUE_MAXSIZE = int(os.getenv("BROADCAST_QUEUE_MAXSIZE", "1000"))

# Per-send timeout so a stuck client won't block the whole broadcast worker.
PER_CLIENT_SEND_TIMEOUT = float(os.getenv("PER_CLIENT_SEND_TIMEOUT", "5.0"))

# Logging level
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Port (Render exposes PORT env var)
PORT = int(os.environ.get("PORT", os.environ.get("RENDER_PORT", 8765)))
HOST = "0.0.0.0"

# ---------- Setup ----------
logging.basicConfig(
    level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("relay_server")

# Try to use uvloop for better performance on Unix systems
try:
    import uvloop  # type: ignore

    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    logger.info("uvloop loaded; using for asyncio event loop.")
except Exception:
    logger.debug("uvloop not available; using default asyncio event loop.")

# Keep track of connected websocket clients
CONNECTED_CLIENTS: Set[web.WebSocketResponse] = set()

# Async queue to decouple HTTP POST handling from websocket sends
broadcast_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=BROADCAST_QUEUE_MAXSIZE)


# ---------- Utilities ----------
def is_authorized_token(token: str | None) -> bool:
    """Timing-safe compare of tokens."""
    if not token:
        return False
    try:
        return secrets.compare_digest(token, SECRET_TOKEN)
    except Exception:
        # On any unexpected error, deny authorization.
        return False


async def safe_send(ws: web.WebSocketResponse, payload: str | bytes) -> bool:
    """
    Send payload to a single websocket with a timeout.
    Returns True on success, False on failure (and the caller should remove the ws).
    """
    try:
        if isinstance(payload, (bytes, bytearray)):
            coro = ws.send_bytes(bytes(payload))
        else:
            coro = ws.send_str(str(payload))
        await asyncio.wait_for(coro, timeout=PER_CLIENT_SEND_TIMEOUT)
        return True
    except Exception as exc:
        logger.debug("safe_send failed for client %s: %s", getattr(ws, "remote", "<unknown>"), exc)
        return False


# ---------- Broadcast worker ----------
async def broadcaster_worker(app: web.Application):
    """
    Background task that consumes `broadcast_queue` and sends messages to all connected websockets.
    Using a dedicated worker makes POST /log fast (it only enqueues) and avoids blocking HTTP request handling.
    """
    logger.info("Broadcaster worker started.")
    try:
        while True:
            payload = await broadcast_queue.get()
            if payload is None:  # sentinel for shutdown (type: ignore)
                logger.info("Broadcaster worker received shutdown sentinel.")
                break

            # Capture snapshot of clients to avoid mutation while sending
            clients = list(CONNECTED_CLIENTS)
            if not clients:
                logger.debug("No connected clients while processing broadcast: dropping message.")
                broadcast_queue.task_done()
                continue

            logger.info("Broadcasting message to %d clients (queue size: %d).", len(clients), broadcast_queue.qsize())

            # Launch sends concurrently and wait for them all (but not indefinitely)
            send_tasks = [safe_send(ws, payload) for ws in clients]
            results = await asyncio.gather(*send_tasks, return_exceptions=False)

            # Remove clients that failed sends or are closed
            removed = 0
            for ws, ok in zip(clients, results):
                if not ok or ws.closed:
                    try:
                        CONNECTED_CLIENTS.discard(ws)
                        await ws.close(code=1011, message=b"server: removing dead client")
                    except Exception:
                        pass
                    removed += 1
            if removed:
                logger.info("Removed %d dead/failed clients during broadcast.", removed)

            broadcast_queue.task_done()
    except asyncio.CancelledError:
        logger.info("Broadcaster worker cancelled.")
    except Exception as exc:
        logger.exception("Unexpected error in broadcaster worker: %s", exc)
    finally:
        logger.info("Broadcaster worker stopped.")


# ---------- HTTP / WebSocket handlers ----------
async def root_handler(request: web.Request):
    """
    Root path:
      - Respond to HEAD/GET as health-check when not an Upgrade request.
      - If it's a GET with Upgrade: websocket, upgrade and handle websocket.
    """
    upgrade_hdr = request.headers.get("Upgrade", "").lower()
    if request.method == "GET" and "websocket" in upgrade_hdr:
        # delegate to websocket handler
        return await websocket_handler(request)

    # For health checks (HEAD or non-upgrade GET), return OK and small body.
    return web.Response(text="OK")


async def websocket_handler(request: web.Request) -> web.StreamResponse:
    """
    Accept a websocket connection and keep it in CONNECTED_CLIENTS.
    This endpoint is used for clients connecting via the root path or /ws.
    """
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=2 ** 20)  # 1MB max message size
    await ws.prepare(request)

    # store and log
    CONNECTED_CLIENTS.add(ws)
    client_id = getattr(ws, "remote", request.remote)
    logger.info("WebSocket client connected: %s (total %d)", client_id, len(CONNECTED_CLIENTS))

    try:
        async for msg in ws:
            # Server only relays incoming POST logs to connected clients in this design.
            # However, if the websocket client sends something, we can optionally log it.
            if msg.type == WSMsgType.TEXT:
                logger.debug("Received text from client %s: %s", client_id, msg.data)
            elif msg.type == WSMsgType.BINARY:
                logger.debug("Received binary from client %s (%d bytes).", client_id, len(msg.data))
            elif msg.type == WSMsgType.ERROR:
                logger.warning("WebSocket connection error from %s: %s", client_id, ws.exception())
    finally:
        CONNECTED_CLIENTS.discard(ws)
        try:
            await ws.close()
        except Exception:
            pass
        logger.info("WebSocket client disconnected: %s (total %d)", client_id, len(CONNECTED_CLIENTS))

    return ws


async def post_log_handler(request: web.Request) -> web.Response:
    """
    Accept POST /log from your PHP sender.
    Expects:
      - Header X-Auth-Token with the shared secret (timing-safe compare)
      - JSON body (will be enqueued and broadcast asynchronously)
    """
    token = request.headers.get("X-Auth-Token")
    if not is_authorized_token(token):
        logger.warning("Unauthorized attempt to POST /log from %s", request.remote)
        # Use 403 rather than 401 because no WWW-Authenticate is being offered.
        return web.Response(status=403, text="Forbidden")

    # Fast accept: parse body and enqueue
    try:
        # Using await request.read() and manual json.loads is slightly faster in some cases,
        # but aiohttp's request.json() is fine and clearer:
        payload = await request.json()
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in POST /log from %s", request.remote)
        return web.Response(status=400, text="Bad Request: invalid JSON")
    except Exception as exc:
        logger.exception("Error while parsing JSON in POST /log: %s", exc)
        return web.Response(status=400, text="Bad Request")

    # Convert to canonical JSON string (small optimization: ensure compact representation)
    try:
        payload_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        payload_text = str(payload)

    # Try to enqueue without blocking: if full, return 503 to indicate overload/backpressure
    try:
        broadcast_queue.put_nowait(payload_text)
        logger.debug("Enqueued message from %s (queue size %d).", request.remote, broadcast_queue.qsize())
        # 202 Accepted is appropriate because we queued it for async processing.
        return web.Response(status=202, text="Accepted")
    except asyncio.QueueFull:
        logger.warning("Broadcast queue full; rejecting POST /log from %s", request.remote)
        return web.Response(status=503, text="Service Unavailable: queue full")


# ---------- Application startup/shutdown hooks ----------
async def on_startup(app: web.Application):
    # start broadcaster worker
    app["broadcaster_task"] = asyncio.create_task(broadcaster_worker(app))
    logger.info("Application startup complete. Broadcaster task created.")


async def on_cleanup(app: web.Application):
    # Send sentinel to worker then cancel/await it
    logger.info("Application cleanup starting. Signaling broadcaster worker to stop.")
    try:
        # put sentinel if possible (non-blocking)
        try:
            broadcast_queue.put_nowait(None)  # type: ignore[arg-type]
        except Exception:
            # If queue full or otherwise, cancel the task instead
            pass

        task = app.get("broadcaster_task")
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.debug("Broadcaster worker cancelled in cleanup.")
    except Exception as exc:
        logger.exception("Error during cleanup: %s", exc)

    # Close all websockets
    logger.info("Closing %d connected websocket clients.", len(CONNECTED_CLIENTS))
    for ws in list(CONNECTED_CLIENTS):
        try:
            await ws.close(code=1001, message=b"server shutdown")
        except Exception:
            pass
    CONNECTED_CLIENTS.clear()
    logger.info("Cleanup complete.")


def create_app() -> web.Application:
    app = web.Application()
    # Root accepts health checks and websocket upgrades.
    app.router.add_route("GET", "/", root_handler)
    app.router.add_route("HEAD", "/", root_handler)
    # explicit websocket path (if clients prefer /ws)
    app.router.add_route("GET", "/ws", websocket_handler)
    # POST log endpoint for PHP sender
    app.router.add_route("POST", "/log", post_log_handler)

    # lifecycle hooks
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


# ---------- Run ----------
if __name__ == "__main__":
    # Install signal handlers for graceful shutdown on platforms that support it
    loop = asyncio.get_event_loop()

    def _shutdown():
        logger.info("Received shutdown signal.")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # add_signal_handler may not be implemented on Windows event loop policy
            pass

    app = create_app()
    logger.info("Starting relay server on %s:%d (LOG_LEVEL=%s)", HOST, PORT, LOG_LEVEL)
    web.run_app(app, host=HOST, port=PORT)
