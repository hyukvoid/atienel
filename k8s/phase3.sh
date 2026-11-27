#!/usr/bin/env bash
set -euo pipefail

kubectl_bin=$1
cluster=$2
context="k3d-$cluster"

k() { "$kubectl_bin" --context "$context" "$@"; }
core() { k -n aiops-core "$@"; }
data() { k -n aiops-data "$@"; }

credentials() (
    umask 077
    temp=$(mktemp -d)
    trap 'rm -rf -- "$temp"' EXIT
    data get secret data-credentials -o jsonpath='{.data.mcp}' |
        base64 -d > "$temp/password"
    test -s "$temp/password"
    core create secret generic mcp-db --from-file=password="$temp/password"         --dry-run=client -o yaml | core apply --server-side -f -

    if [[ -z $(core get secret mcp-server-tls --ignore-not-found -o name) ]] ||
        ! core get secret mcp-server-tls -o jsonpath='{.data.tls\.crt}' |
            base64 -d | openssl x509 -checkend 604800 -noout >/dev/null; then
        openssl req -x509 -newkey rsa:2048 -nodes -days 365             -subj '/CN=Atienel local MCP CA' -addext 'basicConstraints=critical,CA:TRUE'             -keyout "$temp/ca.key" -out "$temp/ca.crt" 2> "$temp/openssl.log"
        openssl req -new -newkey rsa:2048 -nodes -subj '/CN=mcp-server.aiops-core.svc'             -keyout "$temp/tls.key" -out "$temp/request.csr" 2>> "$temp/openssl.log"
        cat > "$temp/server.ext" <<'EXT'
basicConstraints=critical,CA:FALSE
extendedKeyUsage=serverAuth
subjectAltName=DNS:mcp-server.aiops-core.svc,IP:127.0.0.1
EXT
        openssl x509 -req -in "$temp/request.csr" -CA "$temp/ca.crt" -CAkey "$temp/ca.key"             -CAcreateserial -days 180 -extfile "$temp/server.ext"             -out "$temp/tls.crt" 2>> "$temp/openssl.log"
        core create secret tls mcp-server-tls --cert="$temp/tls.crt" --key="$temp/tls.key"             --dry-run=client -o yaml | core apply --server-side -f -
    else
        core get secret mcp-access -o jsonpath='{.data.ca\.crt}' | base64 -d > "$temp/ca.crt"
        test -s "$temp/ca.crt"
    fi
    if [[ -n $(core get secret mcp-access --ignore-not-found -o name) ]]; then
        core get secret mcp-access -o jsonpath='{.data.token}' | base64 -d > "$temp/token"
    else
        openssl rand -hex 32 | tr -d '\n' > "$temp/token"
    fi
    test -s "$temp/token"
    core create secret generic mcp-access --from-file=token="$temp/token"         --from-file=ca.crt="$temp/ca.crt" --dry-run=client -o yaml |
        core apply --server-side -f -
)

seed() {
    local image=$1 job
    job=$(sed "s|atienel-mcp-server:phase3|$image|" k8s/data/seed-runbooks.yaml |
        data create -f - -o name)
    local state
    for _ in {1..125}; do
        state=$(data get "$job" -o jsonpath='{.status.conditions[?(@.status=="True")].type}')
        if [[ $state == *Complete* ]]; then
            data logs "$job"
            return 0
        fi
        [[ $state != *Failed* ]] || break
        sleep 2
    done
    data logs "$job" --all-containers=true || true
    return 1
}

inventory() {
    data exec postgresql-0 -- psql -XAt -v ON_ERROR_STOP=1 -U postgres -d atienel -c "
        SELECT count(*), min(vector_dims(embedding)), max(vector_dims(embedding)),
            md5(string_agg(id::text, ',' ORDER BY id)) FROM runbook_chunks;"
}

case "$3" in
    apply)
        k get nodes
        ./k8s/phase1.sh "$kubectl_bin" "$context" network
        docker build -t atienel-mcp-server:phase3 -f services/mcp-server/Dockerfile .
        image_id=$(docker image inspect -f '{{.Id}}' atienel-mcp-server:phase3)
        image="atienel-mcp-server:phase3-${image_id#sha256:}"
        docker tag atienel-mcp-server:phase3 "$image"
        k3d image import -c "$cluster" "$image"
        credentials
        seed "$image"
        sed "s|atienel-mcp-server:phase3|$image|" k8s/apps/mcp-server.yaml | core apply -f -
        core rollout restart deployment/mcp-server
        core rollout status deployment/mcp-server --timeout=150s
        ;;
    verify)
        subject=system:serviceaccount:aiops-core:mcp-server
        [[ $(k auth can-i --as="$subject" -n target-apps get pods --subresource=log) == yes ]]
        [[ $(k auth can-i --as="$subject" -n target-apps delete pods || true) == no ]]
        echo "MCP ServiceAccount: Pod log read allowed, Pod delete denied"
        before=$(inventory)
        [[ $before =~ ^[1-9][0-9]*\|384\|384\| ]]
        image=$(core get deployment mcp-server -o jsonpath='{.spec.template.spec.containers[0].image}')
        seed "$image"
        [[ $(inventory) == "$before" ]]
        echo "384-dimensional seed inventory unchanged after rerun"
        PYTHONPATH=services/mcp-server uv run --locked --package atienel-mcp-server \
            python k8s/phase3_verify.py "$kubectl_bin" "$context" "${@:4}"
        ;;
    *)
        exit 2
        ;;
esac
