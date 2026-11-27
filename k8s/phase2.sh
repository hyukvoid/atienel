#!/usr/bin/env bash
set -euo pipefail

kubectl_bin=$1
cluster=$2
context="k3d-$cluster"

k() { "$kubectl_bin" --context "$context" "$@"; }

case "$3" in
    apply)
        k get nodes
        ./k8s/phase1.sh "$kubectl_bin" "$context" network
        images=()
        for service in chaos-app aiops-api event-watcher; do
            tag="atienel-$service:phase2"
            docker build -t "$tag" -f "services/$service/Dockerfile" .
            images+=("$tag")
        done
        k3d image import -c "$cluster" "${images[@]}"

        k -n aiops-data exec -i postgresql-0 -- psql -X -v ON_ERROR_STOP=1 -U postgres \
            -d atienel < k8s/data/migrations/005_reconciler_lease.sql

        python3 k8s/phase2_credentials.py "$kubectl_bin" "$context"
        k apply -f k8s/apps/chaos-app.yaml -f k8s/apps/hot-path.yaml -f k8s/hpa/aiops-api.yaml
        for namespace in target-apps aiops-core; do
            if [[ $namespace == target-apps ]]; then
                deployments=(chaos-app chaos-oom-fixture chaos-crash-fixture)
            else
                deployments=(aiops-api outbox-reconciler event-watcher)
            fi
            for name in "${deployments[@]}"; do
                k -n "$namespace" rollout restart "deployment/$name"
                k -n "$namespace" rollout status "deployment/$name" --timeout=120s
            done
        done
        ;;
    verify)
        python3 k8s/phase2_verify.py "$kubectl_bin" "$context" "${@:4}"
        ;;
    *)
        exit 2
        ;;
esac
