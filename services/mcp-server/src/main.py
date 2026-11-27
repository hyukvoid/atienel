import asyncio
import logging
import os
import ssl
from contextlib import asynccontextmanager

import httpx
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import StaticTokenVerifier
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from starlette.responses import JSONResponse

from .database import conninfo
from .embedding import Embedder
from .k8s_inspector import SERVICE_ACCOUNT, inspect
from .runbook_rag import search
from .schemas import K8sInspectorInput, K8sInspectorOutput, RunbookRagInput, RunbookRagOutput

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_server):
    embedder = await asyncio.to_thread(Embedder)
    pool = AsyncConnectionPool(
        conninfo(),
        kwargs={"row_factory": dict_row},
        min_size=1,
        max_size=4,
        timeout=1,
        open=False,
    )
    ca = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    async with (
        pool,
        httpx.AsyncClient(
            base_url="https://kubernetes.default.svc",
            verify=ca,
            timeout=3,
            trust_env=False,
        ) as kubernetes,
    ):
        yield {
            "kubernetes": kubernetes,
            "pool": pool,
            "embedder": embedder,
            "inference_lock": asyncio.Lock(),
        }


token = os.environ["MCP_TOKEN"]
if not token:
    raise ValueError("MCP_TOKEN must not be empty")
mcp = FastMCP(
    "Atienel read-only evidence",
    auth=StaticTokenVerifier(tokens={token: {"client_id": "aiops-worker", "sub": "aiops-worker"}}),
    lifespan=lifespan,
    mask_error_details=True,
)
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False}


@mcp.tool(annotations=READ_ONLY, timeout=15)
async def k8s_inspector(request: K8sInspectorInput, ctx: Context) -> K8sInspectorOutput:
    """Inspect a target Pod's state, bounded logs and recent events by immutable UID."""
    try:
        return await inspect(request, ctx.lifespan_context["kubernetes"])
    except Exception as exc:
        log.warning("Kubernetes evidence unavailable (%s)", type(exc).__name__)
        raise ToolError("Kubernetes evidence unavailable") from None


@mcp.tool(annotations=READ_ONLY, timeout=10)
async def runbook_rag(request: RunbookRagInput, ctx: Context) -> RunbookRagOutput:
    """Retrieve citable runbook and postmortem excerpts by cosine distance."""
    resources = ctx.lifespan_context
    try:
        return await search(
            request,
            resources["pool"],
            resources["embedder"],
            resources["inference_lock"],
        )
    except Exception as exc:
        log.warning("Runbook retrieval unavailable (%s)", type(exc).__name__)
        raise ToolError("Runbook retrieval unavailable") from None


@mcp.custom_route("/healthz", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/readyz", methods=["GET"])
async def ready(_request):
    # FastMCP completes its lifespan before accepting requests.
    return JSONResponse({"status": "ready"})


app = mcp.http_app(
    path="/mcp",
    stateless_http=True,
    json_response=True,
    allowed_hosts=["mcp-server.aiops-core.svc:*", "127.0.0.1:*"],
)
