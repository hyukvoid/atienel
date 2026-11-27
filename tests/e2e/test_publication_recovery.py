import json
import time

from phase2_verify import kube, port_forward, psql, verify_recovery, wait_for
from phase4_verify import GROUP, STREAM, WORKER, rollout, valkey


def test_outage_and_abandoned_consumer_recovery(cluster):
    started = time.time()
    try:
        kube("aiops-core", "scale", WORKER, "--replicas=0")
        wait_for(
            lambda: (
                not json.loads(
                    kube(
                        "aiops-core",
                        "get",
                        "pods",
                        "-l",
                        "app.kubernetes.io/name=aiops-worker",
                        "-o",
                        "json",
                    )
                )["items"]
            ),
            "worker stopped",
            timeout=115,
        )
        with port_forward("aiops-core", "deployment/aiops-api", 8000) as api:
            verify_recovery(api, cluster)
        row = json.loads(
            psql(f"""
            SELECT row_to_json(s) FROM (
                SELECT j.incident_id,j.stream_entry_id FROM investigation_jobs j
                JOIN incidents i ON i.id=j.incident_id
                WHERE i.event_id LIKE 'verify:recovery:%'
                  AND i.created_at >= to_timestamp({started})
                ORDER BY i.created_at DESC LIMIT 1
            ) s;
        """)
        )
        entries = valkey(
            "--json",
            "XREADGROUP",
            "GROUP",
            GROUP,
            "phase5-abandoned",
            "COUNT",
            "100",
            "STREAMS",
            STREAM,
            ">",
        )
        assert row["stream_entry_id"] in entries
        assert row["stream_entry_id"] in valkey("XPENDING", STREAM, GROUP, "-", "+", "100")
    finally:
        kube("aiops-core", "scale", WORKER, "--replicas=1")
        rollout()

    def done():
        return psql(f"""
            SELECT count(*) FROM investigation_reports WHERE incident_id='{row["incident_id"]}';
        """) == "1" and not valkey("XRANGE", STREAM, row["stream_entry_id"], row["stream_entry_id"])

    assert wait_for(done, "abandoned PEL entry reclaimed and settled", timeout=120)
