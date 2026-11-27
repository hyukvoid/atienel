import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
from atienel_shared.contracts import IncidentEvent, IncidentType
from pydantic import ValidationError

NAMESPACE = "target-apps"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
log = logging.getLogger(__name__)


def epoch_ns(timestamp: str) -> int:
    base, fraction, zone = re.fullmatch(
        r"(.*T\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", timestamp
    ).groups()
    delta = datetime.fromisoformat(base + zone) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 10**9 + int((fraction or "").ljust(9, "0"))


def terminations(pod):
    status = pod.get("status", {})
    for group in ("containerStatuses", "initContainerStatuses", "ephemeralContainerStatuses"):
        for container in status.get(group, []):
            for state in ("state", "lastState"):
                term = container.get(state, {}).get("terminated", {})
                if term.get("reason") == "OOMKilled" and term.get("finishedAt"):
                    yield container["name"], term["finishedAt"]


def oom_event(uid, name, container, finished_at, source="pod_status"):
    return IncidentEvent(
        event_id=f"oom:{uid}:{container}:{epoch_ns(finished_at)}",
        namespace=NAMESPACE,
        pod_name=name,
        pod_uid=uid,
        container_name=container,
        incident_type=IncidentType.OOM,
        observed_at=finished_at,
        source=source,
        summary="Container terminated with reason OOMKilled",
    )


class Observation:
    def __init__(self):
        self.pods = {}
        self.pods_listed = asyncio.Event()

    def pod(self, action, pod):
        metadata = pod["metadata"]
        uid = metadata["uid"]
        if action == "DELETED":
            self.pods.pop(uid, None)
        else:
            self.pods[uid] = pod
        for container, finished_at in terminations(pod):
            yield oom_event(uid, metadata["name"], container, finished_at)

    def warning(self, _action, event):
        target = event.get("involvedObject", {})
        if (
            event.get("type") != "Warning"
            or target.get("kind") != "Pod"
            or target.get("namespace") != NAMESPACE
            or not target.get("uid")
        ):
            return
        uid, name = target["uid"], target["name"]
        field = re.fullmatch(
            r"spec\.(?:containers|initContainers|ephemeralContainers)\{([^}]+)\}",
            target.get("fieldPath", ""),
        )
        container = field[1] if field else None
        observed_at = (
            event.get("firstTimestamp")
            or event.get("eventTime")
            or event["metadata"]["creationTimestamp"]
        )
        reason = event.get("reason", "")
        message = event.get("message", "")
        if reason in ("OOMKilled", "OOMKilling") and container and uid in self.pods:
            for candidate, finished_at in terminations(self.pods[uid]):
                if candidate == container and epoch_ns(observed_at) == epoch_ns(finished_at):
                    yield oom_event(uid, name, container, finished_at, "kubernetes_event")
                    return
        crash = reason == "CrashLoopBackOff" or (
            reason == "BackOff" and "restarting failed container" in message.lower()
        )
        yield IncidentEvent(
            event_id=f"k8s:{event['metadata']['uid']}",
            namespace=NAMESPACE,
            pod_name=name,
            pod_uid=uid,
            container_name=container,
            incident_type=IncidentType.CRASH if crash else IncidentType.WARNING,
            observed_at=observed_at,
            source="kubernetes_event",
            summary=(message or reason or "Kubernetes Warning")[:2048],
        )


class Relist(Exception):
    pass


async def watch(client, resource, handle, submit, observation):
    url = f"/api/v1/namespaces/{NAMESPACE}/{resource}"
    version = None
    delay = 1
    if resource == "events":
        await observation.pods_listed.wait()
    while True:
        try:
            token = (SERVICE_ACCOUNT / "token").read_text().strip()
            headers = {"Authorization": f"Bearer {token}"}
            if version is None:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
                if resource == "pods":
                    observation.pods.clear()
                for item in data["items"]:
                    await observe(handle, submit, "ADDED", item)
                version = data["metadata"]["resourceVersion"]
                if resource == "pods":
                    observation.pods_listed.set()
            params = {
                "watch": "true",
                "timeoutSeconds": 30,
                "allowWatchBookmarks": "true",
                "resourceVersion": version,
            }
            async with client.stream("GET", url, params=params, headers=headers) as response:
                if response.status_code == 410:
                    raise Relist
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    action, item = data["type"], data["object"]
                    if action == "ERROR":
                        if item.get("code") == 410:
                            raise Relist
                        raise ValueError("Kubernetes watch error")
                    if action != "BOOKMARK":
                        await observe(handle, submit, action, item)
                    version = item["metadata"]["resourceVersion"]
            delay = 1
        except Relist:
            version = None
        except (httpx.HTTPError, KeyError, ValueError, OSError) as exc:
            log.warning("%s watch reconnecting (%s)", resource, type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


async def observe(handle, submit, action, item):
    try:
        for event in handle(action, item):
            await submit(event)
    except (ValidationError, AttributeError, OverflowError, ValueError):
        log.warning("Ignoring invalid Kubernetes observation")
