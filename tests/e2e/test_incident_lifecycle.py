import json
import re
import time
from uuid import UUID, uuid4

import pytest
import yaml
from phase2_verify import http, kube, pod_for, port_forward, psql, wait_for
from phase4_verify import approval, verify, workload_config


def test_oom_lifecycle(cluster):
    started = time.time()
    uid = UUID(pod_for("chaos-oom-fixture")["metadata"]["uid"])
    with (
        port_forward("target-apps", "svc/chaos-app", 8080) as chaos,
        port_forward("aiops-core", "deployment/aiops-api", 8000) as api,
    ):
        verify(api, chaos, cluster, "verification")
    scenario_report(uid, "OOMKilled", started)


def scenario_report(uid, signal, started):
    def read():
        return psql(f"""
            SELECT row_to_json(result) FROM (
                SELECT r.recommended_yaml_patch AS patch, r.patch_unavailable_reason,
                    r.suspected_cause AS cause, r.citations,
                    round(extract(epoch FROM r.created_at - i.created_at)::numeric, 3) AS elapsed
                FROM investigation_reports r JOIN incidents i ON i.id=r.incident_id
                WHERE i.pod_uid='{uid}' AND i.incident_type='{signal}'
                    AND i.created_at >= to_timestamp({started})
                ORDER BY i.created_at LIMIT 1
            ) result;
        """)

    result = json.loads(wait_for(read, f"{signal} report", timeout=120))
    assert result["elapsed"] <= 120
    assert result["patch"], result["patch_unavailable_reason"]
    assert result["patch_unavailable_reason"] is None
    patch = yaml.safe_load(result["patch"])
    assert isinstance(patch, dict)
    targets = {
        "OOMKilled": ("chaos-oom-fixture", "/oom.md", "128Mi"),
        "CrashLoopBackOff": ("chaos-crash-fixture", "/crashloop.md", "invalid configuration"),
        "Http5xxSpike": ("chaos-app", "/http-5xx.md", "/faults/http-500"),
    }
    target, source, cause = targets[signal]
    assert cause in result["cause"]
    assert (patch["apiVersion"], patch["kind"], patch["metadata"]) == (
        "apps/v1",
        "Deployment",
        {"name": target, "namespace": "target-apps"},
    )
    assert any(
        row["source_uri"].endswith(source) and row["section"].startswith("SC-003 ")
        for row in result["citations"]
    )
    template = patch["spec"]["template"]
    if signal == "OOMKilled":
        assert template["spec"]["containers"][0]["resources"]["limits"]["memory"] == "384Mi"
    elif signal == "CrashLoopBackOff":
        assert UUID(template["metadata"]["annotations"]["atienel.io/reset-fixture-state"])
    else:
        assert {"name": "HTTP_FAULT_ENABLED", "value": "false"} in (
            template["spec"]["containers"][0]["env"]
        )
    print(
        f"SC-003 {signal}: {result['elapsed']:.3f}s, "
        f"{len(result['citations'])} citations, YAML parsed and checked"
    )
    return result


def deployment_spec(name):
    return json.loads(kube("target-apps", "get", "deployment", name, "-o", "json"))["spec"]


def test_crashloop_diagnosis(cluster):
    uid = UUID(pod_for("chaos-crash-fixture")["metadata"]["uid"])
    before = deployment_spec("chaos-crash-fixture")
    with port_forward("target-apps", "svc/chaos-app", 8080) as chaos:
        started = time.time()
        assert http(f"{chaos}/faults/crashloop", method="POST")[0] == 202
        try:
            assert scenario_report(uid, "CrashLoopBackOff", started)
            assert deployment_spec("chaos-crash-fixture") == before
        finally:
            kube("target-apps", "rollout", "restart", "deployment/chaos-crash-fixture")
            kube(
                "target-apps",
                "rollout",
                "status",
                "deployment/chaos-crash-fixture",
                "--timeout=90s",
            )


def test_http_spike_diagnosis(cluster):
    uid = UUID(pod_for("chaos-app")["metadata"]["uid"])
    before = deployment_spec("chaos-app")
    previous = psql(f"""
        SELECT extract(epoch FROM max(observed_at)) FROM incidents
        WHERE pod_uid = '{uid}' AND incident_type = 'Http5xxSpike';
    """)
    time.sleep(max(0, 36 + float(previous) - time.time()) if previous else 6)
    with port_forward("target-apps", "svc/chaos-app", 8080) as chaos:
        started = time.time()
        for _ in range(10):
            assert http(f"{chaos}/faults/http-500")[0] == 500
        assert scenario_report(uid, "Http5xxSpike", started)
        assert deployment_spec("chaos-app") == before


def test_ten_approval_replays(cluster):
    """Use this run's load tickets for the ten-approval acceptance sample when available."""
    prefix = psql("""
        SELECT substring(event_id FROM '^(phase5:[0-9a-f]{32}:fresh:|phase5-offline:[0-9a-f]{32}:)')
        FROM incidents
        WHERE event_id ~ '^(phase5:[0-9a-f]{32}:fresh:|phase5-offline:[0-9a-f]{32}:)[0-9]+$'
        ORDER BY created_at DESC LIMIT 1;
    """)
    if not prefix:
        pytest.skip("run the worker-off acceptance or load exercise to seed ten tickets")
    assert re.fullmatch(r"(phase5:[0-9a-f]{32}:fresh:|phase5-offline:[0-9a-f]{32}:)", prefix)
    rows = psql(f"""
        SELECT id FROM incidents WHERE event_id LIKE '{prefix}%'
        AND status IN ('NEEDS_REVIEW', 'APPROVED') ORDER BY created_at LIMIT 10;
    """)
    ids = [str(UUID(row)) for row in rows.splitlines()]
    assert len(ids) == 10
    config = workload_config()
    with port_forward("aiops-core", "deployment/aiops-api", 8000) as api:
        for incident in ids:
            key = f"phase5-{uuid4()}"
            result = approval(api, cluster["approver"], incident, key)
            assert result[0] == 200
            assert approval(api, cluster["approver"], incident, key) == result
    assert workload_config() == config
