CREATE TABLE incidents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id text NOT NULL UNIQUE CHECK (
        char_length(event_id) <= 256 AND event_id ~ '^[A-Za-z0-9:._-]+$'
    ),
    namespace text NOT NULL CHECK (namespace = 'target-apps'),
    pod_name text NOT NULL,
    pod_uid text NOT NULL,
    container_name text,
    incident_type text NOT NULL CHECK (
        incident_type IN ('OOMKilled', 'CrashLoopBackOff', 'Http5xxSpike', 'KubernetesWarning')
    ),
    observed_at timestamptz NOT NULL,
    signal jsonb NOT NULL CHECK (jsonb_typeof(signal) = 'object'),
    status text NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'NEEDS_REVIEW', 'APPROVED')),
    approved_by text,
    approved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        (status = 'APPROVED' AND approved_by IS NOT NULL AND approved_at IS NOT NULL)
        OR (status <> 'APPROVED' AND approved_by IS NULL AND approved_at IS NULL)
    )
);
