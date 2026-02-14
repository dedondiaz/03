import os
import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/saas_ai")
os.environ.setdefault("LLM_MODE", "fake")
os.environ.setdefault("APP_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-secret")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "http://localhost:8000/integrations/google/callback")
os.environ.setdefault("SLACK_CLIENT_ID", "slack-client")
os.environ.setdefault("SLACK_CLIENT_SECRET", "slack-secret")
os.environ.setdefault("SLACK_REDIRECT_URI", "http://localhost:8000/integrations/slack/callback")
os.environ.setdefault("SLACK_SCOPES", "channels:read,groups:read,chat:write,users:read.email")
os.environ.setdefault("JIRA_CLIENT_ID", "jira-client")
os.environ.setdefault("JIRA_CLIENT_SECRET", "jira-secret")
os.environ.setdefault("JIRA_REDIRECT_URI", "http://localhost:8000/integrations/jira/callback")
os.environ.setdefault("JIRA_SCOPES", "read:jira-work,write:jira-work,read:jira-user,offline_access")
os.environ.setdefault("NOTION_CLIENT_ID", "notion-client")
os.environ.setdefault("NOTION_CLIENT_SECRET", "notion-secret")
os.environ.setdefault("NOTION_REDIRECT_URI", "http://localhost:8000/integrations/notion/callback")
os.environ.setdefault("MICROSOFT_CLIENT_ID", "ms-client")
os.environ.setdefault("MICROSOFT_CLIENT_SECRET", "ms-secret")
os.environ.setdefault("MICROSOFT_REDIRECT_URI", "http://localhost:8000/integrations/microsoft/callback")
os.environ.setdefault("MICROSOFT_SCOPES", "offline_access User.Read Mail.Read Mail.Send Calendars.ReadWrite")

from app.main import app
from app.db import engine, init_db


@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE tenant_invites, tenant_notion_policies, integration_health, tenant_usage_daily, tenant_plans, browser_automation_artifacts, browser_automation_runs, automation_sessions, tenant_automation_policies, microsoft_oauth_states, microsoft_oauth_credentials, automation_executions, automation_rules, workflow_runs, notion_documents, notion_credentials, notion_oauth_states, jira_oauth_states, jira_oauth_credentials, tenant_jira_policies, slack_oauth_states, slack_oauth_credentials, tenant_slack_policies, google_oauth_states, google_oauth_credentials, tenant_calendar_settings, tenant_policies, tool_invocations, notes, audit_logs, approvals, task_runs, tasks, sessions RESTART IDENTITY CASCADE"))
        conn.execute(text("""
            INSERT INTO integration_health (tenant_id, integration, connected, updated_at)
            SELECT t.id, i.integration, FALSE, now()
            FROM tenants t
            CROSS JOIN (VALUES ('google'),('slack'),('jira'),('notion'),('microsoft'),('web_automation')) AS i(integration)
            ON CONFLICT (tenant_id, integration) DO NOTHING;

            INSERT INTO tenant_plans (tenant_id, plan_name, limits_json)
            VALUES
              ('10000000-0000-0000-0000-000000000001', 'pilot', jsonb_build_object('api_requests_per_minute',120,'tool_calls_per_day',200,'llm_tokens_per_day',200000,'workflow_runs_per_day',100,'automation_runs_per_day',100,'web_automation_runs_per_day',30,'web_automation_runtime_seconds_per_day',1200)),
              ('20000000-0000-0000-0000-000000000001', 'pilot', jsonb_build_object('api_requests_per_minute',120,'tool_calls_per_day',200,'llm_tokens_per_day',200000,'workflow_runs_per_day',100,'automation_runs_per_day',100,'web_automation_runs_per_day',30,'web_automation_runtime_seconds_per_day',1200))
            ON CONFLICT (tenant_id) DO NOTHING;
            INSERT INTO tenant_policies (tenant_id, allowed_email_domains)
            VALUES ('10000000-0000-0000-0000-000000000001', ARRAY[]::text[]), ('20000000-0000-0000-0000-000000000001', ARRAY[]::text[])
            ON CONFLICT (tenant_id) DO NOTHING;
            INSERT INTO tenant_slack_policies (tenant_id)
            VALUES ('10000000-0000-0000-0000-000000000001'), ('20000000-0000-0000-0000-000000000001')
            ON CONFLICT (tenant_id) DO NOTHING;
            INSERT INTO tenant_jira_policies (tenant_id)
            VALUES ('10000000-0000-0000-0000-000000000001'), ('20000000-0000-0000-0000-000000000001')
            ON CONFLICT (tenant_id) DO NOTHING;
            INSERT INTO tenant_calendar_settings (tenant_id)
            VALUES ('10000000-0000-0000-0000-000000000001'), ('20000000-0000-0000-0000-000000000001')
            ON CONFLICT (tenant_id) DO NOTHING;
            INSERT INTO tenant_automation_policies (tenant_id)
            VALUES ('10000000-0000-0000-0000-000000000001'), ('20000000-0000-0000-0000-000000000001')
            ON CONFLICT (tenant_id) DO NOTHING;
            INSERT INTO tenant_notion_policies (tenant_id)
            VALUES ('10000000-0000-0000-0000-000000000001'), ('20000000-0000-0000-0000-000000000001')
            ON CONFLICT (tenant_id) DO NOTHING;
        """))
    yield


@pytest.fixture()
def client(monkeypatch):
    from app import main

    def immediate_enqueue(func, *args, **kwargs):
        func(*args)
        class _J:
            id = "local"
        return _J()

    monkeypatch.setattr(main.queue, "enqueue", immediate_enqueue)
    monkeypatch.setattr(main, "enqueue_run_job", lambda run_id, tenant_id, user_id, job_id: immediate_enqueue(main.execute_run_job, run_id, tenant_id, user_id))
    return TestClient(app)


@pytest.fixture
def login(client):
    def _login(client_override=None, email="owner@example.com"):
        active_client = client_override or client
        res = active_client.post("/auth/login", json={"email": email, "password": "dev-password"})
        assert res.status_code == 200
        token = res.json()["token"]
        return {"Authorization": f"Bearer {token}"}

    return _login
