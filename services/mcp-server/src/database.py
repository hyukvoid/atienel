import os

from psycopg.conninfo import make_conninfo


def conninfo() -> str:
    return make_conninfo(
        host=os.environ.get("DB_HOST", "postgresql.aiops-data"),
        dbname="atienel",
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=3,
        options="-c statement_timeout=3000",
    )
