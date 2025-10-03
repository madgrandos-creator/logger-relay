#!/usr/bin/env python3
"""
relay_server.py - aiohttp-based WebSocket relay that safely handles Render health checks
and accepts POST requests from a PHP sender.
"""

import os
import asyncio
import logging
import json
from aiohttp import web, WSMsgType

# --- UVLoop Optimization ---
# Try to install uvloop for a performance boost on Linux servers like Render.
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    logging.info("Using uvloop for asyncio event loop.")
except ImportError:
    logging.info("uvloop not found, using standard asyncio event loop.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# A secret token to ensure only your PHP script can send logs.
# For better security, you could set this as an environment variable on Render.
SECRET_TOKEN = "NYXVJcB4xFk-ZG7QeBY-4S9A8TdM5zwvYhUKkB10kLU5bBaYbgEieLVRUEuBifHE"

CONNECTED_CLIENTS: set[web.WebSocketResponse] = set()

async def broadcast(message: str | bytes):
    """Send a message to all connected clients; remove dead ones."""
    if not CONNECTED_CLIENTS:
        logging.info("Broadcast received, but no clients are connected.")
        return
    
    logging.info(f"Broadcasting message to {len(CONNECTED_CLIENTS)} clients.")
    to_remove = []
    for ws in list(CONNECTED_CLIENTS):
        if ws.closed:
            to_remove.append(ws)
            continue
        try:
            if isinstance(message, bytes):
                await ws.send_bytes(message)
            else:
                await ws.send_str(message)
        except Exception as exc:
            logging.warning("Error sending to client %s: %s", ws, exc)
            to_remove.append(ws)
    for ws in to_remove:
        CONNECTED_CLIENTS.discard(ws)

async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """Handles incoming WebSocket connections from the desktop app."""
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    logging.info("Desktop client connected: %s (total %d)", request.remote, len(CONNECTED_CLIENTS) + 1)
    CONNECTED_CLIENTS.add(ws)
    try:
        # We just keep the connection open. This handler doesn't expect to receive messages from the desktop app.
        while not ws.closed:
            await asyncio.sleep(1)
    finally:
        CONNECTED_CLIENTS.discard(ws)
        logging.info("Desktop client disconnected: %s (total %d)", request.remote, len(CONNECTED_CLIENTS))
        await ws.close()
    return ws

# --- NEW: Handler for receiving logs from PHP via POST ---
async def log_receiver_handler(request: web.Request):
    """Accepts POST requests from the PHP script and broadcasts the data."""
    # Security Check: Ensure the request includes the correct secret token.
    if request.headers.get("X-Auth-Token") != SECRET_TOKEN:
        logging.warning("Unauthorized log attempt from IP: %s", request.remote)
        return web.Response(status=403, text="Forbidden")

    try:
        log_data = await request.json()
        log_json = json.dumps(log_data)
        await broadcast(log_json)
        return web.Response(text="OK")
    except json.JSONDecodeError:
        return web.Response(status=400, text="Bad Request: Invalid JSON")
    except Exception as e:
        logging.error("Error in log_receiver_handler: %s", e)
        return web.Response(status=500, text="Internal Server Error")


async def health_check_handler(request: web.Request):
    """Handles Render's health checks."""
    return web.Response(text="OK")

def create_app():
    app = web.Application()
    # Endpoint for the desktop app to connect to
    app.router.add_route("GET", "/", websocket_handler)
    # Endpoint for the PHP script to send data to
    app.router.add_route("POST", "/log", log_receiver_handler)
    # Endpoint for Render's health checks
    app.router.add_route("HEAD", "/health", health_check_handler)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    host = "0.0.0.0"
    logging.info("Starting relay server on %s:%d", host, port)
    app = create_app()
    web.run_app(app, host=host, port=port)
