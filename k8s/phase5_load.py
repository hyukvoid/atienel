"""Run bounded k6 traffic and correlate it with the local k3d pipeline."""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from phase2_verify import kube, pod_for, psql, require

K6_IMAGE = "grafana/k6:1.5.0"
LOAD_SCRIPT = Path("tests/load/aiops_api_hpa.js")

COUNTS = """
SELECT json_build_object(
  'tickets',count(*), 'jobs',count(j.id), 'reports',count(r.id),
  'succeeded',count(*) FILTER (WHERE j.state='SUCCEEDED'),
  'failed',count(*) FILTER (WHERE j.state='FAILED'),
  'unpublished',count(*) FILTER (WHERE j.published_at IS NULL),
  'max_outbox_ms',round(max(extract(epoch FROM (j.published_at-j.created_at))*1000),2),
  'max_diagnosis_ms',round(max(extract(epoch FROM (r.created_at-i.created_at))*1000),2))
FROM incidents i LEFT JOIN investigation_jobs j ON i.id=j.incident_id
LEFT JOIN investigation_reports r ON i.id=r.incident_id WHERE i.event_id LIKE '{prefix}%';
"""


def sample(prefix):
    connections = psql("""
        SELECT coalesce(json_agg(s), '[]') FROM (
            SELECT usename, state, wait_event_type, count(*) FROM pg_stat_activity
            WHERE datname = 'atienel' AND backend_type = 'client backend'
              AND pid <> pg_backend_pid()
            GROUP BY usename,state,wait_event_type ORDER BY usename,state,wait_event_type
        ) s;
    """)
    hpa = json.loads(kube("aiops-core", "get", "hpa", "aiops-api", "-o", "json"))
    stats = {}
    for ns in ("aiops-core", "aiops-data"):
        pods = json.loads(kube(ns, "get", "pods", "-o", "json"))["items"]
        stats[ns] = {
            "top": kube(ns, "top", "pods", "--no-headers").splitlines(),
            "restarts": {
                pod["metadata"]["name"]: sum(
                    item["restartCount"]
                    for item in pod.get("status", {}).get("containerStatuses", [])
                )
                for pod in pods
                if pod["status"]["phase"] == "Running"
            },
        }
    queue = kube(
        "aiops-data",
        "exec",
        "valkey-0",
        "--",
        "valkey-cli",
        "--json",
        "XINFO",
        "GROUPS",
        "aiops:incidents:v1",
    )
    group = next(group for group in json.loads(queue) if group["name"] == "aiops-workers-v1")
    return {
        "timestamp": time.time(),
        "postgres_connections": json.loads(connections),
        "resources": stats,
        "hpa": hpa["status"],
        "run": json.loads(psql(COUNTS.format(prefix=prefix))),
        "pending": group,
        "stream_length": int(
            kube("aiops-data", "exec", "valkey-0", "--", "valkey-cli", "XLEN", "aiops:incidents:v1")
        ),
    }


def run_profile(cluster, path, seconds):
    return subprocess.Popen(
        ["tests/profiling/profile_worker.sh", cluster, str(path), str(seconds)],
        env={**os.environ, "KUBECTL": sys.argv[1]},
    )


def run_load():
    context = sys.argv[2]
    require(context.startswith("k3d-"), "Load testing requires a local k3d context")
    cluster = context.removeprefix("k3d-")
    require(re.fullmatch(r"[a-z0-9-]+", cluster), "Expected local cluster name")
    deployment = json.loads(kube("aiops-core", "get", "deploy", "aiops-worker", "-o", "json"))
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    require(
        {"name": "DIAGNOSIS_MODE", "value": "verification"} in env,
        "Deploy verification mode before running load",
    )
    out = Path(tempfile.mkdtemp(prefix="atienel-phase5-"))
    prefix = f"phase5:{uuid4().hex}:"
    print(f"Results directory {out}", flush=True)
    print(f"Run prefix {prefix}", flush=True)
    started = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    samples = [sample(prefix)]
    require(samples[0]["stream_length"] == 0, "Let the existing queue drain before the load test")
    profiling = os.environ.get("PHASE5_PROFILE", "0") == "1"
    if profiling:
        subprocess.run(
            ["docker", "build", "-t", "atienel-py-spy:phase5", "tests/profiling"], check=True
        )
        with run_profile(cluster, out / "worker-idle.svg", 10) as idle:
            require(idle.wait() == 0, "Idle profile failed")

    pod = pod_for("chaos-app")["metadata"]
    ip = kube("aiops-core", "get", "svc", "aiops-api", "-o", "jsonpath={.spec.clusterIP}").strip()
    raw = json.loads(kube("aiops-core", "get", "secret", "api-tokens", "-o", "json"))["data"]
    inputs = {
        "API_URL": f"http://{ip}:8000",
        "INGEST_TOKEN": base64.b64decode(raw["ingest"]).decode(),
        "POD_NAME": pod["name"],
        "POD_UID": pod["uid"],
        "RUN_ID": prefix.rstrip(":"),
        "HOLD_SECONDS": os.environ.get("PHASE5_HOLD_SECONDS", "35"),
        "FRESH_VUS": os.environ.get("PHASE5_FRESH_VUS", "3"),
        "REPLAY_RATE": os.environ.get("PHASE5_REPLAY_RATE", "60"),
    }
    args = ["docker", "run", "--rm", "-i", "--network", f"container:k3d-{cluster}-server-0"]
    for key in inputs:
        args.extend(["-e", key])
    args.extend([K6_IMAGE, "run", "--quiet", "-"])
    with (
        LOAD_SCRIPT.open() as source,
        (out / "k6.json").open("w") as summary,
        subprocess.Popen(args, stdin=source, stdout=summary, env={**os.environ, **inputs}) as load,
    ):
        if profiling:
            time.sleep(4)
            profiler = run_profile(cluster, out / "worker-load.svg", 20)
        else:
            profiler = None
        while load.poll() is None:
            samples.append(sample(prefix))
            time.sleep(5)
        status = load.returncode
        load_end = time.time()
        if profiler:
            require(profiler.wait() == 0, "Load profile failed")

    deadline = time.monotonic() + 300
    drained = None
    while time.monotonic() < deadline:
        reading = sample(prefix)
        samples.append(reading)
        run = reading["run"]
        if drained is None and reading["stream_length"] == 0 and reading["pending"]["pending"] == 0:
            drained = time.time() - load_end
        replicas = [s["hpa"]["currentReplicas"] for s in samples]
        observing_downscale = max(replicas) > 1 and replicas[-1] > 1
        if drained is not None and run["tickets"] == run["reports"] and not observing_downscale:
            break
        time.sleep(5)

    worker_logs = kube("aiops-core", "logs", "deployment/aiops-worker", f"--since-time={started}")
    metrics = [line for line in worker_logs.splitlines() if "queue_metrics" in line]
    events = kube(
        "aiops-core",
        "get",
        "events",
        "--field-selector=involvedObject.name=aiops-api,involvedObject.kind=HorizontalPodAutoscaler",
    )
    pg_logs = kube("aiops-data", "logs", "postgresql-0", f"--since-time={started}")
    result = {
        "statement_timeouts": sum(
            "statement timeout" in line.lower() for line in pg_logs.splitlines()
        ),
        "run_prefix": prefix,
        "samples": samples,
        "queue_metrics": metrics,
        "drain_seconds": drained,
        "hpa_events": events,
        "k6_exit": status,
    }
    (out / "telemetry.json").write_text(json.dumps(result, indent=2))
    summary = json.loads((out / "k6.json").read_text())
    fresh = int(summary["metrics"]["fresh_202"]["values"]["count"])
    replay = int(summary["metrics"]["replay_202"]["values"]["count"])
    duplicate_rows = int(
        psql(f"""
        SELECT count(*) FROM (
            SELECT event_id FROM incidents WHERE event_id LIKE '{prefix}%'
            GROUP BY event_id HAVING count(*) > 1
            UNION ALL
            SELECT i.event_id FROM investigation_reports r JOIN incidents i ON i.id = r.incident_id
            WHERE i.event_id LIKE '{prefix}%' GROUP BY i.event_id HAVING count(*) > 1
        ) d;
    """)
    )
    require(status == 0, f"k6 thresholds failed. Inspect {out}")
    require(fresh >= 30 and replay >= 5 and duplicate_rows == 0, "Load volume or uniqueness failed")
    require(
        drained is not None
        and run["tickets"] == run["jobs"] == run["reports"] == run["succeeded"] == fresh + 1
        and run["failed"] == run["unpublished"] == 0,
        f"DB/Stream reconciliation failed: {run}",
    )
    print(
        json.dumps(
            {
                "db": run,
                "duplicates": duplicate_rows,
                "drain_seconds": drained,
                "hpa_peak": max(replicas),
                "fresh_202": fresh,
                "replay_202": replay,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    run_load()
