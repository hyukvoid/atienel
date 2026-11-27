from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

FIXTURES = {
    "OOMKilled": ("chaos-oom-fixture", "oom.md", "SC-003 로컬 OOM fixture"),
    "CrashLoopBackOff": ("chaos-crash-fixture", "crashloop.md", "SC-003 로컬 설정 오류 fixture"),
    "Http5xxSpike": ("chaos-app", "http-5xx.md", "SC-003 로컬 HTTP 500 fixture"),
}
IMAGE = "atienel-chaos-app:phase2"


@dataclass(frozen=True)
class PatchProposal:
    cause: str
    manifest: dict
    citations: tuple[str, ...]


def observed_time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) and value.utcoffset() is not None else None


def recent_log_lines(pod, container, observed_at, seconds):
    for log in pod.get("logs", []):
        if log.get("container") != container:
            continue
        for line in log.get("text", "").splitlines():
            stamp, _, message = line.partition(" ")
            at = observed_time(stamp)
            if at is not None and -seconds <= (at - observed_at).total_seconds() <= 5:
                yield message


def fixture_proposal(incident, pod, matches):
    fixture = FIXTURES.get(incident["incident_type"])
    workload = pod.get("workload")
    owner = pod.get("owner")
    observed = observed_time(incident.get("observed_at"))
    if (
        not fixture
        or pod.get("status") not in ("ok", "partial")
        or not workload
        or not owner
        or observed is None
        or pod.get("pod_uid") != incident.get("pod_uid")
        or pod.get("pod_name") != incident.get("pod_name")
        or incident.get("namespace") != "target-apps"
    ):
        return None
    name, runbook, section = fixture
    if (
        workload != {"kind": "Deployment", "namespace": "target-apps", "name": name}
        or owner.get("kind") != "ReplicaSet"
        or not owner.get("name", "").startswith(f"{name}-")
    ):
        return None
    citations = tuple(
        row["chunk_id"]
        for row in matches
        if row.get("source_uri") == f"repo:///data/runbooks/{runbook}"
        and row.get("section") == section
    )
    container = next(
        (
            item
            for item in pod.get("containers", [])
            if item.get("kind") == "app" and item.get("name") == name
        ),
        None,
    )
    if (
        not citations
        or not container
        or container.get("image")
        not in (
            IMAGE,
            f"docker.io/library/{IMAGE}",
        )
    ):
        return None
    if incident.get("container_name") not in (None, name):
        return None

    incident_type = incident["incident_type"]
    termination = container.get("termination") or {}
    template = {}
    if incident_type == "OOMKilled":
        finished = observed_time(termination.get("finished_at"))
        if (
            termination.get("reason") != "OOMKilled"
            or termination.get("exit_code") != 137
            or finished is None
            or abs((finished - observed).total_seconds()) > 5
            or container.get("limits", {}).get("memory") != "128Mi"
            or container.get("requests", {}).get("memory") != "32Mi"
            or not any(
                '"POST /trigger HTTP/1.1" 202 ' in line
                for line in recent_log_lines(pod, name, observed, 15)
            )
        ):
            return None
        cause = (
            "The local fixture's controlled allocation exceeded its 128Mi container limit. "
            "Kubernetes recorded a matching OOMKilled termination with exit code 137."
        )
        template["spec"] = {
            "containers": [{"name": name, "resources": {"limits": {"memory": "384Mi"}}}]
        }
    elif incident_type == "CrashLoopBackOff":
        if (
            "/fixture" not in container.get("empty_dir_mounts", [])
            or not termination.get("exit_code")
            or not any(
                line.strip() == "Fixture startup failed: invalid configuration"
                for line in recent_log_lines(pod, name, observed, 30)
            )
        ):
            return None
        cause = (
            "The fixture startup log confirms invalid configuration. The failure flag survives "
            "container restarts in the Pod's /fixture emptyDir; a replacement Pod clears it."
        )
        try:
            marker = str(UUID(str(incident.get("id"))))
        except ValueError:
            return None
        template["metadata"] = {"annotations": {"atienel.io/reset-fixture-state": marker}}
    else:
        if (
            container.get("http_fault_enabled") is not True
            or incident["signal"].get("source") != "http_metrics"
            or not incident.get("event_id", "").startswith(f"http:{incident['pod_uid']}:")
            or sum(
                '"GET /faults/http-500 HTTP/1.1" 500 ' in line
                for line in recent_log_lines(pod, name, observed, 35)
            )
            < 10
        ):
            return None
        cause = (
            "The enabled chaos HTTP fixture returned at least ten HTTP 500 responses "
            "from /faults/http-500 in the observed window."
        )
        template["spec"] = {
            "containers": [
                {"name": name, "env": [{"name": "HTTP_FAULT_ENABLED", "value": "false"}]}
            ]
        }

    return PatchProposal(
        cause,
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": "target-apps"},
            "spec": {"template": template},
        },
        citations,
    )
