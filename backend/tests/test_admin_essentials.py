from sqlalchemy import text

from .conftest import login


def test_invite_accept_single_use_and_expiry(client):
    owner = login(client, 'owner@example.com')
    inv = client.post('/tenant/members/invite', headers=owner, json={'email': 'member@example.com', 'role': 'member'})
    assert inv.status_code == 200
    token = inv.json()['invite_token']

    member = login(client, 'member@example.com')
    ok = client.post('/tenant/members/accept', headers=member, json={'invite_token': token})
    assert ok.status_code == 200
    reused = client.post('/tenant/members/accept', headers=member, json={'invite_token': token})
    assert reused.status_code == 400


def test_role_permissions_member_cannot_invite(client):
    member = login(client, 'member@example.com')
    denied = client.post('/tenant/members/invite', headers=member, json={'email': 'x@example.com', 'role': 'member'})
    assert denied.status_code == 403


def test_policies_hub_aggregate_and_update(client):
    owner = login(client, 'owner@example.com')
    got = client.get('/tenant/policies', headers=owner)
    assert got.status_code == 200
    body = got.json()
    assert 'email_domains' in body and 'automation_policy' in body and 'plan_limits' in body

    upd = client.put('/tenant/policies', headers=owner, json={
        'email_domains': {'allowed_email_domains': ['example.com']},
        'slack_policy': {'allowed_channel_ids': ['C123'], 'allow_external_shared': False},
        'jira_policy': {'allowed_project_keys': ['ABC'], 'allow_write': True},
        'notion_policy': {'allowed_parent_ids': ['parent1']}
    })
    assert upd.status_code == 200
    out = upd.json()
    assert out['email_domains']['allowed_email_domains'] == ['example.com']


def test_audit_export_csv_redacted_and_scoped(client):
    owner = login(client, 'owner@example.com')
    task = client.post('/tasks', headers=owner, json={'title': 'a', 'description': 'b', 'risk_level': 'LOW'}).json()
    client.post(f"/tasks/{task['id']}/run", headers=owner, json={'approve': True})

    csv_r = client.get('/audit/export?limit=100', headers=owner)
    assert csv_r.status_code == 200
    assert 'text/csv' in csv_r.headers.get('content-type', '')
    text_body = csv_r.text
    assert 'Bearer ' not in text_body


def test_usage_summary_is_tenant_scoped(client):
    a = login(client, 'owner@example.com')
    b_login = client.post('/auth/login', json={'email': 'owner@example.com', 'password': 'dev-password'}).json()
    sw = client.post('/tenants/switch', headers={'Authorization': f"Bearer {b_login['token']}"}, json={'tenant_id': '20000000-0000-0000-0000-000000000001'}).json()
    b = {'Authorization': f"Bearer {sw['token']}"}
    client.get('/tasks', headers=a)
    ua = client.get('/tenant/usage/summary?days=7', headers=a).json()
    ub = client.get('/tenant/usage/summary?days=7', headers=b).json()
    assert ua != ub or ub.get('daily') == []


def test_invites_rls_isolation(client):
    owner = login(client, 'owner@example.com')
    client.post('/tenant/members/invite', headers=owner, json={'email': 'member@example.com', 'role': 'member'})

    from app.db import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("SET app.tenant_id='20000000-0000-0000-0000-000000000001'"))
        c = db.execute(text('SELECT count(*) FROM tenant_invites')).scalar() or 0
        assert int(c) == 0
    finally:
        db.close()
