from .conftest import login


def test_workflow_templates_api_contract(client):
    headers = login(client)
    res = client.get('/workflows/templates', headers=headers)
    assert res.status_code == 200
    ids = {x['id'] for x in res.json()}
    assert {'email_triage_v1', 'schedule_meeting_v1', 'jira_action_items_v1'}.issubset(ids)


def test_workflow_input_validation(client):
    headers = login(client)
    bad = client.post('/workflows/runs', headers=headers, json={'template_id': 'schedule_meeting_v1', 'input': {'title': 'x'}})
    assert bad.status_code == 400


def test_workflow_create_returns_linked_run_id(client):
    headers = login(client)
    res = client.post('/workflows/runs', headers=headers, json={'template_id': 'email_triage_v1', 'input': {'mode': 'draft_only'}})
    assert res.status_code == 200
    body = res.json()
    assert body['linked_run_id']
    assert body['template_id'] == 'email_triage_v1'


def test_workflow_tenant_isolation(client):
    headers = login(client)
    created = client.post('/workflows/runs', headers=headers, json={'template_id': 'email_triage_v1', 'input': {}}).json()

    switch = client.post('/tenants/switch', headers=headers, json={'tenant_id': '20000000-0000-0000-0000-000000000001'})
    beta_headers = {'Authorization': f"Bearer {switch.json()['token']}"}
    listed = client.get('/workflows/runs', headers=beta_headers).json()
    assert all(r['id'] != created['id'] for r in listed)


def test_workflow_compiler_constraints():
    from app.workflows.compiler import compile_workflow_task

    draft_only = compile_workflow_task('email_triage_v1', 'Email Triage + Draft Replies', {'mode': 'draft_only'})
    assert 'unified_search first' in draft_only['description']
    assert 'Do NOT call gmail_send' in draft_only['description']


def test_workflow_waiting_approval_propagation(client, monkeypatch):
    headers = login(client)

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['x'], 'tool_calls': [{'tool': 'jira_transition_issue', 'args': {'issue_key': 'ENG-1', 'transition_id': '31'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    res = client.post('/workflows/runs', headers=headers, json={'template_id': 'jira_action_items_v1', 'input': {'query_or_text': 'action items', 'project_key': 'ENG'}})
    assert res.status_code == 200
    wf = res.json()

    detail = client.get(f"/workflows/runs/{wf['id']}", headers=headers).json()
    assert detail['workflow_run']['status'] == 'waiting_approval'


def test_workflow_idempotency_no_dup_within_run(client, monkeypatch):
    headers = login(client)

    client.put('/tenant/jira-policy', headers=headers, json={'allowed_project_keys': ['ENG'], 'allow_write': True})

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['x'], 'tool_calls': [
        {'tool': 'jira_create_issue', 'args': {'project_key': 'ENG', 'issue_type': 'Task', 'summary': 'dup', 'description': 'same'}},
        {'tool': 'jira_create_issue', 'args': {'project_key': 'ENG', 'issue_type': 'Task', 'summary': 'dup', 'description': 'same'}},
    ]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    from app.integrations.jira.jira_client import JiraClient
    calls = {'n': 0}
    monkeypatch.setattr(JiraClient, '__init__', lambda self, db: None)
    monkeypatch.setattr(JiraClient, 'create_issue', lambda self, project_key, issue_type, summary, description=None, priority=None, assignee_account_id=None, labels=None: calls.__setitem__('n', calls['n'] + 1) or {'key': 'ENG-1'})

    res = client.post('/workflows/runs', headers=headers, json={'template_id': 'jira_action_items_v1', 'input': {'query_or_text': 'x', 'project_key': 'ENG'}})
    assert res.status_code == 200
    assert calls['n'] == 1
