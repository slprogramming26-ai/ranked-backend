from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from .manager import manager


router = APIRouter()


@router.websocket("/ws/chat/{username}")
async def chat(websocket: WebSocket, username: str):
    await manager.connect(websocket)
    await manager.send_to_everyone(f"*** {username} joined the chat ***")
    try:
        while True:
            data = await websocket.receive_text()
            await manager.send_to_everyone(f"{username}: {data}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        await manager.send_to_everyone(f"*** {username} left the chat ***")
