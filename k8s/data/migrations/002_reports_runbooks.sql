CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vectorscale;

CREATE TABLE investigation_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid NOT NULL UNIQUE REFERENCES incidents(id),
    suspected_cause text NOT NULL CHECK (length(btrim(suspected_cause)) > 0),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array'),
    citations jsonb NOT NULL CHECK (jsonb_typeof(citations) = 'array'),
    limitations jsonb NOT NULL CHECK (jsonb_typeof(limitations) = 'array'),
    recommended_yaml_patch text,
    patch_unavailable_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(recommended_yaml_patch, patch_unavailable_reason) = 1)
);

CREATE TABLE runbook_chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_kind text NOT NULL CHECK (source_kind IN ('RUNBOOK', 'POSTMORTEM')),
    source_uri text NOT NULL,
    source_revision text NOT NULL,
    title text NOT NULL,
    section text NOT NULL,
    chunk_no integer NOT NULL CHECK (chunk_no >= 0),
    body text NOT NULL CHECK (length(btrim(body)) > 0),
    embedding vector(384) NOT NULL CHECK (vector_norm(embedding) > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_uri, source_revision, chunk_no)
);

CREATE INDEX runbook_chunks_embedding_diskann
    ON runbook_chunks USING diskann (embedding vector_cosine_ops);
