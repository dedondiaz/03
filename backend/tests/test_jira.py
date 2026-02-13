from sqlalchemy import text
from app.db import engine
from app.integrations.jira import oauth as jira_oauth
from .conftest import login


def _connect_jira(client, headers, monkeypatch):
    monkeypatch.setattr(jira_oauth, '_token_exchange', lambda code: {
        'access_token': 'at-1', 'refresh_token': 'rt-1', 'expires_in': 3600, 'scope': 'read:jira-work write:jira-work'
    })
    monkeypatch.setattr(jira_oauth, '_accessible_resources', lambda token: [
        {'id': 'cloud-1', 'name': 'Acme Jira', 'url': 'https://acme.atlassian.net', 'scopes': ['read:jira-work','write:jira-work']}
    ])
    client.post('/integrations/jira/connect', headers=headers)
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text('SELECT state FROM jira_oauth_states ORDER BY created_at DESC LIMIT 1')).scalar()
    return client.get(f'/integrations/jira/callback?state={state}&code=abc')


def test_jira_rls_isolation(client, monkeypatch):
    headers = login(client)
    _connect_jira(client, headers, monkeypatch)
    client.put('/tenant/jira-policy', headers=headers, json={'allowed_project_keys': ['ENG'], 'allow_write': True})

    switch = client.post('/tenants/switch', headers=headers, json={'tenant_id': '20000000-0000-0000-0000-000000000001'})
    beta = {'Authorization': f"Bearer {switch.json()['token']}"}
    assert client.get('/integrations/jira/status', headers=beta).json()['connected'] is False
    assert client.get('/tenant/jira-policy', headers=beta).json()['allowed_project_keys'] == []


def test_jira_state_validation(client, monkeypatch):
    headers = login(client)
    client.post('/integrations/jira/connect', headers=headers)
    assert client.get('/integrations/jira/callback?state=bad&code=abc').status_code == 400

    monkeypatch.setattr(jira_oauth, '_token_exchange', lambda code: {'access_token': 'a', 'expires_in': 3600})
    monkeypatch.setattr(jira_oauth, '_accessible_resources', lambda token: [{'id': 'cloud-1', 'name': 'Acme Jira', 'url': 'https://acme.atlassian.net', 'scopes': ['read:jira-work']}])
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text('SELECT state FROM jira_oauth_states ORDER BY created_at DESC LIMIT 1')).scalar()
    assert client.get(f'/integrations/jira/callback?state={state}&code=ok').status_code == 200
    assert client.get(f'/integrations/jira/callback?state={state}&code=ok').status_code == 400


def test_jira_create_issue_gating_and_allowlist(client, monkeypatch):
    headers = login(client)
    _connect_jira(client, headers, monkeypatch)

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan':['create'],'tool_calls':[{'tool':'jira_create_issue','args':{'project_key':'ENG','issue_type':'Task','summary':'hello'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status':'success','summary':'ok','follow_up_questions':[]})

    from app.integrations.jira.jira_client import JiraClient
    monkeypatch.setattr(JiraClient, 'create_issue', lambda self, project_key, issue_type, summary, description=None, priority=None, assignee_account_id=None, labels=None: {'key':'ENG-1'})

    t = client.post('/tasks', headers=headers, json={'title':'jira','description':'jira create', 'risk_level':'LOW'}).json()
    r = client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve':False}).json()
    assert client.get(f"/runs/{r['run_id']}", headers=headers).json()['status'] == 'PENDING_APPROVAL'

    client.put('/tenant/jira-policy', headers=headers, json={'allowed_project_keys':['ENG'], 'allow_write': True})
    r2 = client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve':False}).json()
    assert client.get(f"/runs/{r2['run_id']}", headers=headers).status_code == 200


def test_jira_transition_always_high(client, monkeypatch):
    headers = login(client)
    _connect_jira(client, headers, monkeypatch)
    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan':['tr'],'tool_calls':[{'tool':'jira_transition_issue','args':{'issue_key':'ENG-1','transition_id':'31'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status':'success','summary':'ok','follow_up_questions':[]})
    t = client.post('/tasks', headers=headers, json={'title':'jira','description':'jira transition', 'risk_level':'LOW'}).json()
    r = client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve':False}).json()
    assert client.get(f"/runs/{r['run_id']}", headers=headers).json()['status'] == 'PENDING_APPROVAL'


def test_jira_idempotency_create_and_comment(client, monkeypatch):
    headers = login(client)
    _connect_jira(client, headers, monkeypatch)
    client.put('/tenant/jira-policy', headers=headers, json={'allowed_project_keys':['ENG'], 'allow_write': True})

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan':['a','b','c','d'],'tool_calls':[
        {'tool':'jira_create_issue','args':{'project_key':'ENG','issue_type':'Task','summary':'same','description':'d'}},
        {'tool':'jira_create_issue','args':{'project_key':'ENG','issue_type':'Task','summary':'same','description':'d'}},
        {'tool':'jira_add_comment','args':{'issue_key':'ENG-1','body_text':'note'}},
        {'tool':'jira_add_comment','args':{'issue_key':'ENG-1','body_text':'note'}},
    ]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status':'success','summary':'ok','follow_up_questions':[]})

    from app.integrations.jira.jira_client import JiraClient
    calls={'create':0,'comment':0}
    monkeypatch.setattr(JiraClient, 'create_issue', lambda self, project_key, issue_type, summary, description=None, priority=None, assignee_account_id=None, labels=None: calls.__setitem__('create', calls['create']+1) or {'key':'ENG-1'})
    monkeypatch.setattr(JiraClient, 'add_comment', lambda self, issue_key, body_text: calls.__setitem__('comment', calls['comment']+1) or {'id':'1001'})

    t = client.post('/tasks', headers=headers, json={'title':'jira','description':'jira dup', 'risk_level':'LOW'}).json()
    client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve':False})
    assert calls['create'] == 1
    assert calls['comment'] == 1


def test_jira_refresh_rotating_token_persists(client, monkeypatch):
    headers = login(client)
    _connect_jira(client, headers, monkeypatch)
    client.put('/tenant/jira-policy', headers=headers, json={'allowed_project_keys':['ENG'], 'allow_write': True})
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        conn.execute(text("UPDATE jira_oauth_credentials SET token_expires_at=now() - interval '10 minutes'"))

    from urllib import request as ureq
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"access_token":"at-new","refresh_token":"rt-new","expires_in":3600}'
    monkeypatch.setattr(ureq, 'urlopen', lambda *a, **k: Resp())

    from app.integrations.jira.jira_client import JiraClient
    monkeypatch.setattr(JiraClient, 'create_issue', lambda self, project_key, issue_type, summary, description=None, priority=None, assignee_account_id=None, labels=None: {'key':'ENG-1'})
    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan':['a'],'tool_calls':[{'tool':'jira_create_issue','args':{'project_key':'ENG','issue_type':'Task','summary':'x'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status':'success','summary':'ok','follow_up_questions':[]})

    t = client.post('/tasks', headers=headers, json={'title':'jira','description':'jira refresh', 'risk_level':'LOW'}).json()
    client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve':False})
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        assert conn.execute(text("SELECT refresh_token_enc FROM jira_oauth_credentials WHERE is_primary=TRUE")).scalar() is not None
