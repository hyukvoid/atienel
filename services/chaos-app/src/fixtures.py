import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException

CRASH_FLAG = Path("/fixture/invalid-config")


async def exhaust_memory() -> None:
    await asyncio.sleep(0.2)
    blocks = []
    for _ in range(64):
        blocks.append(bytearray(b"x" * (4 * 1024 * 1024)))
        await asyncio.sleep(0.01)


def fixture_app(mode: str) -> FastAPI:
    app = FastAPI()
    pending: asyncio.Task | None = None

    @app.post("/trigger", status_code=202)
    async def trigger():
        nonlocal pending
        if pending and not pending.done():
            raise HTTPException(409, "Fixture is already running")
        if mode == "oom":
            pending = asyncio.create_task(exhaust_memory())
        else:
            await asyncio.to_thread(CRASH_FLAG.touch)

            async def restart() -> None:
                await asyncio.sleep(0.2)
                os._exit(1)

            pending = asyncio.create_task(restart())
        return {"fixture": mode}

    return app
