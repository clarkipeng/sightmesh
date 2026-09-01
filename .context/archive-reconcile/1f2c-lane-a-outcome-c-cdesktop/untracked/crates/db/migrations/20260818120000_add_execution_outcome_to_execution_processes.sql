ALTER TABLE execution_processes ADD COLUMN outcome_class TEXT
    CHECK (outcome_class IN ('quota_exhausted', 'auth_expired', 'auth_invalid',
        'model_unavailable', 'rate_limited_transient', 'network_transient',
        'user_stopped', 'task_failed', 'unknown'));
ALTER TABLE execution_processes ADD COLUMN provider_code TEXT;
ALTER TABLE execution_processes ADD COLUMN retry_after_seconds INTEGER;
ALTER TABLE execution_processes ADD COLUMN resets_at TEXT;
ALTER TABLE execution_processes ADD COLUMN binding_scope TEXT
    CHECK (binding_scope IN ('account', 'route', 'global'));
ALTER TABLE execution_processes ADD COLUMN safe_message TEXT;
