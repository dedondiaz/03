from datetime import datetime, timedelta, timezone
from urllib import request as ureq

from sqlalchemy import text

from app.db import engine
from app.integrations.microsoft import oauth as ms_oauth


def _connect_ms(client, headers, monkeypatch):
    monkeypatch.setattr(ms_oauth, '_exchange_code', lambda code, verifier: {
        'access_token': 'at-1',
        'refresh_token': 'rt-1',
        'expires_in': 3600,
        'scope': 'offline_access User.Read Mail.Read Mail.Send Calendars.ReadWrite',
        'id_token_claims': {'oid': 'oid1', 'preferred_username': 'owner@example.com', 'tid': 'tid1'}
    })
    client.post('/integrations/microsoft/connect', headers=headers)
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text('SELECT state FROM microsoft_oauth_states ORDER BY created_at DESC LIMIT 1')).scalar()
    return client.get(f'/integrations/microsoft/callback?state={state}&code=ok')


def test_microsoft_rls_isolation(client, monkeypatch, login):
    headers = login(client)
    assert _connect_ms(client, headers, monkeypatch).status_code == 200
    switch = client.post('/tenants/switch', headers=headers, json={'tenant_id': '20000000-0000-0000-0000-000000000001'})
    beta = {'Authorization': f"Bearer {switch.json()['token']}"}
    assert client.get('/integrations/microsoft/status', headers=beta).json()['connected'] is False


def test_microsoft_state_validation(client, monkeypatch, login):
    headers = login(client)
    client.post('/integrations/microsoft/connect', headers=headers)
    assert client.get('/integrations/microsoft/callback?state=bad&code=ok').status_code == 400

    monkeypatch.setattr(ms_oauth, '_exchange_code', lambda code, verifier: {
        'access_token': 'at', 'scope': 'Mail.Read', 'expires_in': 3600
    })
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text('SELECT state FROM microsoft_oauth_states ORDER BY created_at DESC LIMIT 1')).scalar()
    assert client.get(f'/integrations/microsoft/callback?state={state}&code=ok').status_code == 200
    assert client.get(f'/integrations/microsoft/callback?state={state}&code=ok').status_code == 400


def test_microsoft_refresh_rotating_token_persists(client, monkeypatch, login):
    headers = login(client)
    _connect_ms(client, headers, monkeypatch)
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        conn.execute(text("UPDATE microsoft_oauth_credentials SET token_expires_at=now() - interval '10 minutes'"))

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"access_token":"at-new","refresh_token":"rt-new","expires_in":3600}'

    monkeypatch.setattr(ureq, 'urlopen', lambda *a, **k: Resp())
    from app.integrations.microsoft.graph_client import MicrosoftGraphClient
    monkeypatch.setattr(MicrosoftGraphClient, '_api', lambda self, method, path, body=None, retries=3: {'value': []})

    from app.tools.impl import ms_mail_search
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        ms_mail_search({'query': 'x'}, {'db': conn, 'tenant_id': '10000000-0000-0000-0000-000000000001', 'user_id': '00000000-0000-0000-0000-000000000001'})
        val = conn.execute(text("SELECT refresh_token_enc FROM microsoft_oauth_credentials WHERE is_primary=TRUE")).scalar()
        assert val is not None


def test_ms_mail_send_risk_gating(client, monkeypatch, login):
    headers = login(client)
    _connect_ms(client, headers, monkeypatch)

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['send'], 'tool_calls': [{'tool': 'ms_mail_send', 'args': {'to': ['outside@ext.com'], 'subject': 'x', 'body_text': 'y'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    t = client.post('/tasks', headers=headers, json={'title': 'ms', 'description': 'send mail', 'risk_level': 'LOW'}).json()
    r = client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve': False}).json()
    assert client.get(f"/runs/{r['run_id']}", headers=headers).json()['status'] == 'PENDING_APPROVAL'


def test_ms_calendar_cancel_always_high(client, monkeypatch, login):
    headers = login(client)
    _connect_ms(client, headers, monkeypatch)

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['cancel'], 'tool_calls': [{'tool': 'ms_calendar_cancel_event', 'args': {'event_id': 'ev1'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    t = client.post('/tasks', headers=headers, json={'title': 'ms', 'description': 'cancel event', 'risk_level': 'LOW'}).json()
    r = client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve': False}).json()
    assert client.get(f"/runs/{r['run_id']}", headers=headers).json()['status'] == 'PENDING_APPROVAL'


def test_ms_calendar_create_idempotency(client, monkeypatch, login):
    headers = login(client)
    _connect_ms(client, headers, monkeypatch)
    client.put('/tenant/policy', headers=headers, json={'allowed_email_domains': ['example.com']})

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['a', 'b'], 'tool_calls': [
        {'tool': 'ms_calendar_create_event', 'args': {'title': 'sync', 'start': '2026-01-01T10:00:00Z', 'end': '2026-01-01T10:30:00Z', 'attendees': ['owner@example.com']}},
        {'tool': 'ms_calendar_create_event', 'args': {'title': 'sync', 'start': '2026-01-01T10:00:00Z', 'end': '2026-01-01T10:30:00Z', 'attendees': ['owner@example.com']}},
    ]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    from app.integrations.microsoft.graph_client import MicrosoftGraphClient
    calls = {'n': 0}
    monkeypatch.setattr(MicrosoftGraphClient, '__init__', lambda self, db: None)
    monkeypatch.setattr(MicrosoftGraphClient, 'calendar_create_event', lambda self, payload: calls.__setitem__('n', calls['n'] + 1) or {'id': 'ev1'})

    t = client.post('/tasks', headers=headers, json={'title': 'ms', 'description': 'create calendar', 'risk_level': 'LOW'}).json()
    client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve': False})
    assert calls['n'] == 1
