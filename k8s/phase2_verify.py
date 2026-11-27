"""Verify Phase 2 against the local k3d cluster."""

import base64
import json
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4


def kube(namespace, *args, capture=True):
    return subprocess.run(
        [sys.argv[1], "--context", sys.argv[2], "-n", namespace, *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def wait_for(observe, description, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = observe()
        if result:
            return result
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {description}")


@contextmanager
def port_forward(namespace, target, port):
    with subprocess.Popen(
        [
            sys.argv[1],
            "--context",
            sys.argv[2],
            "-n",
            namespace,
            "port-forward",
            "--address",
            "127.0.0.1",
            target,
            f":{port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        try:
            line = process.stdout.readline()
            match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
            require(match, f"{target} port forwarding failed: {line.strip()}")
            yield f"http://127.0.0.1:{match[1]}"
        finally:
            process.terminate()
            process.wait(timeout=5)


def http(url, *, method="GET", token=None, key=None, event=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if key:
        headers["Idempotency-Key"] = key
    if event is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        url,
        method=method,
        headers=headers,
        data=json.dumps(event).encode() if event is not None else None,
    )
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response), response.headers


def pod_for(name):
    pods = json.loads(
        kube("target-apps", "get", "pod", "-l", f"app.kubernetes.io/name={name}", "-o", "json")
    )["items"]
    return next(pod for pod in pods if "deletionTimestamp" not in pod["metadata"])


def container_status(pod):
    return pod.get("status", {}).get("containerStatuses", [{}])[0]


def psql(query):
    return kube(
        "aiops-data",
        "exec",
        "postgresql-0",
        "--",
        "psql",
        "-XAt",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        "atienel",
        "-c",
        query,
    ).strip()


def queued(event_id):
    require(re.fullmatch(r"[A-Za-z0-9:._-]+", event_id), "Unexpected event ID")
    row = psql(f"""
        SELECT row_to_json(result) FROM (
            SELECT i.id AS incident_id, i.signal, i.status, j.id AS job_id,
                   j.stream_entry_id, j.published_at
            FROM incidents i JOIN investigation_jobs j ON j.incident_id = i.id
            WHERE i.event_id = '{event_id}'
        ) result;
    """)
    return json.loads(row) if row else None


def verify_entry(row):
    entry_id = row["stream_entry_id"]
    require(entry_id and row["published_at"], "Publication was not committed")
    entries = json.loads(
        kube(
            "aiops-data",
            "exec",
            "valkey-0",
            "--",
            "valkey-cli",
            "--json",
            "XRANGE",
            "aiops:incidents:v1",
            entry_id,
            entry_id,
        )
    )
    expected = {"schema_version": 1, "job_id": row["job_id"], "incident_id": row["incident_id"]}
    require(len(entries) == 1, f"Valkey entry {entry_id} missing")
    entry, fields = entries[0]
    require(
        entry == entry_id and len(fields) == 2 and fields[0] == "payload",
        "Unexpected Valkey fields",
    )
    require(json.loads(fields[1]) == expected, "Valkey payload differs from PostgreSQL job")


def watcher_acceptance(event_id):
    row = queued(event_id)
    if not row or not row["published_at"]:
        return None
    log = kube("aiops-core", "logs", "deployment/event-watcher", "--since=10m")
    expected = f"status=202 event_id={event_id} incident_id={row['incident_id']}"
    return row if expected in log else None


def verify_oom(chaos, api, tokens):
    pod = pod_for("chaos-oom-fixture")
    uid = str(UUID(pod["metadata"]["uid"]))
    restarts = container_status(pod).get("restartCount", 0)
    status, _, _ = http(f"{chaos}/faults/oom", method="POST")
    require(status == 202, f"OOM fixture returned HTTP {status}")

    def termination():
        current = pod_for("chaos-oom-fixture")
        require(current["metadata"]["uid"] == uid, "OOM fixture Pod was replaced")
        state = container_status(current)
        if state.get("restartCount", 0) <= restarts:
            return None
        term = state.get("lastState", {}).get("terminated", {})
        return term if term.get("reason") == "OOMKilled" and term.get("exitCode") == 137 else None

    term = wait_for(termination, "OOMKilled / exit code 137")
    at = datetime.fromisoformat(term["finishedAt"])
    seconds = int(at.timestamp())
    event_id = f"oom:{uid}:chaos-oom-fixture:{seconds * 10**9 + at.microsecond * 1000}"
    row = wait_for(lambda: watcher_acceptance(event_id), "watcher HTTP 202 and PostgreSQL row")
    verify_entry(row)
    incident_id = row["incident_id"]
    status, detail, _ = http(f"{api}/incidents/{incident_id}", token=tokens["reader"])
    require(
        status == 200
        and detail["event_id"] == event_id
        and detail["status"] == row["status"] == "OPEN",
        "GET incident did not return the OPEN ticket",
    )

    event = {
        **{
            name: detail[name]
            for name in (
                "event_id",
                "namespace",
                "pod_name",
                "pod_uid",
                "container_name",
                "incident_type",
                "observed_at",
            )
        },
        **row["signal"],
    }
    key = f"verify-{uuid4()}"
    request = {"method": "POST", "token": tokens["ingest"], "key": key, "event": event}
    for _ in range(2):
        status, payload, headers = http(f"{api}/incidents", **request)
        require(
            status == 202
            and payload == {"incident_id": incident_id}
            and headers.get("Location") == f"/incidents/{incident_id}",
            "Idempotent response differs",
        )
    request["event"] = {**event, "summary": "Changed verification payload"}
    status, payload, _ = http(f"{api}/incidents", **request)
    require(status == 409 and payload["error"]["code"] == "IDEMPOTENCY_CONFLICT", "Expected 409")
    require(queued(event_id) == row, "Replay changed the PostgreSQL publication")
    print(
        f"OOMKilled (137) → watcher HTTP 202 → OPEN {incident_id} → Valkey {row['stream_entry_id']}"
    )
    print("Idempotency replay, Location and conflicting payload verified")


def verify_http(chaos):
    uid = str(UUID(pod_for("chaos-app")["metadata"]["uid"]))
    last = psql(f"""
        SELECT extract(epoch FROM max(observed_at)) FROM incidents
        WHERE pod_uid = '{uid}' AND incident_type = 'Http5xxSpike';
    """)
    # Let the previous verification burst age out before beginning another episode.
    time.sleep(max(0, 36 + float(last) - time.time()) if last else 6)
    started = int(time.time())
    status, baseline, _ = http(f"{chaos}/metrics")
    require(status == 200 and baseline["pod_uid"] == uid, "Metrics baseline unavailable")
    for _ in range(10):
        status, _, _ = http(f"{chaos}/faults/http-500")
        require(status == 500, f"Expected fixture HTTP 500, got {status}")
    _, after, _ = http(f"{chaos}/metrics")
    require(after["http_5xx_total"] - baseline["http_5xx_total"] == 10, "Counter delta differs")

    # One prior sample can precede the burst by the poll interval.
    def accepted():
        result = psql(f"""
            SELECT event_id FROM incidents WHERE pod_uid = '{uid}'
            AND incident_type = 'Http5xxSpike'
            AND observed_at >= to_timestamp({started})
            ORDER BY observed_at DESC LIMIT 1;
        """)
        return watcher_acceptance(result) if result else None

    verify_entry(wait_for(accepted, "HTTP 5xx spike acceptance"))
    print("10 HTTP 500 responses, Pod counter and Http5xxSpike publication verified")


def verify_crash(chaos):
    pod = pod_for("chaos-crash-fixture")
    uid = str(UUID(pod["metadata"]["uid"]))
    status, _, _ = http(f"{chaos}/faults/crashloop", method="POST")
    require(status == 202, f"Crash fixture returned HTTP {status}")
    try:
        wait_for(
            lambda: (
                container_status(pod_for("chaos-crash-fixture"))
                .get("state", {})
                .get("waiting", {})
                .get("reason")
                == "CrashLoopBackOff"
            ),
            "CrashLoopBackOff",
        )

        def accepted():
            event_id = psql(f"""
                SELECT event_id FROM incidents WHERE pod_uid = '{uid}'
                AND incident_type = 'CrashLoopBackOff' ORDER BY created_at DESC LIMIT 1;
            """)
            return watcher_acceptance(event_id) if event_id else None

        verify_entry(wait_for(accepted, "CrashLoopBackOff acceptance"))
        print("CrashLoopBackOff and publication verified")
    finally:
        kube("target-apps", "rollout", "restart", "deployment/chaos-crash-fixture")
        kube("target-apps", "rollout", "status", "deployment/chaos-crash-fixture", "--timeout=90s")


def verify_recovery(api, tokens):
    pod = pod_for("chaos-app")["metadata"]
    event = {
        "event_id": f"verify:recovery:{uuid4()}",
        "namespace": "target-apps",
        "pod_name": pod["name"],
        "pod_uid": pod["uid"],
        "incident_type": "KubernetesWarning",
        "observed_at": datetime.now(UTC).isoformat(),
        "source": "kubernetes_event",
        "summary": "Phase 2 Valkey outage verification",
    }
    request = {
        "method": "POST",
        "key": f"verify-{uuid4()}",
        "token": tokens["ingest"],
        "event": event,
    }
    try:
        kube("aiops-data", "scale", "statefulset/valkey", "--replicas=0")
        kube("aiops-data", "wait", "--for=delete", "pod/valkey-0", "--timeout=60s")
        status, _, headers = http(f"{api}/incidents", **request)
        require(status == 503 and headers.get("Retry-After") == "1", "Expected retryable 503")
        initial = queued(event["event_id"])
        require(initial and initial["published_at"] is None, "Outbox row was not retained")
    finally:
        kube("aiops-data", "scale", "statefulset/valkey", "--replicas=1")
        kube("aiops-data", "rollout", "status", "statefulset/valkey", "--timeout=90s")

    def published():
        row = queued(event["event_id"])
        return row if row and row["published_at"] else None

    row = wait_for(published, "background publication recovery")
    status, body, _ = http(f"{api}/incidents", **request)
    require(
        status == 202
        and body["incident_id"] == initial["incident_id"]
        and row["job_id"] == initial["job_id"],
        "Retry did not reuse the original ticket and job",
    )
    verify_entry(row)
    print("Valkey outage HTTP 503, outbox recovery and same-key HTTP 202 verified")

    # Delete only the entry we created above to exercise missing-entry reconciliation.
    deleted = kube(
        "aiops-data",
        "exec",
        "valkey-0",
        "--",
        "valkey-cli",
        "XDEL",
        "aiops:incidents:v1",
        row["stream_entry_id"],
    ).strip()
    require(deleted == "1", "Could not remove the verification entry")

    def republished():
        current = published()
        return current if current and current["stream_entry_id"] != row["stream_entry_id"] else None

    replacement = wait_for(republished, "republication of the missing stream entry")
    require(replacement["job_id"] == row["job_id"], "Reconciliation changed the logical job ID")
    verify_entry(replacement)
    print("Missing Valkey entry republished with the original job ID")


def main():
    data = json.loads(kube("aiops-core", "get", "secret", "api-tokens", "-o", "json"))["data"]
    tokens = {name: base64.b64decode(data[name]).decode() for name in ("ingest", "reader")}
    with (
        port_forward("target-apps", "svc/chaos-app", 8080) as chaos,
        port_forward("aiops-core", "deployment/aiops-api", 8000) as api,
    ):
        require(http(f"{api}/readyz")[0] == 200, "API is not ready")
        verify_oom(chaos, api, tokens)
        verify_http(chaos)
        verify_crash(chaos)
        if "--recovery" in sys.argv[3:]:
            verify_recovery(api, tokens)


if __name__ == "__main__":
    main()
