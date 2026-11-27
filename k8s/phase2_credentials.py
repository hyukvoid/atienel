"""Copy only the Phase 1 credentials needed by each Phase 2 workload."""

import base64
import json
import secrets
import subprocess
import sys

kubectl, context = sys.argv[1:]


def kube(namespace, *args, **kwargs):
    return subprocess.run(
        [kubectl, "--context", context, "-n", namespace, *args],
        check=True,
        text=True,
        **kwargs,
    )


def secret(namespace, name):
    result = kube(
        namespace,
        "get",
        "secret",
        name,
        "--ignore-not-found",
        "-o",
        "json",
        capture_output=True,
    )
    return json.loads(result.stdout)["data"] if result.stdout else None


def apply(name, data):
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": "aiops-core"},
        "type": "Opaque",
        "data": data,
    }
    kube("aiops-core", "apply", "--server-side", "-f", "-", input=json.dumps(manifest))


def main():
    storage = secret("aiops-data", "data-credentials")
    if storage is None:
        raise SystemExit("Run make phase1-apply before deploying Phase 2")
    for role in ("api", "reconciler"):
        apply(f"{role}-storage", {"postgres": storage[role], "valkey": storage["valkey"]})
    tokens = secret("aiops-core", "api-tokens")
    if tokens is None:
        tokens = {
            name: base64.b64encode(secrets.token_hex(32).encode()).decode()
            for name in ("ingest", "reader", "approver")
        }
        apply("api-tokens", tokens)
    apply("watcher-token", {"ingest": tokens["ingest"]})


if __name__ == "__main__":
    main()
