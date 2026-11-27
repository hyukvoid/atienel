#!/usr/bin/env bash
set -euo pipefail

kubectl_bin=$1
cluster=$2
context="k3d-$cluster"
core() { "$kubectl_bin" --context "$context" -n aiops-core "$@"; }
data() { "$kubectl_bin" --context "$context" -n aiops-data "$@"; }

storage() (
    umask 077
    temp=$(mktemp -d)
    trap 'rm -rf -- "$temp"' EXIT
    data get secret data-credentials -o jsonpath='{.data.worker}' | base64 -d > "$temp/postgres"
    data get secret data-credentials -o jsonpath='{.data.valkey}' | base64 -d > "$temp/valkey"
    test -s "$temp/postgres" && test -s "$temp/valkey"
    core create secret generic worker-storage --from-file=postgres="$temp/postgres"         --from-file=valkey="$temp/valkey" --dry-run=client -o yaml |
        core apply --server-side -f -
)

case "$3" in
    apply)
        mode=${DIAGNOSIS_MODE:-verification}
        case "$mode" in
            verification) ;;
            provider)
                : "${PROVIDER_CIDR:?Set PROVIDER_CIDR for the configured HTTPS provider}"
                python3 -c 'import ipaddress, sys; ipaddress.ip_network(sys.argv[1])' "$PROVIDER_CIDR"
                if [[ -f secrets/phase4-llm.env ]]; then
                    core create secret generic worker-llm --from-env-file=secrets/phase4-llm.env                         --dry-run=client -o yaml | core apply --server-side -f -
                fi
                for field in LLM_API_KEY LLM_MODEL LLM_BASE_URL; do
                    core get secret worker-llm -o jsonpath="{.data.$field}" |
                        python3 -c 'import sys; sys.exit(not bool(sys.stdin.read().strip()))'
                done
                ;;
            *) echo "Unknown DIAGNOSIS_MODE" >&2; exit 2 ;;
        esac

        ./k8s/phase1.sh "$kubectl_bin" "$context" network
        core rollout status deployment/mcp-server --timeout=90s
        core get secret mcp-access -o name
        storage

        images=()
        for service in aiops-api aiops-worker; do
            tag="atienel-$service:phase4"
            docker build -t "$tag" -f "services/$service/Dockerfile" .
            digest=$(docker image inspect -f '{{.Id}}' "$tag")
            pinned="$tag-${digest#sha256:}"
            docker tag "$tag" "$pinned"
            images+=("$pinned")
        done
        k3d image import -c "$cluster" "${images[@]}"

        {
            cat <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: phase4-worker-provider
  namespace: aiops-core
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: aiops-worker
  policyTypes: [Egress]
YAML
            if [[ $mode == provider ]]; then
                cat <<YAML
  egress:
  - to:
    - ipBlock:
        cidr: "$PROVIDER_CIDR"
    ports:
    - protocol: TCP
      port: 443
YAML
            else
                printf '  egress: []\n'
            fi
        } | core apply -f -
        core set image deployment/aiops-api "aiops-api=${images[0]}"
        core rollout status deployment/aiops-api --timeout=120s
        sed -e "s|atienel-aiops-worker:phase4|${images[1]}|"             -e "s|value: verification$|value: $mode|" k8s/apps/aiops-worker.yaml |
            core apply -f -
        core rollout status deployment/aiops-worker --timeout=140s
        ;;
    verify)
        python3 k8s/phase4_verify.py "$kubectl_bin" "$context"
        ;;
    *) exit 2 ;;
esac
