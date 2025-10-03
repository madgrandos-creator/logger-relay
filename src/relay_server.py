import asyncio
import websockets
import json
from http import HTTPStatus

# A set to store all connected client websockets
CONNECTED_CLIENTS = set()

async def register(websocket):
    """Adds a new client to the set of connected clients."""
    CONNECTED_CLIENTS.add(websocket)
    print(f"Client connected: {websocket.remote_address}. Total clients: {len(CONNECTED_CLIENTS)}")

async def unregister(websocket):
    """Removes a client from the set."""
    CONNECTED_CLIENTS.remove(websocket)
    print(f"Client disconnected: {websocket.remote_address}. Total clients: {len(CONNECTED_CLIENTS)}")

async def broadcast(message):
    """Sends a message to all connected clients."""
    if CONNECTED_CLIENTS:
        tasks = [client.send(message) for client in CONNECTED_CLIENTS]
        await asyncio.gather(*tasks)

async def handler(websocket, path):
    """
    Handles incoming WebSocket connections.
    """
    await register(websocket)
    try:
        async for message in websocket:
            print(f"Received message, broadcasting to {len(CONNECTED_CLIENTS)} clients...")
            await broadcast(message)
    except websockets.exceptions.ConnectionClosedError:
        pass # Client disconnected normally
    finally:
        await unregister(websocket)

# --- NEW FUNCTION TO HANDLE RENDER'S HEALTH CHECKS ---
async def health_check_handler(path, request_headers):
    """
    This function is called by the server for every incoming HTTP request
    before it attempts to upgrade to a WebSocket connection.
    """
    # Render's health checker sends a HEAD request.
    if request_headers.get("X-Render-Health-Check") == "1" or request_headers.get('User-Agent') == 'Render/1.0':
        # If it's a health check, we return a simple 200 OK response and stop.
        print("Received Render health check. Responding with 200 OK.")
        return HTTPStatus.OK, [], b""

    # If it's not a health check, we return None to let the normal
    # WebSocket connection process continue.
    return None


async def main():
    # Start the WebSocket server, now with the health check handler
    async with websockets.serve(handler, "0.0.0.0", 8765, process_request=health_check_handler):
        print("WebSocket Relay Server started on ws://0.0.0.0:8765")
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())
