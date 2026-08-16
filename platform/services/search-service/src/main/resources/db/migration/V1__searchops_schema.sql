CREATE TABLE search_strategies (
  id UUID PRIMARY KEY,
  version INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'PUBLISHED', 'ROLLED_BACK', 'RETIRED')),
  config JSONB NOT NULL,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  submitted_at TIMESTAMPTZ,
  approved_by TEXT,
  approved_at TIMESTAMPTZ,
  approval_token_hash TEXT,
  published_at TIMESTAMPTZ,
  supersedes_version INTEGER
);

CREATE UNIQUE INDEX one_published_strategy ON search_strategies ((status)) WHERE status = 'PUBLISHED';

CREATE TABLE search_requests (
  id BIGSERIAL PRIMARY KEY,
  request_id TEXT NOT NULL,
  query TEXT NOT NULL,
  effective_query TEXT NOT NULL,
  locale TEXT NOT NULL,
  filters JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_count INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL,
  top_product_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  strategy_version INTEGER NOT NULL,
  searched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX search_requests_time_idx ON search_requests (searched_at DESC);
CREATE INDEX search_requests_query_idx ON search_requests (lower(query), searched_at DESC);
CREATE UNIQUE INDEX search_request_id_idx ON search_requests (request_id);

CREATE TABLE quality_metrics (
  query_id BIGINT NOT NULL,
  query_text TEXT NOT NULL,
  strategy_version INTEGER NOT NULL,
  precision10 DOUBLE PRECISION NOT NULL,
  recall10 DOUBLE PRECISION NOT NULL,
  mrr10 DOUBLE PRECISION NOT NULL,
  ndcg10 DOUBLE PRECISION NOT NULL,
  zero_result BOOLEAN NOT NULL,
  evaluated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (query_id, strategy_version)
);

CREATE TABLE evaluation_runs (
  id UUID PRIMARY KEY,
  strategy_version INTEGER NOT NULL,
  query_count INTEGER NOT NULL,
  precision10 DOUBLE PRECISION NOT NULL,
  recall10 DOUBLE PRECISION NOT NULL,
  mrr10 DOUBLE PRECISION NOT NULL,
  ndcg10 DOUBLE PRECISION NOT NULL,
  zero_result_rate DOUBLE PRECISION NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_logs (
  id BIGSERIAL PRIMARY KEY,
  actor TEXT NOT NULL,
  request_id TEXT NOT NULL,
  action TEXT NOT NULL,
  idempotency_key TEXT,
  before_version INTEGER,
  after_version INTEGER,
  outcome TEXT NOT NULL,
  details JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX audit_idempotency_idx
  ON audit_logs (action, idempotency_key)
  WHERE idempotency_key IS NOT NULL AND outcome = 'SUCCESS';

CREATE OR REPLACE FUNCTION prevent_audit_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_logs are append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_logs_immutable
  BEFORE UPDATE OR DELETE ON audit_logs
  FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation();

CREATE TABLE idempotency_results (
  idempotency_key TEXT NOT NULL,
  operation TEXT NOT NULL,
  response JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (idempotency_key, operation)
);

INSERT INTO search_strategies (id, version, name, status, config, created_by, published_at)
VALUES (
  '00000000-0000-0000-0000-000000000001',
  1,
  'BM25 baseline',
  'PUBLISHED',
  '{"synonyms":{},"rewrite_rules":[],"pinned_product_ids":[],"blocked_product_ids":[],"brand_boosts":{},"field_weights":{"title":4.0,"brand":2.5,"bullet_point":1.5,"description":1.0,"category":1.2},"minimum_score":0.0}'::jsonb,
  'system',
  now()
) ON CONFLICT DO NOTHING;
