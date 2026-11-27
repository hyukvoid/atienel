CREATE TABLE investigation_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid NOT NULL UNIQUE REFERENCES incidents(id),
    state text NOT NULL DEFAULT 'QUEUED'
        CHECK (state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    stream_entry_id text,
    published_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    execution_token uuid,
    execution_expires_at timestamptz,
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((stream_entry_id IS NULL) = (published_at IS NULL)),
    CHECK (
        (state = 'RUNNING' AND execution_token IS NOT NULL AND execution_expires_at IS NOT NULL)
        OR (state <> 'RUNNING' AND execution_token IS NULL AND execution_expires_at IS NULL)
    )
);

CREATE INDEX investigation_jobs_unpublished
    ON investigation_jobs (created_at, id)
    WHERE published_at IS NULL AND state = 'QUEUED';

CREATE TABLE api_idempotency (
    principal_id text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('create_incident', 'approve_incident')),
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'),
    request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
    incident_id uuid REFERENCES incidents(id),
    response_status integer,
    response_body jsonb,
    response_location text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, operation, idempotency_key),
    CHECK (
        (response_status IS NULL AND response_body IS NULL AND response_location IS NULL)
        OR (
            response_status IS NOT NULL AND incident_id IS NOT NULL
            AND response_body IS NOT NULL AND jsonb_typeof(response_body) = 'object'
            AND (
                (operation = 'create_incident' AND response_status = 202 AND response_location IS NOT NULL)
                OR (operation = 'approve_incident' AND response_status = 200 AND response_location IS NULL)
            )
        )
    )
);

CREATE INDEX api_idempotency_incident ON api_idempotency (incident_id);
