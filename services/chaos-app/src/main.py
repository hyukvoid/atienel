import os

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .fixtures import CRASH_FLAG, fixture_app

mode = os.environ.get("FIXTURE_MODE")
if mode == "crash" and CRASH_FLAG.exists():
    raise SystemExit("Fixture startup failed: invalid configuration")

app = fixture_app(mode) if mode else FastAPI()
http_5xx = 0


@app.get("/healthz")
async def health():
    return {"status": "ok"}


if mode is None:

    @app.get("/metrics")
    async def metrics():
        return {"pod_uid": os.environ.get("POD_UID"), "http_5xx_total": http_5xx}

    @app.get("/faults/http-500")
    async def http_error():
        global http_5xx
        if os.environ.get("HTTP_FAULT_ENABLED", "true") != "true":
            raise HTTPException(404, "HTTP fault fixture disabled")
        http_5xx += 1
        return JSONResponse({"error": "simulated failure"}, status_code=500)

    @app.post("/faults/{fault}", status_code=202)
    async def trigger(fault: str):
        fixtures = {"oom": "chaos-oom-fixture", "crashloop": "chaos-crash-fixture"}
        if fault not in fixtures:
            raise HTTPException(404, "Unknown fixture")
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                response = await client.post(f"http://{fixtures[fault]}:8080/trigger")
        except httpx.RequestError as exc:
            raise HTTPException(503, "Fixture unavailable") from exc
        if response.status_code != 202:
            raise HTTPException(response.status_code, "Fixture rejected the trigger")
        return response.json()
