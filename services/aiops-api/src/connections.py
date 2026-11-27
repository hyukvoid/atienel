import os

from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from valkey.asyncio import Valkey


def connections():
    pool = AsyncConnectionPool(
        make_conninfo(
            host=os.environ.get("DB_HOST", "postgresql.aiops-data"),
            dbname="atienel",
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            connect_timeout=2,
        ),
        kwargs={"row_factory": dict_row},
        min_size=1,
        max_size=8,
        timeout=0.5,
        open=False,
    )
    queue = Valkey(
        host=os.environ.get("VALKEY_HOST", "valkey.aiops-data"),
        password=os.environ["VALKEY_PASSWORD"],
        decode_responses=True,
        socket_connect_timeout=0.15,
        socket_timeout=0.15,
    )
    return pool, queue
