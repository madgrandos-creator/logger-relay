#!/usr/bin/env python3
"""
relay_server.py - aiohttp-based WebSocket relay that safely handles Render health checks.

Dependencies:
  pip install aiohttp

Usage on Render:
  - Start command: python3 relay_server.py
  - Ensure the service's PORT env var (Render sets it automatically).
  - Optionally set Render health check path to "/" (default) or "/health".
"""

import os
import asyncio
import logging
import json
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Keep track of connected WebSocketResponse objects
CONNECTED_CLIENTS: set[web.WebSocketResponse] = set()

async def broadcast(message: str | bytes):
    """Send a message to all connected clients; remove dead ones."""
    if not CONNECTED_CLIENTS:
        return
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
    """
    Accept and handle a single websocket connection. This function
    returns the WebSocketResponse to aiohttp.
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    logging.info("Client connected: %s (total %d)", request.remote, len(CONNECTED_CLIENTS) + 1)
    CONNECTED_CLIENTS.add(ws)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # you can validate/transform the message here if needed
                logging.debug("Received text message from %s: %s", request.remote, msg.data)
                await broadcast(msg.data)
            elif msg.type == WSMsgType.BINARY:
                logging.debug("Received binary message from %s (%d bytes)", request.remote, len(msg.data))
                await broadcast(msg.data)
            elif msg.type == WSMsgType.ERROR:
                logging.warning("WS connection error from %s: %s", request.remote, ws.exception())
    finally:
        CONNECTED_CLIENTS.discard(ws)
        logging.info("Client disconnected: %s (total %d)", request.remote, len(CONNECTED_CLIENTS))
        await ws.close()
    return ws

# Health check handler (responds 200 OK for HEAD and non-upgrade GETs)
async def root_handler(request: web.Request):
    # If this is a GET and a websocket upgrade is requested, perform WS handshake
    upgrade_hdr = request.headers.get("Upgrade", "")
    if request.method == "GET" and "websocket" in upgrade_hdr.lower():
        return await websocket_handler(request)

    # Otherwise treat as a health check / normal HTTP request
    # HEAD requests will automatically receive the same status without body.
    logging.debug("Health check from %s (%s %s)", request.remote, request.method, request.path)
    return web.Response(text="OK")

# Explicit websocket path
async def ws_path_handler(request: web.Request):
    return await websocket_handler(request)

async def on_shutdown(app):
    logging.info("Shutting down: closing %d websockets", len(CONNECTED_CLIENTS))
    tasks = []
    for ws in list(CONNECTED_CLIENTS):
        tasks.append(ws.close(code=1001, message=b"server shutdown"))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    CONNECTED_CLIENTS.clear()

def create_app():
    app = web.Application()
    # Root accepts health checks and websocket upgrades.
    app.router.add_route("GET", "/", root_handler)
    app.router.add_route("HEAD", "/", root_handler)
    # Explicit websocket endpoint (for clients that expect /ws)
    app.router.add_route("GET", "/ws", ws_path_handler)
    # Add shutdown cleanup
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", os.environ.get("RENDER_PORT", 8765)))
    host = "0.0.0.0"

    logging.info("Starting relay server on %s:%d", host, port)
    app = create_app()
    # web.run_app handles signals and graceful shutdown on Render
    web.run_app(app, host=host, port=port)
