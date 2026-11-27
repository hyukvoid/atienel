CLUSTER_NAME := atienel

.PHONY: k3d-up k3d-down lint

k3d-up:
	k3d cluster create $(CLUSTER_NAME) --agents 2 --port "8080:80@loadbalancer"

k3d-down:
	k3d cluster delete $(CLUSTER_NAME)

lint:
	uv run ruff check .
