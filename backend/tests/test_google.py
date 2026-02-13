from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from app.db import engine
from app.integrations.google import oauth
from .conftest import login


def test_oauth_state_validation(client, monkeypatch):
    headers = login(client)
    res = client.post('/integrations/google/connect', headers=headers)
    assert res.status_code == 200

    bad = client.get('/integrations/google/callback?state=bad&code=abc')
    assert bad.status_code == 400

    monkeypatch.setattr(oauth, 'exchange_code', lambda code, verifier: {
        'access_token': 'tok',
        'refresh_token': 'ref',
        'expires_in': 3600,
        'scope': 'openid email https://www.googleapis.com/auth/gmail.send'
    })

    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text("SELECT state FROM google_oauth_states ORDER BY created_at DESC LIMIT 1")).scalar()

    ok = client.get(f'/integrations/google/callback?state={state}&code=abc')
    assert ok.status_code == 200

    reused = client.get(f'/integrations/google/callback?state={state}&code=abc')
    assert reused.status_code == 400


def test_rls_isolation_google_credentials_and_policy(client, monkeypatch):
    headers = login(client)
    client.post('/integrations/google/connect', headers=headers)

    monkeypatch.setattr(oauth, 'exchange_code', lambda code, verifier: {
        'access_token': 'tokA', 'refresh_token': 'refA', 'expires_in': 3600, 'scope': 'openid email'
    })
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text("SELECT state FROM google_oauth_states ORDER BY created_at DESC LIMIT 1")).scalar()
    client.get(f'/integrations/google/callback?state={state}&code=abc')

    client.put('/tenant/policy', headers=headers, json={'allowed_email_domains': ['alpha.com']})

    switch = client.post('/tenants/switch', headers=headers, json={'tenant_id': '20000000-0000-0000-0000-000000000001'})
    beta_headers = {'Authorization': f"Bearer {switch.json()['token']}"}

    assert client.get('/integrations/google/status', headers=beta_headers).json()['connected'] is False
    assert client.get('/tenant/policy', headers=beta_headers).json()['allowed_email_domains'] == []


def test_gmail_send_high_risk_gate_and_approval(client, monkeypatch):
    headers = login(client)

    monkeypatch.setattr(oauth, 'exchange_code', lambda code, verifier: {
        'access_token': 'tok', 'refresh_token': 'ref', 'expires_in': 3600, 'scope': 'openid email'
    })
    client.post('/integrations/google/connect', headers=headers)
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text("SELECT state FROM google_oauth_states ORDER BY created_at DESC LIMIT 1")).scalar()
    client.get(f'/integrations/google/callback?state={state}&code=abc')

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['send'], 'tool_calls': [{'tool': 'gmail_send', 'args': {'to': ['ext@outside.com'], 'subject': 'x', 'body_text': 'y'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    from app.integrations.google.gmail_client import GmailClient
    monkeypatch.setattr(GmailClient, 'send', lambda self, **kwargs: {'message_id': 'm-1'})

    task = client.post('/tasks', headers=headers, json={'title': 'email', 'description': 'send gmail', 'risk_level': 'LOW'}).json()
    run = client.post(f"/tasks/{task['id']}/run", headers=headers, json={'approve': False}).json()
    detail = client.get(f"/runs/{run['run_id']}", headers=headers).json()
    assert detail['status'] == 'PENDING_APPROVAL'

    approved = client.post(f"/tasks/{task['id']}/run", headers=headers, json={'approve': True}).json()
    detail2 = client.get(f"/runs/{approved['run_id']}", headers=headers).json()
    assert any(i['tool_name'] == 'gmail_send' for i in detail2['tool_invocations'])


def test_token_refresh_behavior(client, monkeypatch):
    headers = login(client)
    monkeypatch.setattr(oauth, 'exchange_code', lambda code, verifier: {
        'access_token': 'tok', 'refresh_token': 'ref', 'expires_in': 1, 'scope': 'openid email'
    })
    client.post('/integrations/google/connect', headers=headers)
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text("SELECT state FROM google_oauth_states ORDER BY created_at DESC LIMIT 1")).scalar()
    client.get(f'/integrations/google/callback?state={state}&code=abc')

    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        conn.execute(text("UPDATE google_oauth_credentials SET expiry=now() - interval '10 minutes'"))

    refresh_calls = {'n': 0}
    from urllib import request as urllib_request
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            refresh_calls['n'] += 1
            return b'{"access_token":"newtok","expires_in":3600}'

    monkeypatch.setattr(urllib_request, 'urlopen', lambda *a, **k: FakeResp())

    from app.integrations.google.gmail_client import GmailClient
    monkeypatch.setattr(GmailClient, '_request', lambda self, method, path, body=None: {'messages': []})

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['search'], 'tool_calls': [{'tool': 'gmail_search', 'args': {'query': 'x'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    task = client.post('/tasks', headers=headers, json={'title': 'q', 'description': 'gmail', 'risk_level': 'LOW'}).json()
    client.post(f"/tasks/{task['id']}/run", headers=headers, json={'approve': False})
    assert refresh_calls['n'] >= 1


def test_gmail_send_idempotency_same_run(client, monkeypatch):
    headers = login(client)

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {
        'plan': ['send1', 'send2'],
        'tool_calls': [
            {'tool': 'gmail_send', 'args': {'draft_id': 'd-1', 'to': ['a@outside.com'], 'subject': 's'}},
            {'tool': 'gmail_send', 'args': {'draft_id': 'd-1', 'to': ['a@outside.com'], 'subject': 's'}},
        ]
    })
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    send_calls = {'n': 0}
    from app.integrations.google.gmail_client import GmailClient
    def fake_send(self, **kwargs):
        send_calls['n'] += 1
        return {'message_id': 'm-1'}
    monkeypatch.setattr(GmailClient, '__init__', lambda self, db: None)
    monkeypatch.setattr(GmailClient, 'send', fake_send)

    # allow domain so MEDIUM risk
    client.put('/tenant/policy', headers=headers, json={'allowed_email_domains': ['outside.com']})

    task = client.post('/tasks', headers=headers, json={'title': 'q', 'description': 'email send', 'risk_level': 'LOW'}).json()
    client.post(f"/tasks/{task['id']}/run", headers=headers, json={'approve': False})
    assert send_calls['n'] == 1
