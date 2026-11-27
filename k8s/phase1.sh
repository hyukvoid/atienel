#!/usr/bin/env bash
set -euo pipefail

kubectl_bin=$1
context=$2

k() { "$kubectl_bin" --context "$context" "$@"; }
data() { k -n aiops-data "$@"; }

apply_network_policies() {
    local service_ip node_ip
    service_ip=$(k -n default get service kubernetes -o jsonpath='{.spec.clusterIP}')
    node_ip=$(k get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
    test -n "$service_ip" && test -n "$node_ip"
    sed -e "s/KUBERNETES_SERVICE_IP/$service_ip/g" -e "s/KUBERNETES_NODE_IP/$node_ip/g" \
        k8s/network-policies.yaml | k apply -f -
}

apply() {
    k apply -f k8s/namespaces.yaml
    k apply -f k8s/rbac/

    apply_network_policies
    local existing
    if [[ -n ${PROVIDER_CIDR:-} ]]; then
        k apply -f - <<YAML
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: worker-provider-egress
  namespace: aiops-core
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: aiops-worker
  policyTypes: [Egress]
  egress:
    - to:
        - ipBlock:
            cidr: "$PROVIDER_CIDR"
      ports:
        - protocol: TCP
          port: 443
YAML
    fi

    existing=$(data get secret data-credentials --ignore-not-found -o name)
    if [[ -z $existing ]]; then
        for key in postgres valkey api worker mcp seed reconciler; do
            printf '%s=%s\n' "$key" "$(openssl rand -hex 32)"
        done | data create secret generic data-credentials --from-env-file=/dev/stdin
    fi
    data create configmap data-migrations --from-file=k8s/data/migrations \
        --dry-run=client -o yaml | data apply -f -
    data apply -f k8s/data/postgresql.yaml -f k8s/data/valkey.yaml
    data rollout status statefulset/postgresql --timeout=600s
    data rollout status statefulset/valkey --timeout=300s
    data apply -f k8s/data/bootstrap-jobs.yaml
    data wait --for=condition=complete job/data-bootstrap --timeout=310s
}

verify() {
    data wait --for=condition=Ready pod/postgresql-0 pod/valkey-0 --timeout=180s
    data wait --for=jsonpath='{.status.phase}'=Bound pvc/data-postgresql-0 pvc/data-valkey-0 --timeout=60s
    data wait --for=condition=complete job/data-bootstrap --timeout=60s

    local extensions migrations diskann role_privileges group_info valkey_settings
    extensions=$(data exec postgresql-0 -- psql -XAt -U postgres -d atienel -c \
        "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'vectorscale') ORDER BY extname")
    [[ $extensions == $'vector\nvectorscale' ]]
    migrations=$(data exec postgresql-0 -- psql -XAt -U postgres -d atienel -c \
        'SELECT version FROM atienel_migrations.applied ORDER BY version')
    [[ $migrations == $'1\n2\n3\n4' ]]
    diskann=$(data exec postgresql-0 -- psql -XAt -U postgres -d atienel -c \
        "SELECT am.amname FROM pg_class c JOIN pg_am am ON c.relam = am.oid
         WHERE c.oid = 'public.runbook_chunks_embedding_diskann'::regclass")
    [[ $diskann == diskann ]]
    role_privileges=$(data exec postgresql-0 -- psql -XAt -U postgres -d atienel -c \
        "SELECT has_table_privilege('atienel_mcp', 'runbook_chunks', 'SELECT'),
                has_table_privilege('atienel_mcp', 'runbook_chunks', 'INSERT'),
                has_table_privilege('atienel_mcp', 'incidents', 'SELECT')")
    [[ $role_privileges == 't|f|f' ]]
    printf 'PostgreSQL extensions\n%s\nApplied migrations\n%s\n' "$extensions" "$migrations"
    group_info=$(data exec valkey-0 -- valkey-cli --raw XINFO GROUPS aiops:incidents:v1)
    grep -Fx 'aiops-workers-v1' <<< "$group_info"
    valkey_settings=$(data exec valkey-0 -- sh -ec '
        valkey-cli --raw CONFIG GET appendonly
        valkey-cli --raw CONFIG GET maxmemory
        valkey-cli --raw CONFIG GET maxmemory-policy')
    [[ $valkey_settings == $'appendonly\nyes\nmaxmemory\n201326592\nmaxmemory-policy\nnoeviction' ]]

    local mcp=system:serviceaccount:aiops-core:mcp-server
    k auth can-i --quiet --as="$mcp" get pods --subresource=log -n target-apps
    k auth can-i --quiet --as="$mcp" watch events -n target-apps
    local check verb resource namespace result
    for check in 'patch pods target-apps' 'delete pods target-apps' 'get secrets target-apps' \
                 'get pods aiops-core' 'list pods aiops-data'; do
        read -r verb resource namespace <<< "$check"
        result=$(k auth can-i --as="$mcp" "$verb" "$resource" -n "$namespace") || [[ $result == no ]]
        [[ $result == no ]]
    done

    k wait --for=condition=Available apiservice/v1beta1.metrics.k8s.io --timeout=180s
    k get pods -A
    local unhealthy
    unhealthy=$(k get pods -A --field-selector='status.phase!=Running,status.phase!=Succeeded' -o name)
    [[ -z $unhealthy ]] || { printf 'Pods outside Running or Succeeded:\n%s\n' "$unhealthy" >&2; return 1; }
    data get statefulset,pvc,job
}

case $3 in
    apply) apply ;;
    network) apply_network_policies ;;
    verify) verify ;;
    *) exit 2 ;;
esac
