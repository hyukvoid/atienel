import asyncio
import logging
import os
import signal
import ssl
from pathlib import Path

from fastmcp.client.transports import StreamableHttpTransport
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from valkey.asyncio import Valkey

from .agent import provider_model
from .consumer import consume, ensure_group, queue_metrics
from .graph import build_graph


async def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    mode = os.environ.get("DIAGNOSIS_MODE", "verification")
    if mode not in ("provider", "verification"):
        raise ValueError("Unknown diagnosis mode")
    model = None
    if mode == "provider":
        model = provider_model(
            os.environ["LLM_MODEL"],
            os.environ["LLM_API_KEY"],
            os.environ["LLM_BASE_URL"],
        )
    ca = ssl.create_default_context(cafile="/mcp-tls/ca.crt")
    transport = StreamableHttpTransport(
        "https://mcp-server.aiops-core.svc:8000/mcp",
        auth=os.environ["MCP_TOKEN"],
        verify=ca,
    )
    pool = AsyncConnectionPool(
        make_conninfo(
            host="postgresql.aiops-data",
            dbname="atienel",
            user="atienel_worker",
            password=os.environ["DB_PASSWORD"],
            connect_timeout=3,
            options="-c statement_timeout=4000",
        ),
        kwargs={"row_factory": dict_row},
        min_size=1,
        max_size=3,
        timeout=2,
        open=False,
    )
    queue = Valkey(
        host="valkey.aiops-data",
        password=os.environ["VALKEY_PASSWORD"],
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with pool, queue:
        graph = build_graph(pool, transport, model)
        await pool.wait(timeout=10)
        await ensure_group(queue)
        await asyncio.to_thread(Path("/tmp/consumer-ready").touch)
        consumer = f"{os.environ['POD_NAME']}:{os.environ['POD_UID']}"
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(queue_metrics(pool, queue, stop))
            tasks.create_task(consume(pool, queue, graph, consumer, stop))


if __name__ == "__main__":
    asyncio.run(main())
