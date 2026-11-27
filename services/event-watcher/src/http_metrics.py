import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from atienel_shared.contracts import IncidentEvent, IncidentType

log = logging.getLogger(__name__)


@dataclass
class Window:
    last_count: int
    last_time: int
    increases: deque[tuple[int, int]] = field(default_factory=deque)
    active: bool = False

    def record(self, now, count):
        if count < self.last_count or now - self.last_time > 10:
            self.increases.clear()
            self.active = False
        elif count > self.last_count:
            self.increases.append((self.last_time, count - self.last_count))
        self.last_time, self.last_count = now, count
        while self.increases and self.increases[0][0] < now - 30:
            self.increases.popleft()
        active = sum(delta for _, delta in self.increases) >= 10
        started = active and not self.active
        self.active = active
        return self.increases[0][0] if started else None


async def poll_metrics(client, pods, submit):
    windows = {}

    async def sample(uid, pod):
        address = pod["status"]["podIP"]
        host = f"[{address}]" if ":" in address else address
        try:
            response = await client.get(f"http://{host}:8080/metrics")
            response.raise_for_status()
            result = response.json()
            count = result["http_5xx_total"]
            if result["pod_uid"] != uid or type(count) is not int or count < 0:
                raise ValueError("Invalid metrics")
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            if windows.pop(uid, None) is not None:
                log.warning("HTTP 5xx observation unavailable for pod_uid=%s", uid)
            return
        now = int(datetime.now(UTC).timestamp())
        window = windows.setdefault(uid, Window(count, now))
        start = window.record(now, count)
        if start is not None:
            await submit(
                IncidentEvent(
                    event_id=f"http:{uid}:{start}",
                    namespace="target-apps",
                    pod_name=pod["metadata"]["name"],
                    pod_uid=uid,
                    incident_type=IncidentType.HTTP,
                    observed_at=datetime.fromtimestamp(now, UTC),
                    source="http_metrics",
                    summary="At least 10 HTTP 5xx responses within 30 seconds",
                )
            )

    while True:
        targets = {
            uid: pod
            for uid, pod in pods.items()
            if pod["metadata"].get("labels", {}).get("atienel.io/http-metrics") == "true"
            and pod.get("status", {}).get("podIP")
        }
        for uid in windows.keys() - targets.keys():
            del windows[uid]
        await asyncio.gather(*(sample(uid, pod) for uid, pod in targets.items()))
        await asyncio.sleep(5)
