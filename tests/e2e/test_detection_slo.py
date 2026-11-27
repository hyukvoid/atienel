import os
import time
from uuid import UUID

import pytest
from phase2_verify import http, kube, pod_for, port_forward, psql, wait_for

SCENARIOS = (
    ("chaos-oom-fixture", "/faults/oom", "OOMKilled"),
    ("chaos-crash-fixture", "/faults/crashloop", "CrashLoopBackOff"),
    ("chaos-app", "/faults/http-500", "Http5xxSpike"),
)


def replace(name):
    kube("target-apps", "rollout", "restart", f"deployment/{name}")
    kube("target-apps", "rollout", "status", f"deployment/{name}", "--timeout=90s")


@pytest.mark.skipif(os.environ.get("PHASE5_SLO") != "1", reason="extended live detection sample")
def test_thirty_real_detection_samples(cluster):
    del cluster  # Selects the verification cluster before changing any fixture.
    measurements = []
    for fixture, path, signal in SCENARIOS:
        scenario_times = []
        for _ in range(10):
            replace(fixture)
            uid = UUID(pod_for(fixture)["metadata"]["uid"])
            if signal == "Http5xxSpike":
                time.sleep(7)  # Establish a baseline on the newly created Pod.
            with port_forward("target-apps", "svc/chaos-app", 8080) as chaos:
                if signal == "Http5xxSpike":
                    for _ in range(10):
                        assert http(f"{chaos}{path}")[0] == 500
                else:
                    assert http(f"{chaos}{path}", method="POST")[0] == 202

            def accepted(uid=uid, signal=signal):
                return psql(f"""
                    SELECT extract(epoch FROM j.published_at - i.observed_at)
                    FROM incidents i JOIN investigation_jobs j ON i.id=j.incident_id
                    WHERE i.pod_uid='{uid}' AND i.incident_type='{signal}'
                      AND j.published_at IS NOT NULL ORDER BY j.published_at LIMIT 1;
                """)

            result = float(wait_for(accepted, f"{signal} published", timeout=60))
            scenario_times.append(result)
        measurements.extend(scenario_times)
        print(f"SC-001 {signal}: {len(scenario_times)} samples, max {max(scenario_times):.3f} s")
        if fixture != "chaos-app":
            replace(fixture)
    assert sum(observed <= 30 for observed in measurements) >= 29
