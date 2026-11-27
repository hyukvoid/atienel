import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from .schemas import (
    ContainerEvidence,
    EventEvidence,
    EvidenceGap,
    K8sInspectorInput,
    K8sInspectorOutput,
    LogEvidence,
    Termination,
)

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
MAX_LOG_BYTES = 8192
GROUPS = (
    ("app", "containers", "containerStatuses"),
    ("init", "initContainers", "initContainerStatuses"),
    ("ephemeral", "ephemeralContainers", "ephemeralContainerStatuses"),
)
CREDENTIAL = re.compile(
    r"""(?i)(bearer\s+|["']?(?:password|passwd|token|secret|api[-_]?key)["']?\s*[:=]\s*)"""
    r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)"""
)
URI_CREDENTIAL = re.compile(r"(://[^\s:/@]+:)[^\s/@]+(@)")


def redact(text: str) -> str:
    text = CREDENTIAL.sub(r"\1[REDACTED]", text)
    return URI_CREDENTIAL.sub(r"\1[REDACTED]\2", text)


def timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def container_evidence(pod):
    for kind, spec_key, status_key in GROUPS:
        statuses = {s["name"]: s for s in pod.get("status", {}).get(status_key, ())}
        for definition in pod["spec"].get(spec_key, ()):
            name = definition["name"]
            status = statuses.get(name, {})
            state = status.get("state", {})
            terminated = state.get("terminated") or status.get("lastState", {}).get("terminated")
            term = None
            if terminated:
                term = Termination(
                    reason=terminated.get("reason"),
                    exit_code=terminated["exitCode"],
                    finished_at=timestamp(terminated.get("finishedAt")),
                )
            yield ContainerEvidence(
                name=name,
                kind=kind,
                restart_count=status.get("restartCount"),
                waiting_reason=state.get("waiting", {}).get("reason"),
                termination_state=(
                    "observed" if term else "not_applicable" if status else "unavailable"
                ),
                termination=term,
                limits=definition.get("resources", {}).get("limits", {}),
            )


async def get_pod(client, path, headers):
    response = await client.get(path, headers=headers)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


async def events_for(client, root, headers, uid, cutoff):
    params = {"fieldSelector": f"involvedObject.uid={uid}", "limit": "100"}
    events = []
    while True:
        result = await client.get(f"{root}/events", headers=headers, params=params)
        result.raise_for_status()
        page = result.json()
        for event in page["items"]:
            if event["involvedObject"]["uid"] != str(uid):
                continue
            at = timestamp(
                event.get("series", {}).get("lastObservedTime")
                or event.get("lastTimestamp")
                or event.get("eventTime")
                or event["metadata"]["creationTimestamp"]
            )
            if at and at >= cutoff:
                events.append(
                    EventEvidence(
                        uid=UUID(event["metadata"]["uid"]),
                        type=event.get("type", ""),
                        reason=event.get("reason", ""),
                        message=redact(event.get("message", ""))[:512],
                        observed_at=at,
                    )
                )
        events = sorted(events, key=lambda event: (event.observed_at, event.uid), reverse=True)[:20]
        continuation = page["metadata"].get("continue")
        if not continuation:
            return events
        params["continue"] = continuation


async def read_log(client, path, headers, request, container, previous):
    scope = "logs_previous" if previous else "logs_current"
    async with client.stream(
        "GET",
        f"{path}/log",
        headers=headers,
        params={
            "container": container,
            "previous": str(previous).lower(),
            "tailLines": request.tail_lines,
            "sinceSeconds": request.lookback_seconds,
            "limitBytes": MAX_LOG_BYTES + 1,
        },
    ) as response:
        if response.status_code in (400, 404):
            return EvidenceGap(scope=scope, code="no_retained_data", container=container)
        response.raise_for_status()
        content = bytearray()
        async for block in response.aiter_bytes(chunk_size=MAX_LOG_BYTES + 1):
            content.extend(block[: MAX_LOG_BYTES + 1 - len(content)])
            if len(content) > MAX_LOG_BYTES:
                break
    # Redact the lookahead too so a credential crossing the byte boundary stays masked.
    safe = redact(content.decode("utf-8", errors="replace")).encode()
    return LogEvidence(
        container=container,
        instance="previous" if previous else "current",
        text=safe[:MAX_LOG_BYTES].decode(errors="ignore"),
        truncated=len(content) > MAX_LOG_BYTES or len(safe) > MAX_LOG_BYTES,
    )


def pod_gap(pod, expected):
    if pod is None:
        return EvidenceGap(scope="pod", code="pod_not_found")
    if UUID(pod["metadata"]["uid"]) != expected:
        return EvidenceGap(scope="pod", code="pod_uid_mismatch")
    return None


async def inspect(request: K8sInspectorInput, client) -> K8sInspectorOutput:
    now = datetime.now(UTC)
    output = K8sInspectorOutput(
        namespace=request.namespace,
        pod_name=request.pod_name,
        pod_uid=request.pod_uid,
        observed_at=now,
        status="ok",
        containers=[],
        logs=[],
        events=[],
        gaps=[],
    )
    if request.pod_uid is None:
        output.status = "unavailable"
        output.gaps.append(EvidenceGap(scope="pod", code="pod_identity_unavailable"))
        return output

    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    headers = {"Authorization": f"Bearer {token}"}
    root = f"/api/v1/namespaces/{request.namespace}"
    path = f"{root}/pods/{request.pod_name}"
    pod = await get_pod(client, path, headers)
    output.events = await events_for(
        client, root, headers, request.pod_uid, now - timedelta(seconds=request.lookback_seconds)
    )
    gap = pod_gap(pod, request.pod_uid)
    if gap:
        output.status = "not_found"
        output.gaps.append(gap)
        return output

    output.containers = sorted(
        (item for item in container_evidence(pod) if request.container in (None, item.name)),
        key=lambda item: item.name,
    )
    if request.container and not output.containers:
        output.gaps.append(
            EvidenceGap(scope="container", code="container_not_found", container=request.container)
        )
    for container in output.containers[:8]:
        instances = [False]
        if request.include_previous:
            if container.restart_count:
                instances.append(True)
            else:
                output.gaps.append(
                    EvidenceGap(
                        scope="logs_previous",
                        code="not_applicable"
                        if container.restart_count == 0
                        else "no_retained_data",
                        container=container.name,
                    )
                )
        for previous in instances:
            item = await read_log(client, path, headers, request, container.name, previous)
            if isinstance(item, LogEvidence):
                output.logs.append(item)
            else:
                output.gaps.append(item)
    if output.containers:
        gap = pod_gap(await get_pod(client, path, headers), request.pod_uid)
        if gap:
            output.status = "not_found"
            output.containers.clear()
            output.logs.clear()
            output.gaps = [gap]
            return output
    if any(item.code != "not_applicable" for item in output.gaps):
        output.status = "partial"
    return output
