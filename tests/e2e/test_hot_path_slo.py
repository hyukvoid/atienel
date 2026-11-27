from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from phase2_verify import http, kube, pod_for, port_forward, psql, wait_for
from phase4_verify import WORKER, rollout


def test_thirty_incidents_and_five_replays_without_worker(cluster):
    pod = pod_for("chaos-app")["metadata"]
    prefix = f"phase5-offline:{uuid4().hex}:"
    timings = []
    with port_forward("aiops-core", "deployment/aiops-api", 8000) as api:
        try:
            kube("aiops-core", "scale", WORKER, "--replicas=0")
            wait_for(
                lambda: (
                    not kube(
                        "aiops-core",
                        "get",
                        "pods",
                        "-l",
                        "app.kubernetes.io/name=aiops-worker",
                        "-o",
                        "name",
                    ).strip()
                ),
                "worker Pods to stop",
                timeout=115,
            )
            for number in range(30):
                event_id = f"{prefix}{number}"
                event = {
                    "event_id": event_id,
                    "namespace": "target-apps",
                    "pod_name": pod["name"],
                    "pod_uid": pod["uid"],
                    "incident_type": "Http5xxSpike",
                    "observed_at": datetime.now(UTC).isoformat(),
                    "source": "http_metrics",
                    "summary": "Local worker-off acceptance fixture",
                }
                request = {
                    "method": "POST",
                    "token": cluster["ingest"],
                    "key": event_id,
                    "event": event,
                }
                started = perf_counter()
                status, body, headers = http(f"{api}/incidents", **request)
                timings.append(perf_counter() - started)
                assert status == 202
                for _ in range(5):
                    code, replay, replay_headers = http(f"{api}/incidents", **request)
                    assert (code, replay, replay_headers["Location"]) == (
                        status,
                        body,
                        headers["Location"],
                    )

            assert sum(elapsed <= 1 for elapsed in timings) >= 29
            assert (
                psql(f"""
                SELECT count(*),count(DISTINCT event_id),count(j.published_at)
                FROM incidents i JOIN investigation_jobs j ON i.id=j.incident_id
                WHERE event_id LIKE '{prefix}%';
            """)
                == "30|30|30"
            )
            print(f"SC-002 30 tickets and 150 replays, max create {max(timings) * 1000:.1f} ms")
        finally:
            kube("aiops-core", "scale", WORKER, "--replicas=1")
            rollout()

    def report_count():
        return int(
            psql(f"""
            SELECT count(*) FROM investigation_reports r JOIN incidents i ON i.id=r.incident_id
            WHERE event_id LIKE '{prefix}%';
        """)
        )

    assert wait_for(lambda: report_count() == 30, "offline fixture drain", timeout=120)
