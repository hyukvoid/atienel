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
