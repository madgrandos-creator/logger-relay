import asyncio
import websockets
import json

# A set to store all connected client websockets
# relay_server.py
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
        # Create a list of tasks to send the message to all clients concurrently
        tasks = [client.send(message) for client in CONNECTED_CLIENTS]
        await asyncio.gather(*tasks)

async def handler(websocket, path):
    """
    Handles incoming connections. It registers the client, then listens for messages.
    Any message received is broadcasted to all other clients.
    """
    await register(websocket)
    try:
        # This server's main job is to listen for ONE-WAY data from your PHP script
        # and broadcast it. It also registers/unregisters GUI clients.
        async for message in websocket:
            print(f"Received message, broadcasting to {len(CONNECTED_CLIENTS)} clients...")
            await broadcast(message)
    except websockets.exceptions.ConnectionClosedError:
        pass # Client disconnected normally
    finally:
        await unregister(websocket)

async def main():
    # Start the WebSocket server on all network interfaces (0.0.0.0) on port 8765
    async with websockets.serve(handler, "0.0.0.0", 8765):
        print("WebSocket Relay Server started on ws://0.0.0.0:8765")
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())
