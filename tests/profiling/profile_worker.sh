#!/usr/bin/env bash
set -euo pipefail
cluster=$1
output=$2
duration=${3:-15}
node="k3d-$cluster-server-0"
cid=$("${KUBECTL:-kubectl}" --context "k3d-$cluster" -n aiops-core get pod \
    -l app.kubernetes.io/name=aiops-worker \
    -o jsonpath='{.items[0].status.containerStatuses[0].containerID}')
[[ $cid == containerd://* ]]
pid=$(docker exec "$node" crictl inspect "${cid#containerd://}" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["pid"])')
[[ $pid =~ ^[1-9][0-9]*$ ]]
docker run --rm --network none --pid="container:$node" --cap-add SYS_PTRACE \
    -v "$(dirname "$output"):/profiles" atienel-py-spy:phase5 record \
    --pid "$pid" --duration "$duration" --rate 49 --idle \
    --output "/profiles/$(basename "$output")"
