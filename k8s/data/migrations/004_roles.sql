\getenv api_password DB_API_PASSWORD
\getenv worker_password DB_WORKER_PASSWORD
\getenv mcp_password DB_MCP_PASSWORD
\getenv seed_password DB_SEED_PASSWORD
\getenv reconciler_password DB_RECONCILER_PASSWORD

REVOKE CONNECT, TEMPORARY ON DATABASE atienel FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE ROLE atienel_api LOGIN PASSWORD :'api_password';
CREATE ROLE atienel_worker LOGIN PASSWORD :'worker_password';
CREATE ROLE atienel_mcp LOGIN PASSWORD :'mcp_password';
CREATE ROLE atienel_seed LOGIN PASSWORD :'seed_password';
CREATE ROLE atienel_reconciler LOGIN PASSWORD :'reconciler_password';

GRANT CONNECT ON DATABASE atienel TO
    atienel_api, atienel_worker, atienel_mcp, atienel_seed, atienel_reconciler;
GRANT USAGE ON SCHEMA public TO
    atienel_api, atienel_worker, atienel_mcp, atienel_seed, atienel_reconciler;

GRANT SELECT, INSERT ON incidents, investigation_jobs, api_idempotency TO atienel_api;
GRANT UPDATE (status, approved_by, approved_at, updated_at) ON incidents TO atienel_api;
GRANT UPDATE (stream_entry_id, published_at, updated_at) ON investigation_jobs TO atienel_api;
GRANT UPDATE (incident_id, response_status, response_body, response_location)
    ON api_idempotency TO atienel_api;
GRANT SELECT ON investigation_reports TO atienel_api;

GRANT SELECT ON incidents, investigation_jobs TO atienel_worker;
GRANT INSERT ON investigation_reports TO atienel_worker;
GRANT UPDATE (status, updated_at) ON incidents TO atienel_worker;
GRANT UPDATE (state, attempt_count, next_attempt_at, execution_token, execution_expires_at,
    last_error_code, updated_at) ON investigation_jobs TO atienel_worker;

GRANT SELECT ON runbook_chunks TO atienel_mcp;
GRANT SELECT, INSERT, UPDATE ON runbook_chunks TO atienel_seed;
GRANT SELECT ON investigation_jobs TO atienel_reconciler;
GRANT UPDATE (stream_entry_id, published_at, updated_at) ON investigation_jobs TO atienel_reconciler;
