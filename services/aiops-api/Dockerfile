FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12-slim AS build
COPY --from=uv /uv /bin/uv
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY packages/shared packages/shared
COPY services services
RUN uv sync --locked --no-editable --package atienel-aiops-api
FROM python:3.12-slim
WORKDIR /app
COPY --from=build /workspace/.venv /workspace/.venv
COPY services/aiops-api/src src
ENV PATH="/workspace/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
