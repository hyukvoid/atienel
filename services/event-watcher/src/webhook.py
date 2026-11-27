import asyncio
import logging
from collections import OrderedDict

import httpx
from atienel_shared.contracts import Accepted

log = logging.getLogger(__name__)


class Webhook:
    def __init__(self, client, url, token):
        self.client = client
        self.url = url
        self.token = token
        self.queue = asyncio.Queue(maxsize=512)
        self.pending = set()
        self.delivered = OrderedDict()

    async def submit(self, event):
        if event.event_id in self.pending or event.event_id in self.delivered:
            return
        self.pending.add(event.event_id)
        await self.queue.put(event)

    async def run(self):
        while True:
            event = await self.queue.get()
            delivered = await self.send(event)
            self.pending.remove(event.event_id)
            if delivered:
                self.delivered[event.event_id] = None
                if len(self.delivered) > 4096:
                    self.delivered.popitem(last=False)
            self.queue.task_done()

    async def send(self, event):
        body = event.model_dump_json().encode()
        # Include the canonical request hash so alternate observations of one OOM can coalesce.
        key = event.request_hash().hex()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Idempotency-Key": key,
            "Content-Type": "application/json",
        }
        delay = 1
        while True:
            try:
                result = await self.client.post(self.url, headers=headers, content=body)
                if result.status_code == 202:
                    accepted = Accepted.model_validate_json(result.content)
                    log.info(
                        "Accepted status=202 event_id=%s incident_id=%s",
                        event.event_id,
                        accepted.incident_id,
                    )
                    return True
                if result.status_code not in (408, 425, 429) and result.status_code < 500:
                    log.error(
                        "Webhook rejected event_id=%s status=%d", event.event_id, result.status_code
                    )
                    return False
                log.warning(
                    "Webhook retry event_id=%s status=%d", event.event_id, result.status_code
                )
            except (httpx.RequestError, ValueError) as exc:
                log.warning("Webhook retry event_id=%s (%s)", event.event_id, type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
