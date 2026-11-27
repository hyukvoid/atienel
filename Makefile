CLUSTER_NAME := atienel
KUBECTL ?= kubectl
K := $(KUBECTL) --context k3d-$(CLUSTER_NAME)
PORT_FORWARD ?= 5432:5432
FORWARD_SERVICE ?= postgresql

.PHONY: k3d-up k3d-down context phase1-apply phase1-verify port-forward lint

k3d-up:
	@clusters=$$(k3d cluster list --no-headers) && \
	if printf '%s\n' "$$clusters" | awk '{print $$1}' | grep -Fxq '$(CLUSTER_NAME)'; then \
		k3d cluster start $(CLUSTER_NAME) --wait; \
	else \
		k3d cluster create $(CLUSTER_NAME) --servers 1 --agents 0 --no-lb \
			--k3s-arg '--disable=traefik,servicelb@server:0' --wait; \
	fi
	$(KUBECTL) config use-context k3d-$(CLUSTER_NAME)
	@for i in $$(seq 1 60); do $(K) -n kube-system get deployment/metrics-server >/dev/null 2>&1 && break; sleep 2; done
	$(K) -n kube-system rollout status deployment/metrics-server --timeout=180s

k3d-down:
	k3d cluster delete $(CLUSTER_NAME)

context:
	$(KUBECTL) config current-context
	$(K) get nodes

phase1-apply:
	./k8s/phase1.sh "$(KUBECTL)" "k3d-$(CLUSTER_NAME)" apply

phase1-verify:
	./k8s/phase1.sh "$(KUBECTL)" "k3d-$(CLUSTER_NAME)" verify

port-forward:
	$(K) -n aiops-data port-forward --address 127.0.0.1 svc/$(FORWARD_SERVICE) $(PORT_FORWARD)

lint:
	uv run ruff check .

.PHONY: phase2-apply phase2-verify
phase2-apply:
	./k8s/phase2.sh "$(KUBECTL)" "$(CLUSTER_NAME)" apply

phase2-verify:
	./k8s/phase2.sh "$(KUBECTL)" "$(CLUSTER_NAME)" verify $(PHASE2_VERIFY_ARGS)

.PHONY: phase3-apply phase3-verify
phase3-apply:
	./k8s/phase3.sh "$(KUBECTL)" "$(CLUSTER_NAME)" apply

phase3-verify:
	./k8s/phase3.sh "$(KUBECTL)" "$(CLUSTER_NAME)" verify $(PHASE3_VERIFY_ARGS)

.PHONY: phase4-apply phase4-verify
phase4-apply:
	./k8s/phase4.sh "$(KUBECTL)" "$(CLUSTER_NAME)" apply

phase4-verify:
	./k8s/phase4.sh "$(KUBECTL)" "$(CLUSTER_NAME)" verify

.PHONY: phase5-load phase5-verify phase5-ci phase5-images
phase5-load:
	python3 k8s/phase5_load.py "$(KUBECTL)" "k3d-$(CLUSTER_NAME)"

phase5-verify:
	ATIENEL_E2E=1 KUBECTL="$(KUBECTL)" ATIENEL_CONTEXT="k3d-$(CLUSTER_NAME)" \
		uv run --locked --all-packages --with pytest==8.4.2 pytest -q -rP

phase5-ci:
	uvx ruff@0.14.10 check .
	uvx ruff@0.14.10 format --check packages services k8s tests
	uv run --locked --all-packages --with pytest==8.4.2 pytest -q
	find k8s tests -name '*.sh' -print0 | xargs -0 bash -c 'for script; do bash -n "$$script" || exit; done' _
	python3 -m compileall -q packages services k8s tests
	docker run --rm -i grafana/k6:1.5.0 inspect - < tests/load/aiops_api_hpa.js

phase5-images:
	@set -e; for service in aiops-api aiops-worker event-watcher mcp-server chaos-app; do \
		docker build -f "services/$$service/Dockerfile" -t "atienel-$$service:ci" .; \
	done
	docker build -t atienel-py-spy:phase5 tests/profiling
