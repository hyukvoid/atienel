FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12-slim AS build
COPY --from=uv /uv /bin/uv
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY packages/shared packages/shared
COPY services services
RUN uv sync --locked --no-editable --package atienel-mcp-server

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /workspace/.venv /workspace/.venv
COPY services/mcp-server/src src
COPY data/runbooks runbooks
RUN mkdir -m 0755 /model
ADD --chmod=0444 --checksum=sha256:dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88 https://huggingface.co/intfloat/multilingual-e5-small/resolve/614241f622f53c4eeff9890bdc4f31cfecc418b3/onnx/model_qint8_avx512_vnni.onnx /model/model.onnx
ADD --chmod=0444 --checksum=sha256:cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865 https://huggingface.co/intfloat/multilingual-e5-small/resolve/614241f622f53c4eeff9890bdc4f31cfecc418b3/sentencepiece.bpe.model /model/sentencepiece.bpe.model
ENV PATH="/workspace/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER 65532:65532
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--ssl-certfile", "/tls/tls.crt", "--ssl-keyfile", "/tls/tls.key", "--no-access-log"]
