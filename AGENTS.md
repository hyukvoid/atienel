## 1. Project Identity
- **Name:** Atienel (Kubernetes AIOps Incident Sentinel)
- **Goal:** Detect real K8s pod failures (OOMKilled, CrashLoopBackOff, 5xx spikes) via Hot Path and diagnose root causes using an async AI Agent + Read-Only MCP + pgvector RAG on Cold Path.

## 2. Spec-First Workflow (Mandatory)
- NEVER write or modify service code before reading the relevant file in `specs/`.
- Update `specs/` first whenever changing API contracts, Valkey stream schemas, DB tables, or K8s RBAC policies.

## 3. Tech Stack & Version Lock (2026 Standard)
- **Package/Lint:** `uv` workspace + `ruff` (Python 3.12+)
- **API:** FastAPI + `psycopg` (v3 async) + Pydantic v2
- **Queue/Cache:** Valkey Streams (never use sync polling in Hot Path)
- **DB & Vector:** PostgreSQL 16 + `pgvector` + `pgvectorscale`
- **MCP Server:** FastMCP 4.x using **Stateless Streamable HTTP (`/mcp`)**. DO NOT use deprecated `/sse`.
- **Agent:** LangGraph 1.2 (state graph + HITL) + Pydantic AI 2.0 (strict typed outputs)
- **Infra:** Local `k3d` (k3s), 3 namespaces (`target-apps`, `aiops-core`, `aiops-data`).

## 4. Non-Negotiable Architecture Guardrails
1. **Hot Path / Cold Path Separation:** `aiops-api` (`POST /incidents`) MUST NOT call LLMs or MCP directly. Record to PostgreSQL, push to Valkey Streams, and return `202 Accepted` immediately.
2. **Strict Read-Only RBAC:** `mcp-server` ServiceAccount MUST ONLY have `get`, `list`, `watch` permissions on `pods`, `pods/log`, `events`. Write/delete verbs are strictly forbidden.
3. **16GB RAM Local Budget:** Every Kubernetes Deployment YAML MUST define explicit `resources.requests` and `resources.limits`.
4. **No Hallucinated APIs:** Verify latest library signatures via Context7 MCP or official docs before implementing FastMCP / LangGraph / Pydantic AI code.
