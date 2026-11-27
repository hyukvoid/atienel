from copy import deepcopy
from uuid import uuid4

import pytest
import yaml
from src.agent import Diagnosis, fixture_model, report_from
from src.patch_proposals import FIXTURES

STAMP = "2026-11-27T10:00:00Z"
VALUES = ("OOMKilled", "CrashLoopBackOff", "Http5xxSpike")


def fixture_input(signal):
    name, runbook, section = FIXTURES[signal]
    uid = str(uuid4())
    incident = {
        "incident_type": signal,
        "id": uuid4(),
        "observed_at": STAMP,
        "namespace": "target-apps",
        "pod_name": f"{name}-hash-pod",
        "pod_uid": uid,
        "signal": {"source": "http_metrics"} if signal == "Http5xxSpike" else {},
        "event_id": f"http:{uid}:0",
        "container_name": name,
    }
    pod = {
        "status": "partial",
        "gaps": [],
        "pod_name": incident["pod_name"],
        "pod_uid": uid,
        "owner": {"kind": "ReplicaSet", "name": f"{name}-hash", "uid": str(uuid4())},
        "workload": {"kind": "Deployment", "name": name, "namespace": "target-apps"},
        "containers": [
            {
                "kind": "app",
                "name": name,
                "image": "atienel-chaos-app:phase2",
                "limits": {"memory": "128Mi"},
                "requests": {"memory": "32Mi"},
                "termination": {
                    "reason": "OOMKilled" if signal == "OOMKilled" else "Error",
                    "exit_code": 137 if signal == "OOMKilled" else 1,
                    "finished_at": STAMP,
                },
                "empty_dir_mounts": ["/fixture"],
                "http_fault_enabled": True,
            }
        ],
        "logs": [
            {
                "container": name,
                "text": (
                    f"{STAMP} Fixture startup failed: invalid configuration\n"
                    f'{STAMP} INFO: "POST /trigger HTTP/1.1" 202 Accepted\n'
                    + f'{STAMP} INFO: "GET /faults/http-500 HTTP/1.1" 500 Internal Server Error\n'
                    * 10
                ),
            }
        ],
    }
    matches = [
        {
            "chunk_id": str(uuid4()),
            "source_uri": f"repo:///data/runbooks/{runbook}",
            "section": section,
        }
    ]
    return incident, pod, matches


def fixture_output(incident, pod, matches):
    return Diagnosis.model_validate(fixture_model(incident, pod, matches).custom_output_args)


@pytest.mark.parametrize("signal", VALUES)
def test_fixture_patch_matches_current_configuration_and_citation(signal):
    incident, pod, matches = fixture_input(signal)
    diagnosis = fixture_output(incident, pod, matches)
    assert diagnosis.patch_unavailable_reason is None
    assert diagnosis.citation_ids == [matches[0]["chunk_id"]]
    manifest = yaml.safe_load(diagnosis.recommended_yaml_patch)
    assert manifest["metadata"] == {"name": FIXTURES[signal][0], "namespace": "target-apps"}
    template = manifest["spec"]["template"]
    if signal == "OOMKilled":
        assert template["spec"]["containers"][0]["resources"] == {"limits": {"memory": "384Mi"}}
    elif signal == "CrashLoopBackOff":
        assert template["metadata"]["annotations"]["atienel.io/reset-fixture-state"] == str(
            incident["id"]
        )
    else:
        assert template["spec"]["containers"][0]["env"] == [
            {"name": "HTTP_FAULT_ENABLED", "value": "false"}
        ]
    assert (
        yaml.safe_load(report_from(incident, pod, matches, diagnosis)["recommended_yaml_patch"])
        == manifest
    )


@pytest.mark.parametrize(
    ("signal", "missing"),
    [
        ("KubernetesWarning", "unknown cause"),
        ("OOMKilled", "pod"),
        ("OOMKilled", "workload"),
        ("OOMKilled", "requests"),
        ("OOMKilled", "stale termination"),
        ("OOMKilled", "logs"),
        ("CrashLoopBackOff", "logs"),
        ("Http5xxSpike", "logs"),
        ("Http5xxSpike", "synthetic event"),
        ("Http5xxSpike", "matching runbook"),
    ],
)
def test_insufficient_patch_evidence_keeps_reason(signal, missing):
    incident, pod, matches = fixture_input(signal if signal in VALUES else "OOMKilled")
    incident["incident_type"] = signal
    if missing == "pod":
        pod["status"] = "not_found"
        pod["containers"] = []
    elif missing == "workload":
        del pod["workload"]
    elif missing == "requests":
        del pod["containers"][0]["requests"]
    elif missing == "stale termination":
        pod["containers"][0]["termination"]["finished_at"] = "2026-11-27T09:58:00Z"
    elif missing == "logs":
        pod["logs"] = []
    elif missing == "synthetic event":
        incident["event_id"] = "phase5:synthetic"
    elif missing == "matching runbook":
        matches[0]["section"] = "Signal review"
    diagnosis = fixture_output(incident, pod, matches)
    result = report_from(incident, pod, matches, diagnosis)
    assert result["recommended_yaml_patch"] is None
    assert result["patch_unavailable_reason"]


@pytest.mark.parametrize("tamper", ["target", "memory", "extra field", "missing workload"])
def test_provider_patch_must_match_guarded_proposal(tamper):
    incident, pod, matches = fixture_input("OOMKilled")
    diagnosis = fixture_output(incident, pod, matches)
    manifest = deepcopy(yaml.safe_load(diagnosis.recommended_yaml_patch))
    if tamper == "target":
        manifest["metadata"]["name"] = "another-deployment"
    elif tamper == "memory":
        manifest["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = (
            "99Gi"
        )
    elif tamper == "extra field":
        manifest["spec"]["replicas"] = 10
    elif tamper == "missing workload":
        del pod["workload"]
    diagnosis.recommended_yaml_patch = yaml.safe_dump(manifest)
    result = report_from(incident, pod, matches, diagnosis)
    assert result["recommended_yaml_patch"] is None
    assert result["patch_unavailable_reason"]
