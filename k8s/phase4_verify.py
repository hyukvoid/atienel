"""Verify the OOM diagnosis and approval lifecycle against the local k3d cluster."""

import base64
import json
import os
from datetime import datetime
from uuid import UUID, uuid4

from phase2_verify import (
    container_status,
    http,
    kube,
    pod_for,
    port_forward,
    psql,
    require,
    verify_entry,
    wait_for,
    watcher_acceptance,
)

WORKER = "deployment/aiops-worker"
STREAM = "aiops:incidents:v1"
GROUP = "aiops-workers-v1"


def valkey(*args):
    return kube("aiops-data", "exec", "valkey-0", "--", "valkey-cli", *args).strip()


def trigger(chaos):
    before = pod_for("chaos-oom-fixture")
    uid = str(UUID(before["metadata"]["uid"]))
    restarts = container_status(before)["restartCount"]
    require(http(f"{chaos}/faults/oom", method="POST")[0] == 202, "OOM fixture rejected trigger")

    def terminated():
        current = pod_for("chaos-oom-fixture")
        require(current["metadata"]["uid"] == uid, "OOM Pod changed")
        status = container_status(current)
        term = status.get("lastState", {}).get("terminated", {})
        return (
            term
            if status["restartCount"] > restarts
            and term.get("reason") == "OOMKilled"
            and term.get("exitCode") == 137
            else None
        )

    term = wait_for(terminated, "new OOMKilled/137", timeout=90)
    at = datetime.fromisoformat(term["finishedAt"])
    generation = int(at.timestamp()) * 10**9 + at.microsecond * 1000
    return f"oom:{uid}:chaos-oom-fixture:{generation}"


def get_ticket(api, tokens, identifier):
    status, detail, _ = http(f"{api}/incidents/{identifier}", token=tokens["reader"])
    require(status == 200, f"Ticket lookup returned {status}")
    return detail


def approval(api, token, incident, key):
    status, body, _ = http(
        f"{api}/incidents/{incident}/approve",
        method="POST",
        token=token,
        key=key,
    )
    return status, body


def error(response, status, code):
    require(
        response[0] == status and response[1]["error"]["code"] == code,
        f"Expected {status} {code}, got {response[0]}",
    )


def workload_config():
    deployment = json.loads(
        kube("target-apps", "get", "deployment", "chaos-oom-fixture", "-o", "json")
    )
    return deployment["metadata"]["generation"], deployment["spec"]


def rollout():
    kube("aiops-core", "rollout", "status", WORKER, "--timeout=110s")


def open_oom(api, chaos, tokens, key):
    wait_for(
        lambda: container_status(pod_for("chaos-oom-fixture")).get("ready"),
        "OOM fixture to become ready",
        timeout=130,
    )
    try:
        kube("aiops-core", "scale", WORKER, "--replicas=0")
        selector = "app.kubernetes.io/name=aiops-worker"
        wait_for(
            lambda: (
                not json.loads(
                    kube(
                        "aiops-core",
                        "get",
                        "pods",
                        "-l",
                        selector,
                        "-o",
                        "json",
                    )
                )["items"]
            ),
            "worker Pods to stop",
            timeout=115,
        )
        event_id = trigger(chaos)
        row = wait_for(lambda: watcher_acceptance(event_id), "watcher 202 and database publication")
        verify_entry(row)
        incident = str(UUID(row["incident_id"]))
        opened = get_ticket(api, tokens, incident)
        require(opened["status"] == "OPEN" and opened["report"] is None, "Expected OPEN ticket")
        error(approval(api, tokens["approver"], incident, key), 409, "INVALID_TRANSITION")
        print(
            f"OOMKilled/137 → watcher HTTP 202 → OPEN {incident} → Valkey {row['stream_entry_id']}"
        )
    finally:
        kube("aiops-core", "scale", WORKER, "--replicas=1")
        rollout()

    return incident, opened, row


def resume_oom(api, tokens, incident):
    opened = get_ticket(api, tokens, incident)
    require(opened["incident_type"] == "OOMKilled", "Resume requires an OOMKilled ticket")
    result = psql(f"""
        SELECT row_to_json(receipt)
        FROM (
            SELECT j.id AS job_id, j.stream_entry_id
            FROM investigation_jobs j
            WHERE j.incident_id = '{incident}' AND j.published_at IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM api_idempotency i
                  WHERE i.incident_id = j.incident_id AND i.principal_id = 'event-watcher'
                    AND i.operation = 'create_incident' AND i.response_status = 202
              )
        ) receipt;
    """)
    require(result, "Missing watcher 202 or Valkey publication receipt")
    print(f"Resuming previously published OOM ticket {incident}")
    return incident, opened, json.loads(result)


def verify(api, chaos, tokens, mode):
    key = f"phase4-{uuid4()}"
    config = workload_config()
    resume_id = os.environ.get("PHASE4_VERIFY_INCIDENT_ID")
    if resume_id:
        incident, opened, row = resume_oom(api, tokens, str(UUID(resume_id)))
    else:
        incident, opened, row = open_oom(api, chaos, tokens, key)

    def needs_review():
        detail = get_ticket(api, tokens, incident)
        state = psql(f"SELECT state FROM investigation_jobs WHERE incident_id = '{incident}'")
        require(state != "FAILED", "Worker marked the target job as FAILED")
        return detail if detail["status"] == "NEEDS_REVIEW" else None

    # A provider-mode worker may need to finish earlier queued investigations first.
    review_timeout = 600 if mode == "provider" else 130
    reviewed = wait_for(needs_review, "worker report and NEEDS_REVIEW", timeout=review_timeout)
    report = reviewed["report"]
    if mode == "provider":
        require(
            "Verification mode uses deterministic diagnosis rules." not in report["limitations"],
            "Provider verification received a deterministic diagnosis",
        )
        if not resume_id:
            logs = kube("aiops-core", "logs", WORKER, "--since=10m")
            require(
                any(
                    "Provider diagnosis validated model=" in line
                    and f"incident_id={incident}" in line
                    for line in logs.splitlines()
                ),
                "No validated provider response for the test incident",
            )
    require(report["citations"] and reviewed["approval"] is None, "Report or citations missing")
    require(
        any(
            citation["source_uri"].endswith(("/oom.md", "/fixture-retrospective.md"))
            for citation in report["citations"]
        ),
        "Expected citation from the seeded OOM documents",
    )
    pod = next(item for item in report["evidence"] if item["source"] == "k8s_inspector")
    require(
        pod["pod_uid"] == opened["pod_uid"]
        and any((item["termination"] or {}).get("exit_code") == 137 for item in pod["containers"]),
        "Report does not contain MCP termination evidence",
    )
    require(
        bool(report["recommended_yaml_patch"]) != bool(report["patch_unavailable_reason"]),
        "Missing patch or explanation",
    )
    wait_for(
        lambda: valkey("XRANGE", STREAM, row["stream_entry_id"], row["stream_entry_id"]) == "",
        "Stream settlement",
    )
    print(f"Valkey → worker → MCP evidence and citations → NEEDS_REVIEW {report['id']}")

    error(approval(api, tokens["reader"], incident, key), 403, "FORBIDDEN")
    status, approved = approval(api, tokens["approver"], incident, key)
    require(status == 200 and approved["report_id"] == report["id"], "Approval failed")
    require(
        approval(api, tokens["approver"], incident, key) == (200, approved),
        "Same-key approval replay changed",
    )
    require(
        approval(api, tokens["approver"], incident, f"phase4-{uuid4()}") == (200, approved),
        "New-key same-approver replay changed",
    )
    error(approval(api, tokens["approver"], uuid4(), key), 409, "IDEMPOTENCY_CONFLICT")

    kube("aiops-core", "rollout", "restart", WORKER)
    rollout()
    payload = json.dumps(
        {
            "schema_version": 1,
            "job_id": row["job_id"],
            "incident_id": incident,
        }
    )
    duplicate = valkey("XADD", STREAM, "*", "payload", payload)
    wait_for(
        lambda: valkey("XRANGE", STREAM, duplicate, duplicate) == "",
        "redelivery settlement",
        timeout=review_timeout,
    )
    final = get_ticket(api, tokens, incident)
    require(
        final["status"] == "APPROVED"
        and final["report"] == report
        and final["approval"]
        == {field: approved[field] for field in ("approved_by", "approved_at")},
        "Approval or stored report changed after worker redelivery",
    )
    require(workload_config() == config, "Approval changed the target Deployment")
    print(
        f"APPROVED by {approved['approved_by']}; replay and worker redelivery preserved the report"
    )

    invalid = valkey("XADD", STREAM, "*", "payload", "{}")
    wait_for(
        lambda: valkey("XRANGE", STREAM, invalid, invalid) == "",
        "invalid entry settlement",
        timeout=review_timeout,
    )
    recent = json.loads(
        valkey("--json", "XREVRANGE", "aiops:incidents:invalid:v1", "+", "-", "COUNT", "20")
    )
    require(
        any(
            fields == ["entry_id", invalid, "error_code", "INVALID_PAYLOAD"] for _, fields in recent
        ),
        "Invalid entry was not quarantined",
    )
    print("Invalid Stream payload quarantined without copying the original payload")


def main():
    mode = kube(
        "aiops-core",
        "get",
        WORKER,
        "-o",
        'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="DIAGNOSIS_MODE")].value}',
    ).strip()
    require(mode in ("provider", "verification"), "Unknown worker diagnosis mode")
    raw = json.loads(kube("aiops-core", "get", "secret", "api-tokens", "-o", "json"))["data"]
    tokens = {name: base64.b64decode(raw[name]).decode() for name in ("reader", "approver")}
    with (
        port_forward("target-apps", "svc/chaos-app", 8080) as chaos,
        port_forward("aiops-core", "deployment/aiops-api", 8000) as api,
    ):
        verify(api, chaos, tokens, mode)


if __name__ == "__main__":
    main()
