import asyncio
import logging
import os
import ssl

import httpx

from .http_metrics import poll_metrics
from .observer import SERVICE_ACCOUNT, Observation, watch
from .webhook import Webhook


async def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    observation = Observation()
    ca = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    async with (
        httpx.AsyncClient(
            base_url="https://kubernetes.default.svc", verify=ca, timeout=40, trust_env=False
        ) as kubernetes,
        httpx.AsyncClient(timeout=2, trust_env=False) as internal,
        asyncio.TaskGroup() as tasks,
    ):
        webhook = Webhook(internal, os.environ["INGEST_URL"], os.environ["INGEST_TOKEN"])
        tasks.create_task(watch(kubernetes, "pods", observation.pod, webhook.submit, observation))
        tasks.create_task(
            watch(kubernetes, "events", observation.warning, webhook.submit, observation)
        )
        tasks.create_task(poll_metrics(internal, observation.pods, webhook.submit))
        for _ in range(4):
            tasks.create_task(webhook.run())


if __name__ == "__main__":
    asyncio.run(main())
