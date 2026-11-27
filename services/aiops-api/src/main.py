import asyncio
import logging
from contextlib import asynccontextmanager

import psycopg
from atienel_shared.contracts import ErrorDetail, ErrorResponse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from valkey.exceptions import ValkeyError

from .auth import load_tokens
from .connections import connections
from .incident_routes import ApiError, router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.tokens = load_tokens()
    pool, queue = connections()
    async with pool, queue:
        app.state.pool, app.state.queue = pool, queue
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(router)


@app.exception_handler(ApiError)
async def api_error(_request: Request, exc: ApiError):
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
    body = ErrorResponse(error=ErrorDetail(code=exc.code, message=exc.message))
    return JSONResponse(body.model_dump(), status_code=exc.status, headers=headers)


@app.exception_handler(psycopg.Error)
@app.exception_handler(ValkeyError)
@app.exception_handler(TimeoutError)
async def dependency_error(_request: Request, exc: Exception):
    log.warning("Hot Path dependency unavailable (%s)", type(exc).__name__)
    body = ErrorResponse(
        error=ErrorDetail(code="UNAVAILABLE", message="Storage or publication unavailable")
    )
    return JSONResponse(body.model_dump(), status_code=503, headers={"Retry-After": "1"})


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.get("/readyz")
async def ready(request: Request):
    async with asyncio.timeout(0.8):
        async with request.app.state.pool.connection() as conn:
            await conn.execute("SELECT 1")
        await request.app.state.queue.ping()
    return {"status": "ready"}
