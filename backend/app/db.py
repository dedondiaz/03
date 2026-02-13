import os
from contextlib import contextmanager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/saas_ai")

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    ddl = """
    CREATE EXTENSION IF NOT EXISTS "pgcrypto";

    CREATE TABLE IF NOT EXISTS users (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      email TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS tenants (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      name TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS memberships (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      role TEXT NOT NULL CHECK (role IN ('owner','admin','member')),
      UNIQUE(user_id, tenant_id)
    );

    CREATE TABLE IF NOT EXISTS sessions (
      token TEXT PRIMARY KEY,
      user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS tasks (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      created_by UUID NOT NULL REFERENCES users(id),
      title TEXT NOT NULL,
      description TEXT NOT NULL,
      risk_level TEXT NOT NULL CHECK (risk_level IN ('LOW','MEDIUM','HIGH')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS task_runs (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      task_id UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      status TEXT NOT NULL,
      approval_required BOOLEAN NOT NULL DEFAULT FALSE,
      created_by UUID NOT NULL REFERENCES users(id),
      plan_json JSONB,
      verifier_status TEXT,
      verifier_summary TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS approvals (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      run_id UUID NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
      approved_by UUID NOT NULL REFERENCES users(id),
      approved BOOLEAN NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS audit_logs (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      run_id UUID REFERENCES task_runs(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL,
      payload JSONB NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS notes (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      title TEXT NOT NULL,
      body TEXT NOT NULL,
      created_by UUID NOT NULL REFERENCES users(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS tool_invocations (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      run_id UUID NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
      tool_name TEXT NOT NULL,
      args_json JSONB NOT NULL,
      result_json JSONB,
      status TEXT NOT NULL,
      started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      finished_at TIMESTAMPTZ,
      idempotency_key TEXT,
      error_text TEXT,
      external_ref_type TEXT,
      external_ref_id TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS uniq_tool_idempotency ON tool_invocations(tenant_id, run_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

    CREATE TABLE IF NOT EXISTS google_oauth_credentials (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      provider TEXT NOT NULL DEFAULT 'google',
      scopes TEXT[] NOT NULL,
      subject_email TEXT,
      access_token_enc TEXT NOT NULL,
      refresh_token_enc TEXT,
      expiry TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, provider)
    );

    CREATE TABLE IF NOT EXISTS google_oauth_states (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      state TEXT NOT NULL UNIQUE,
      code_verifier TEXT NOT NULL,
      used BOOLEAN NOT NULL DEFAULT FALSE,
      expires_at TIMESTAMPTZ NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS tenant_policies (
      tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
      allowed_email_domains TEXT[] NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS tenant_calendar_settings (
      tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
      timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
      work_start TIME NOT NULL DEFAULT '10:00',
      work_end TIME NOT NULL DEFAULT '18:00',
      work_days INT[] NOT NULL DEFAULT ARRAY[1,2,3,4,5],
      slot_granularity_minutes INT NOT NULL DEFAULT 15,
      meeting_buffer_minutes INT NOT NULL DEFAULT 10,
      default_calendar_id TEXT NOT NULL DEFAULT 'primary',
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS slack_oauth_credentials (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      team_id TEXT NOT NULL,
      team_name TEXT,
      enterprise_id TEXT,
      app_id TEXT,
      bot_user_id TEXT,
      access_token_enc TEXT NOT NULL,
      refresh_token_enc TEXT,
      token_expires_at TIMESTAMPTZ,
      scopes TEXT[] NOT NULL,
      is_primary BOOLEAN NOT NULL DEFAULT TRUE,
      installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, is_primary)
    );

    CREATE TABLE IF NOT EXISTS slack_oauth_states (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      session_id TEXT NOT NULL,
      state TEXT NOT NULL UNIQUE,
      code_verifier_enc TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      expires_at TIMESTAMPTZ NOT NULL,
      used_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS tenant_slack_policies (
      tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
      allowed_channel_ids TEXT[] NOT NULL DEFAULT '{}',
      allow_external_shared BOOLEAN NOT NULL DEFAULT FALSE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS jira_oauth_states (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      session_id TEXT NOT NULL,
      state TEXT NOT NULL UNIQUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      expires_at TIMESTAMPTZ NOT NULL,
      used_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS jira_oauth_credentials (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      cloud_id TEXT NOT NULL,
      site_url TEXT,
      site_name TEXT,
      access_token_enc TEXT NOT NULL,
      refresh_token_enc TEXT,
      token_expires_at TIMESTAMPTZ,
      scopes TEXT[] NOT NULL,
      is_primary BOOLEAN NOT NULL DEFAULT TRUE,
      installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, is_primary)
    );

    CREATE TABLE IF NOT EXISTS tenant_jira_policies (
      tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
      allowed_project_keys TEXT[] NOT NULL DEFAULT '{}',
      allow_write BOOLEAN NOT NULL DEFAULT TRUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );


    CREATE TABLE IF NOT EXISTS notion_oauth_states (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      state TEXT NOT NULL UNIQUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      expires_at TIMESTAMPTZ NOT NULL,
      used_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS notion_credentials (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      access_token_enc TEXT NOT NULL,
      workspace_id TEXT,
      workspace_name TEXT,
      bot_id TEXT,
      connected_by UUID REFERENCES users(id) ON DELETE SET NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (tenant_id)
    );

    CREATE TABLE IF NOT EXISTS notion_documents (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      notion_page_id TEXT NOT NULL,
      title TEXT NOT NULL,
      content TEXT NOT NULL,
      source_url TEXT,
      last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, notion_page_id)
    );




    CREATE TABLE IF NOT EXISTS microsoft_oauth_states (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      session_id TEXT NOT NULL,
      state TEXT NOT NULL UNIQUE,
      code_verifier_enc TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      expires_at TIMESTAMPTZ NOT NULL,
      used_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS microsoft_oauth_credentials (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      account_id TEXT,
      user_principal_name TEXT,
      tenant_directory_id TEXT,
      access_token_enc TEXT NOT NULL,
      refresh_token_enc TEXT,
      token_expires_at TIMESTAMPTZ,
      scopes TEXT[] NOT NULL,
      installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      is_primary BOOLEAN NOT NULL DEFAULT TRUE,
      UNIQUE (tenant_id, is_primary)
    );


    CREATE TABLE IF NOT EXISTS tenant_automation_policies (
      tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
      allowed_domains TEXT[] NOT NULL DEFAULT '{}',
      allowed_path_prefixes TEXT[] NOT NULL DEFAULT '{}',
      allow_mutations BOOLEAN NOT NULL DEFAULT FALSE,
      max_steps INT NOT NULL DEFAULT 25,
      max_runtime_seconds INT NOT NULL DEFAULT 120,
      retention_days INT NOT NULL DEFAULT 14,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS automation_sessions (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      domain TEXT NOT NULL,
      storage_state_enc TEXT NOT NULL,
      created_by UUID NOT NULL REFERENCES users(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS browser_automation_runs (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      linked_run_id UUID REFERENCES task_runs(id) ON DELETE SET NULL,
      session_id UUID REFERENCES automation_sessions(id) ON DELETE SET NULL,
      status TEXT NOT NULL,
      policy_snapshot_json JSONB NOT NULL,
      steps_redacted_json JSONB NOT NULL,
      result_json JSONB,
      final_url TEXT,
      errors JSONB,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS browser_automation_artifacts (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      browser_run_id UUID NOT NULL REFERENCES browser_automation_runs(id) ON DELETE CASCADE,
      kind TEXT NOT NULL,
      step_index INT,
      file_path TEXT NOT NULL,
      sha256 TEXT NOT NULL,
      byte_size BIGINT NOT NULL DEFAULT 0,
      mime_type TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );


    CREATE TABLE IF NOT EXISTS tenant_plans (
      tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
      plan_name TEXT NOT NULL DEFAULT 'pilot',
      limits_json JSONB NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS tenant_usage_daily (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      day DATE NOT NULL,
      api_requests_count BIGINT NOT NULL DEFAULT 0,
      tool_calls_count BIGINT NOT NULL DEFAULT 0,
      llm_tokens_count BIGINT NOT NULL DEFAULT 0,
      workflow_runs_count BIGINT NOT NULL DEFAULT 0,
      automation_runs_count BIGINT NOT NULL DEFAULT 0,
      web_automation_runs_count BIGINT NOT NULL DEFAULT 0,
      web_automation_runtime_seconds BIGINT NOT NULL DEFAULT 0,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, day)
    );
    CREATE INDEX IF NOT EXISTS idx_tenant_usage_daily_tenant_day ON tenant_usage_daily(tenant_id, day);



    CREATE TABLE IF NOT EXISTS tenant_invites (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      email TEXT NOT NULL,
      role TEXT NOT NULL CHECK (role IN ('admin','member')),
      token_hash TEXT NOT NULL UNIQUE,
      expires_at TIMESTAMPTZ NOT NULL,
      created_by UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      accepted_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS tenant_notion_policies (
      tenant_id UUID PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
      allowed_parent_ids TEXT[] NOT NULL DEFAULT '{}',
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS integration_health (
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      integration TEXT NOT NULL,
      connected BOOLEAN NOT NULL DEFAULT FALSE,
      last_success_at TIMESTAMPTZ,
      last_error_at TIMESTAMPTZ,
      last_error_code TEXT,
      last_error_message_redacted TEXT,
      consecutive_failures INT NOT NULL DEFAULT 0,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant_id, integration)
    );
    CREATE INDEX IF NOT EXISTS idx_integration_health_failures ON integration_health(tenant_id, consecutive_failures);

    CREATE TABLE IF NOT EXISTS workflow_templates (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      description TEXT NOT NULL,
      input_schema_json JSONB NOT NULL,
      enabled BOOLEAN NOT NULL DEFAULT TRUE,
      version INT NOT NULL DEFAULT 1,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS workflow_runs (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      template_id TEXT NOT NULL REFERENCES workflow_templates(id) ON DELETE RESTRICT,
      input_json JSONB NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('queued','running','waiting_approval','completed','failed')),
      linked_run_id UUID REFERENCES task_runs(id) ON DELETE SET NULL,
      triggered_by_rule_id UUID,
      summary_text TEXT,
      created_by UUID NOT NULL REFERENCES users(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS automation_rules (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      created_by UUID NOT NULL REFERENCES users(id),
      name TEXT NOT NULL,
      template_id TEXT NOT NULL REFERENCES workflow_templates(id) ON DELETE RESTRICT,
      input_json JSONB NOT NULL,
      trigger_type TEXT NOT NULL CHECK (trigger_type IN ('schedule','gmail_poll')),
      schedule_cron TEXT,
      timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
      quiet_hours_start TEXT,
      quiet_hours_end TEXT,
      enabled BOOLEAN NOT NULL DEFAULT TRUE,
      max_runs_per_day INT NOT NULL DEFAULT 3,
      max_concurrent_runs INT NOT NULL DEFAULT 1,
      last_run_at TIMESTAMPTZ,
      next_run_at TIMESTAMPTZ,
      last_error TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS automation_executions (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
      rule_id UUID NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
      workflow_run_id UUID REFERENCES workflow_runs(id) ON DELETE SET NULL,
      status TEXT NOT NULL CHECK (status IN ('queued','skipped_quiet_hours','skipped_quota','enqueued','failed')),
      scheduled_for TIMESTAMPTZ,
      started_at TIMESTAMPTZ,
      finished_at TIMESTAMPTZ,
      reason TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS idx_automation_rules_due ON automation_rules(tenant_id, enabled, next_run_at);
    CREATE INDEX IF NOT EXISTS idx_automation_executions_rule_created ON automation_executions(tenant_id, rule_id, created_at);

    CREATE INDEX IF NOT EXISTS idx_task_runs_tenant_created ON task_runs(tenant_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_task_runs_tenant_status_created ON task_runs(tenant_id, status, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_workflow_runs_linked_run ON workflow_runs(linked_run_id);
    CREATE INDEX IF NOT EXISTS idx_tool_invocations_tenant_run_started ON tool_invocations(tenant_id, run_id, started_at);
    CREATE INDEX IF NOT EXISTS idx_tool_invocations_tenant_tool_status_started ON tool_invocations(tenant_id, tool_name, status, started_at);
    CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_created ON audit_logs(tenant_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_logs_tenant_event_created ON audit_logs(tenant_id, event_type, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_automation_executions_tenant_created ON automation_executions(tenant_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_browser_runs_tenant_created ON browser_automation_runs(tenant_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_browser_artifacts_tenant_run ON browser_automation_artifacts(tenant_id, browser_run_id);


    ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS triggered_by_rule_id UUID;

    ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS plan_json JSONB;
    ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS verifier_status TEXT;
    ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS verifier_summary TEXT;
    ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;
    ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;
    ALTER TABLE tool_invocations ADD COLUMN IF NOT EXISTS external_ref_type TEXT;
    ALTER TABLE tool_invocations ADD COLUMN IF NOT EXISTS external_ref_id TEXT;
    ALTER TABLE memberships DROP CONSTRAINT IF EXISTS memberships_role_check;
    ALTER TABLE memberships ADD CONSTRAINT memberships_role_check CHECK (role IN ('owner','admin','member'));

    """

    rls = """
    ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tasks FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tasks ON tasks;
    CREATE POLICY tenant_isolation_tasks ON tasks USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE task_runs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE task_runs FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_task_runs ON task_runs;
    CREATE POLICY tenant_isolation_task_runs ON task_runs USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
    ALTER TABLE approvals FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_approvals ON approvals;
    CREATE POLICY tenant_isolation_approvals ON approvals USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_audit_logs ON audit_logs;
    CREATE POLICY tenant_isolation_audit_logs ON audit_logs USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE notes ENABLE ROW LEVEL SECURITY;
    ALTER TABLE notes FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_notes ON notes;
    CREATE POLICY tenant_isolation_notes ON notes USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE tool_invocations ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tool_invocations FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tool_invocations ON tool_invocations;
    CREATE POLICY tenant_isolation_tool_invocations ON tool_invocations USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE google_oauth_credentials ENABLE ROW LEVEL SECURITY;
    ALTER TABLE google_oauth_credentials FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_google_oauth_credentials ON google_oauth_credentials;
    CREATE POLICY tenant_isolation_google_oauth_credentials ON google_oauth_credentials USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE google_oauth_states ENABLE ROW LEVEL SECURITY;
    ALTER TABLE google_oauth_states FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_google_oauth_states ON google_oauth_states;
    CREATE POLICY tenant_isolation_google_oauth_states ON google_oauth_states USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE tenant_policies ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenant_policies FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tenant_policies ON tenant_policies;
    CREATE POLICY tenant_isolation_tenant_policies ON tenant_policies USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE tenant_calendar_settings ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenant_calendar_settings FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tenant_calendar_settings ON tenant_calendar_settings;
    CREATE POLICY tenant_isolation_tenant_calendar_settings ON tenant_calendar_settings USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE slack_oauth_credentials ENABLE ROW LEVEL SECURITY;
    ALTER TABLE slack_oauth_credentials FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_slack_oauth_credentials ON slack_oauth_credentials;
    CREATE POLICY tenant_isolation_slack_oauth_credentials ON slack_oauth_credentials USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE slack_oauth_states ENABLE ROW LEVEL SECURITY;
    ALTER TABLE slack_oauth_states FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_slack_oauth_states ON slack_oauth_states;
    CREATE POLICY tenant_isolation_slack_oauth_states ON slack_oauth_states USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE tenant_slack_policies ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenant_slack_policies FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tenant_slack_policies ON tenant_slack_policies;
    CREATE POLICY tenant_isolation_tenant_slack_policies ON tenant_slack_policies USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE jira_oauth_states ENABLE ROW LEVEL SECURITY;
    ALTER TABLE jira_oauth_states FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_jira_oauth_states ON jira_oauth_states;
    CREATE POLICY tenant_isolation_jira_oauth_states ON jira_oauth_states USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE jira_oauth_credentials ENABLE ROW LEVEL SECURITY;
    ALTER TABLE jira_oauth_credentials FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_jira_oauth_credentials ON jira_oauth_credentials;
    CREATE POLICY tenant_isolation_jira_oauth_credentials ON jira_oauth_credentials USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE tenant_jira_policies ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenant_jira_policies FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tenant_jira_policies ON tenant_jira_policies;
    CREATE POLICY tenant_isolation_tenant_jira_policies ON tenant_jira_policies USING (tenant_id = current_setting('app.tenant_id', true)::uuid);


    ALTER TABLE notion_oauth_states ENABLE ROW LEVEL SECURITY;
    ALTER TABLE notion_oauth_states FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_notion_oauth_states ON notion_oauth_states;
    CREATE POLICY tenant_isolation_notion_oauth_states ON notion_oauth_states USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE notion_credentials ENABLE ROW LEVEL SECURITY;
    ALTER TABLE notion_credentials FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_notion_credentials ON notion_credentials;
    CREATE POLICY tenant_isolation_notion_credentials ON notion_credentials USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE notion_documents ENABLE ROW LEVEL SECURITY;
    ALTER TABLE notion_documents FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_notion_documents ON notion_documents;
    CREATE POLICY tenant_isolation_notion_documents ON notion_documents USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE microsoft_oauth_states ENABLE ROW LEVEL SECURITY;
    ALTER TABLE microsoft_oauth_states FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_microsoft_oauth_states ON microsoft_oauth_states;
    CREATE POLICY tenant_isolation_microsoft_oauth_states ON microsoft_oauth_states USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE microsoft_oauth_credentials ENABLE ROW LEVEL SECURITY;
    ALTER TABLE microsoft_oauth_credentials FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_microsoft_oauth_credentials ON microsoft_oauth_credentials;
    CREATE POLICY tenant_isolation_microsoft_oauth_credentials ON microsoft_oauth_credentials USING (tenant_id = current_setting('app.tenant_id', true)::uuid);



    ALTER TABLE tenant_automation_policies ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenant_automation_policies FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tenant_automation_policies ON tenant_automation_policies;
    CREATE POLICY tenant_isolation_tenant_automation_policies ON tenant_automation_policies USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE automation_sessions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE automation_sessions FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_automation_sessions ON automation_sessions;
    CREATE POLICY tenant_isolation_automation_sessions ON automation_sessions USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE browser_automation_runs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE browser_automation_runs FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_browser_automation_runs ON browser_automation_runs;
    CREATE POLICY tenant_isolation_browser_automation_runs ON browser_automation_runs USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE browser_automation_artifacts ENABLE ROW LEVEL SECURITY;
    ALTER TABLE browser_automation_artifacts FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_browser_automation_artifacts ON browser_automation_artifacts;
    CREATE POLICY tenant_isolation_browser_automation_artifacts ON browser_automation_artifacts USING (tenant_id = current_setting('app.tenant_id', true)::uuid);


    ALTER TABLE tenant_plans ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenant_plans FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tenant_plans ON tenant_plans;
    CREATE POLICY tenant_isolation_tenant_plans ON tenant_plans USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE tenant_usage_daily ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenant_usage_daily FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tenant_usage_daily ON tenant_usage_daily;
    CREATE POLICY tenant_isolation_tenant_usage_daily ON tenant_usage_daily USING (tenant_id = current_setting('app.tenant_id', true)::uuid);



    ALTER TABLE tenant_invites ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenant_invites FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tenant_invites ON tenant_invites;
    CREATE POLICY tenant_isolation_tenant_invites ON tenant_invites USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE tenant_notion_policies ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tenant_notion_policies FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_tenant_notion_policies ON tenant_notion_policies;
    CREATE POLICY tenant_isolation_tenant_notion_policies ON tenant_notion_policies USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE integration_health ENABLE ROW LEVEL SECURITY;
    ALTER TABLE integration_health FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_integration_health ON integration_health;
    CREATE POLICY tenant_isolation_integration_health ON integration_health USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE workflow_runs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE workflow_runs FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_workflow_runs ON workflow_runs;
    CREATE POLICY tenant_isolation_workflow_runs ON workflow_runs USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE automation_rules ENABLE ROW LEVEL SECURITY;
    ALTER TABLE automation_rules FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_automation_rules ON automation_rules;
    CREATE POLICY tenant_isolation_automation_rules ON automation_rules USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    ALTER TABLE automation_executions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE automation_executions FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation_automation_executions ON automation_executions;
    CREATE POLICY tenant_isolation_automation_executions ON automation_executions USING (tenant_id = current_setting('app.tenant_id', true)::uuid);

    """

    seed = """
    INSERT INTO users (id, email, password)
    VALUES ('00000000-0000-0000-0000-000000000001', 'owner@example.com', 'dev-password')
    ON CONFLICT (email) DO NOTHING;

    INSERT INTO users (id, email, password)
    VALUES ('00000000-0000-0000-0000-000000000002', 'member@example.com', 'dev-password')
    ON CONFLICT (email) DO NOTHING;

    INSERT INTO tenants (id, name)
    VALUES
      ('10000000-0000-0000-0000-000000000001', 'Alpha Corp'),
      ('20000000-0000-0000-0000-000000000001', 'Beta Corp')
    ON CONFLICT (id) DO NOTHING;

    INSERT INTO memberships (user_id, tenant_id, role)
    VALUES
      ('00000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'owner'),
      ('00000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001', 'owner'),
      ('00000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'member')
    ON CONFLICT (user_id, tenant_id) DO NOTHING;

    INSERT INTO tenant_policies (tenant_id, allowed_email_domains)
    VALUES ('10000000-0000-0000-0000-000000000001', ARRAY[]::text[]), ('20000000-0000-0000-0000-000000000001', ARRAY[]::text[])
    ON CONFLICT (tenant_id) DO NOTHING;

    INSERT INTO tenant_calendar_settings (tenant_id)
    VALUES ('10000000-0000-0000-0000-000000000001'), ('20000000-0000-0000-0000-000000000001')
    ON CONFLICT (tenant_id) DO NOTHING;

    INSERT INTO tenant_slack_policies (tenant_id)
    VALUES ('10000000-0000-0000-0000-000000000001'), ('20000000-0000-0000-0000-000000000001')
    ON CONFLICT (tenant_id) DO NOTHING;

    INSERT INTO tenant_jira_policies (tenant_id)
    VALUES ('10000000-0000-0000-0000-000000000001'), ('20000000-0000-0000-0000-000000000001')
    ON CONFLICT (tenant_id) DO NOTHING;





    INSERT INTO tenant_notion_policies (tenant_id)
    VALUES ('10000000-0000-0000-0000-000000000001'), ('20000000-0000-0000-0000-000000000001')
    ON CONFLICT (tenant_id) DO NOTHING;

    INSERT INTO integration_health (tenant_id, integration, connected, updated_at)
    SELECT t.id, i.integration, FALSE, now()
    FROM tenants t
    CROSS JOIN (VALUES ('google'),('slack'),('jira'),('notion'),('microsoft'),('web_automation')) AS i(integration)
    ON CONFLICT (tenant_id, integration) DO NOTHING;

    INSERT INTO tenant_plans (tenant_id, plan_name, limits_json)
    VALUES
      ('10000000-0000-0000-0000-000000000001', 'pilot', '{"api_requests_per_minute":120,"tool_calls_per_day":200,"llm_tokens_per_day":200000,"workflow_runs_per_day":100,"automation_runs_per_day":100,"web_automation_runs_per_day":30,"web_automation_runtime_seconds_per_day":1200}'::jsonb),
      ('20000000-0000-0000-0000-000000000001', 'pilot', '{"api_requests_per_minute":120,"tool_calls_per_day":200,"llm_tokens_per_day":200000,"workflow_runs_per_day":100,"automation_runs_per_day":100,"web_automation_runs_per_day":30,"web_automation_runtime_seconds_per_day":1200}'::jsonb)
    ON CONFLICT (tenant_id) DO NOTHING;

    INSERT INTO tenant_automation_policies (tenant_id)
    VALUES ('10000000-0000-0000-0000-000000000001'), ('20000000-0000-0000-0000-000000000001')
    ON CONFLICT (tenant_id) DO NOTHING;


    INSERT INTO workflow_templates (id, name, description, input_schema_json, enabled, version)
    VALUES
      ('email_triage_v1', 'Email Triage + Draft Replies', 'Summarize recent emails and draft replies; optional gated sending.', '{"type":"object","properties":{"timeframe_days":{"type":"integer","default":7,"minimum":1,"maximum":30},"max_threads":{"type":"integer","default":10,"minimum":1,"maximum":25},"mode":{"type":"string","enum":["draft_only","draft_and_send"],"default":"draft_only"}},"required":[],"additionalProperties":false}'::jsonb, TRUE, 1),
      ('schedule_meeting_v1', 'Schedule a Meeting', 'Find slots and optionally auto-schedule a meeting with policy-aware risk gates.', '{"type":"object","properties":{"title":{"type":"string","minLength":1},"duration_minutes":{"type":"integer","minimum":15,"maximum":240},"attendees_emails":{"type":"array","items":{"type":"string"},"minItems":1},"time_window_start":{"type":"string"},"time_window_end":{"type":"string"},"auto_schedule":{"type":"boolean","default":false},"notes":{"type":"string"}},"required":["title","duration_minutes","attendees_emails","time_window_start","time_window_end"],"additionalProperties":false}'::jsonb, TRUE, 1),
      ('jira_action_items_v1', 'Turn Action Items into Jira Issues', 'Use unified search context to extract action items and create Jira tasks safely.', '{"type":"object","properties":{"query_or_text":{"type":"string","minLength":1},"project_key":{"type":"string"},"max_issues":{"type":"integer","default":5,"minimum":1,"maximum":15}},"required":["query_or_text"],"additionalProperties":false}'::jsonb, TRUE, 1)
    ON CONFLICT (id) DO NOTHING;
    """

    with engine.begin() as conn:
        conn.execute(text(ddl))
        conn.execute(text(rls))
        conn.execute(text(seed))


@contextmanager
def tenant_session(tenant_id: str, user_id: str | None = None):
    db = SessionLocal()
    try:
        db.execute(text("SET app.tenant_id = :tenant_id"), {"tenant_id": tenant_id})
        if user_id:
            db.execute(text("SET app.user_id = :user_id"), {"user_id": user_id})
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
