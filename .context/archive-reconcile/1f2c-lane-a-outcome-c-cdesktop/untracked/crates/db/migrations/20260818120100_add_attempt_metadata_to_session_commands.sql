ALTER TABLE session_commands ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1;
ALTER TABLE session_commands ADD COLUMN route_id TEXT;
ALTER TABLE session_commands ADD COLUMN auth_binding_id TEXT;
ALTER TABLE session_commands ADD COLUMN account_alias TEXT;
ALTER TABLE session_commands ADD COLUMN executor TEXT;
ALTER TABLE session_commands ADD COLUMN model TEXT;
ALTER TABLE session_commands ADD COLUMN billing_class TEXT;
ALTER TABLE session_commands ADD COLUMN policy_digest TEXT;
ALTER TABLE session_commands ADD COLUMN predecessor_execution_process_id BLOB
    REFERENCES execution_processes(id) ON DELETE SET NULL;
