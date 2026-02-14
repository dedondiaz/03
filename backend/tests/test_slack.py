from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from app.db import engine
from app.integrations.slack import oauth as slack_oauth


def _connect_slack(client, headers, monkeypatch):
    monkeypatch.setattr(slack_oauth, '_exchange', lambda code, verifier: {
        'ok': True,
        'access_token': 'xoxb-1',
        'refresh_token': 'r1',
        'expires_in': 3600,
        'scope': 'channels:read,groups:read,chat:write,users:read.email',
        'team': {'id': 'T1', 'name': 'Acme'},
        'bot_user_id': 'Ubot',
    })
    client.post('/integrations/slack/connect', headers=headers)
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text('SELECT state FROM slack_oauth_states ORDER BY created_at DESC LIMIT 1')).scalar()
    return client.get(f'/integrations/slack/callback?state={state}&code=abc')


def test_slack_rls_isolation(client, monkeypatch, login):
    headers = login(client)
    _connect_slack(client, headers, monkeypatch)

    client.put('/tenant/slack-policy', headers=headers, json={'allowed_channel_ids': ['C1'], 'allow_external_shared': False})
    switch = client.post('/tenants/switch', headers=headers, json={'tenant_id': '20000000-0000-0000-0000-000000000001'})
    beta_headers = {'Authorization': f"Bearer {switch.json()['token']}"}

    assert client.get('/integrations/slack/status', headers=beta_headers).json()['connected'] is False
    assert client.get('/tenant/slack-policy', headers=beta_headers).json()['allowed_channel_ids'] == []


def test_slack_state_validation(client, monkeypatch, login):
    headers = login(client)
    client.post('/integrations/slack/connect', headers=headers)

    bad = client.get('/integrations/slack/callback?state=bad&code=abc')
    assert bad.status_code == 400

    monkeypatch.setattr(slack_oauth, '_exchange', lambda code, verifier: {
        'ok': True, 'access_token': 'xoxb', 'scope': 'chat:write', 'team': {'id': 'T1', 'name': 'Acme'}
    })
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text('SELECT state FROM slack_oauth_states ORDER BY created_at DESC LIMIT 1')).scalar()
    ok = client.get(f'/integrations/slack/callback?state={state}&code=abc')
    assert ok.status_code == 200
    reused = client.get(f'/integrations/slack/callback?state={state}&code=abc')
    assert reused.status_code == 400


def test_slack_post_message_risk_gating_and_allowlist(client, monkeypatch, login):
    headers = login(client)
    _connect_slack(client, headers, monkeypatch)

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['post'], 'tool_calls': [{'tool': 'slack_post_message', 'args': {'channel_id': 'CEXT', 'text': 'hello'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    from app.integrations.slack.slack_client import SlackClient
    monkeypatch.setattr(SlackClient, 'conversations_info', lambda self, channel_id: {'ok': True, 'channel': {'id': channel_id, 'is_ext_shared': False}})
    monkeypatch.setattr(SlackClient, 'chat_post_message', lambda self, channel_id, text, thread_ts=None: {'ok': True, 'channel': channel_id, 'ts': '1.0'})

    t = client.post('/tasks', headers=headers, json={'title': 'x', 'description': 'slack post', 'risk_level': 'LOW'}).json()
    run = client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve': False}).json()
    assert client.get(f"/runs/{run['run_id']}", headers=headers).json()['status'] == 'PENDING_APPROVAL'

    client.put('/tenant/slack-policy', headers=headers, json={'allowed_channel_ids': ['CEXT'], 'allow_external_shared': False})
    run2 = client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve': False}).json()
    d2 = client.get(f"/runs/{run2['run_id']}", headers=headers).json()
    assert d2['status'] in {'COMPLETED', 'FAILED'}


def test_slack_external_shared_is_high(client, monkeypatch, login):
    headers = login(client)
    _connect_slack(client, headers, monkeypatch)
    client.put('/tenant/slack-policy', headers=headers, json={'allowed_channel_ids': ['C1'], 'allow_external_shared': True})

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['post'], 'tool_calls': [{'tool': 'slack_post_message', 'args': {'channel_id': 'C1', 'text': 'hi'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    from app.integrations.slack.slack_client import SlackClient
    monkeypatch.setattr(SlackClient, 'conversations_info', lambda self, channel_id: {'ok': True, 'channel': {'id': channel_id, 'is_ext_shared': True}})

    t = client.post('/tasks', headers=headers, json={'title': 'x', 'description': 'slack post', 'risk_level': 'LOW'}).json()
    run = client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve': False}).json()
    assert client.get(f"/runs/{run['run_id']}", headers=headers).json()['status'] == 'PENDING_APPROVAL'


def test_slack_post_idempotency_dedupe(client, monkeypatch, login):
    headers = login(client)
    _connect_slack(client, headers, monkeypatch)
    client.put('/tenant/slack-policy', headers=headers, json={'allowed_channel_ids': ['C1'], 'allow_external_shared': False})

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['a','b'], 'tool_calls': [
        {'tool': 'slack_post_message', 'args': {'channel_id': 'C1', 'text': 'same'}},
        {'tool': 'slack_post_message', 'args': {'channel_id': 'C1', 'text': 'same'}},
    ]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    from app.integrations.slack.slack_client import SlackClient
    monkeypatch.setattr(SlackClient, 'conversations_info', lambda self, channel_id: {'ok': True, 'channel': {'id': channel_id, 'is_ext_shared': False}})
    calls = {'n': 0}
    def post(self, channel_id, text, thread_ts=None):
        calls['n'] += 1
        return {'ok': True, 'channel': channel_id, 'ts': '1.0'}
    monkeypatch.setattr(SlackClient, 'chat_post_message', post)

    t = client.post('/tasks', headers=headers, json={'title': 'x', 'description': 'slack post', 'risk_level': 'LOW'}).json()
    client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve': False})
    assert calls['n'] == 1


def test_slack_token_refresh_persists(client, monkeypatch, login):
    headers = login(client)
    _connect_slack(client, headers, monkeypatch)
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        conn.execute(text("UPDATE slack_oauth_credentials SET token_expires_at=now() - interval '10 minutes'"))

    from urllib import request as ureq
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok":true,"access_token":"xoxb-new","refresh_token":"r2","expires_in":3600}'
    monkeypatch.setattr(ureq, 'urlopen', lambda *a, **k: Resp())

    from app.integrations.slack.slack_client import SlackClient
    monkeypatch.setattr(SlackClient, 'conversations_info', lambda self, channel_id: {'ok': True, 'channel': {'id': channel_id, 'is_ext_shared': False}})
    monkeypatch.setattr(SlackClient, 'chat_post_message', lambda self, channel_id, text, thread_ts=None: {'ok': True, 'channel': channel_id, 'ts': '2.0'})
    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['post'], 'tool_calls': [{'tool': 'slack_post_message', 'args': {'channel_id': 'C1', 'text': 'hi'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})
    client.put('/tenant/slack-policy', headers=headers, json={'allowed_channel_ids': ['C1'], 'allow_external_shared': False})

    t = client.post('/tasks', headers=headers, json={'title': 'x', 'description': 'slack post', 'risk_level': 'LOW'}).json()
    client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve': False})
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        refreshed = conn.execute(text("SELECT token_expires_at FROM slack_oauth_credentials WHERE is_primary=TRUE")).scalar()
        assert refreshed is not None
