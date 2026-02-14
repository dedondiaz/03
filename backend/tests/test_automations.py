from datetime import datetime, timezone

from sqlalchemy import text

from app.automations.service import run_scheduler_tick, compute_next_run_at
from app.db import engine


def test_automation_rule_tenant_isolation(client, login):
    headers = login(client)
    created = client.post('/automations/rules', headers=headers, json={
        'name': 'A', 'template_id': 'email_triage_v1', 'input': {}, 'trigger_type': 'schedule', 'schedule_cron': '*/5 * * * *'
    })
    assert created.status_code == 200
    rid = created.json()['id']

    switch = client.post('/tenants/switch', headers=headers, json={'tenant_id': '20000000-0000-0000-0000-000000000001'})
    beta = {'Authorization': f"Bearer {switch.json()['token']}"}
    rules = client.get('/automations/rules', headers=beta).json()
    assert all(r['id'] != rid for r in rules)


def test_cron_validation_and_next_run():
    nxt = compute_next_run_at('0 9 * * 1-5', 'Asia/Kolkata', datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert nxt is not None


def test_invalid_cron_rejected(client, login):
    headers = login(client)
    bad = client.post('/automations/rules', headers=headers, json={
        'name': 'bad', 'template_id': 'email_triage_v1', 'input': {}, 'trigger_type': 'schedule', 'schedule_cron': 'bad cron'
    })
    assert bad.status_code == 400


def test_quiet_hours_skip(client, login):
    headers = login(client)
    c = client.post('/automations/rules', headers=headers, json={
        'name': 'qh', 'template_id': 'email_triage_v1', 'input': {}, 'trigger_type': 'schedule',
        'schedule_cron': '*/5 * * * *', 'timezone': 'Asia/Kolkata', 'quiet_hours_start': '00:00', 'quiet_hours_end': '23:59'
    })
    assert c.status_code == 200
    rid = c.json()['id']
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        conn.execute(text("UPDATE automation_rules SET next_run_at=now() - interval '1 minute' WHERE id=:id"), {'id': rid})
        ev = run_scheduler_tick(conn, datetime.now(timezone.utc))
        assert any(x['status'] == 'skipped_quiet_hours' for x in ev)


def test_quota_skip(client, login):
    headers = login(client)
    c = client.post('/automations/rules', headers=headers, json={
        'name': 'quota', 'template_id': 'email_triage_v1', 'input': {}, 'trigger_type': 'schedule',
        'schedule_cron': '*/5 * * * *', 'max_runs_per_day': 1
    })
    rid = c.json()['id']
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        conn.execute(text("UPDATE automation_rules SET next_run_at=now() - interval '1 minute' WHERE id=:id"), {'id': rid})
        conn.execute(text("INSERT INTO automation_executions (tenant_id, rule_id, status, created_at) VALUES ('10000000-0000-0000-0000-000000000001', :rid, 'enqueued', now())"), {'rid': rid})
        ev = run_scheduler_tick(conn, datetime.now(timezone.utc))
        assert any(x['status'] == 'skipped_quota' for x in ev)


def test_concurrency_skip(client, login):
    headers = login(client)
    c = client.post('/automations/rules', headers=headers, json={
        'name': 'conc', 'template_id': 'email_triage_v1', 'input': {}, 'trigger_type': 'schedule',
        'schedule_cron': '*/5 * * * *', 'max_concurrent_runs': 1
    })
    rid = c.json()['id']
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        conn.execute(text("UPDATE automation_rules SET next_run_at=now() - interval '1 minute' WHERE id=:id"), {'id': rid})
        conn.execute(text("INSERT INTO workflow_runs (tenant_id, template_id, input_json, status, created_by, triggered_by_rule_id) VALUES ('10000000-0000-0000-0000-000000000001','email_triage_v1','{}','running','00000000-0000-0000-0000-000000000001', :rid)"), {'rid': rid})
        ev = run_scheduler_tick(conn, datetime.now(timezone.utc))
        assert any(x['status'] == 'skipped_quota' for x in ev)


def test_scheduler_enqueues_workflow_run(client, monkeypatch, login):
    headers = login(client)
    c = client.post('/automations/rules', headers=headers, json={
        'name': 'ok', 'template_id': 'email_triage_v1', 'input': {'mode': 'draft_only'}, 'trigger_type': 'schedule',
        'schedule_cron': '*/5 * * * *'
    })
    rid = c.json()['id']

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['x'], 'tool_calls': [{'tool': 'echo_tool', 'args': {'text': 'ok'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        conn.execute(text("UPDATE automation_rules SET next_run_at=now() - interval '1 minute' WHERE id=:id"), {'id': rid})
        ev = run_scheduler_tick(conn, datetime.now(timezone.utc))
        assert any(x['status'] == 'enqueued' for x in ev)
        wf = conn.execute(text("SELECT triggered_by_rule_id FROM workflow_runs WHERE triggered_by_rule_id=:rid LIMIT 1"), {'rid': rid}).fetchone()
        assert wf is not None
