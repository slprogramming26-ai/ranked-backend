import asyncio
from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:8000/ws/chat?token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoiMSIsImV4cCI6MTc4ODAyNjg2N30.ZGkv0zh8TLhkovO3AdaNmKSXKnii3L09R6_LG2pFa38"


N = 2000
BATCH = 10
PAUSE = 1.0


offen = 0
gescheitert = {}

async def hold_one():
    global offen
    for versuch in range(5):
        try:
            async with connect(URL) as ws:
                offen += 1
                await asyncio.Future()
        except ConnectionRefusedError:
            await asyncio.sleep(1 + versuch)
            continue
        except Exception as e:
            name = type(e).__name__
            gescheitert[name] = gescheitert.get(name, 0) + 1
            return
    gescheitert["aufgegeben"] = gescheitert.get("aufgegeben", 0) + 1


async def main():
    tasks = []
    for i in range(N):
        tasks.append(asyncio.create_task(hold_one()))
        if len(tasks) % BATCH == 0:
            await asyncio.sleep(PAUSE)
            print(f"{len(tasks)} Verbindungen gestartet")

    while True:
        await asyncio.sleep(5)
        print(f"offen: {offen} von {N}, gescheitert: {gescheitert}")


asyncio.run(main())

