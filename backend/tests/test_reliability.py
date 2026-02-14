from sqlalchemy import text



def test_workflow_quota_exceeded(client, login):
    headers = login(client)
    # set low workflow limit
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("SET app.tenant_id='10000000-0000-0000-0000-000000000001'"))
        db.execute(text("UPDATE tenant_plans SET limits_json = jsonb_set(limits_json, '{workflow_runs_per_day}', '0'::jsonb) WHERE tenant_id='10000000-0000-0000-0000-000000000001'"))
        db.commit()
    finally:
        db.close()

    tpl = client.get('/workflows/templates', headers=headers).json()[0]
    r = client.post('/workflows/runs', headers=headers, json={"template_id": tpl['id'], "input": {}})
    assert r.status_code == 429


def test_api_rate_limit(client, login):
    headers = login(client)
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("SET app.tenant_id='10000000-0000-0000-0000-000000000001'"))
        db.execute(text("UPDATE tenant_plans SET limits_json = jsonb_set(limits_json, '{api_requests_per_minute}', '1'::jsonb) WHERE tenant_id='10000000-0000-0000-0000-000000000001'"))
        db.commit()
    finally:
        db.close()

    assert client.get('/tasks', headers=headers).status_code == 200
    limited = client.get('/tasks', headers=headers)
    assert limited.status_code == 429


def test_cancel_queued_run(client, login):
    headers = login(client)
    task = client.post('/tasks', headers=headers, json={"title": "x", "description": "do", "risk_level": "LOW"}).json()
    run = client.post(f"/tasks/{task['id']}/run", headers=headers, json={"approve": True}).json()
    canceled = client.post(f"/runs/{run['run_id']}/cancel", headers=headers)
    assert canceled.status_code == 200
    detail = client.get(f"/runs/{run['run_id']}", headers=headers).json()
    assert detail['status'] in {'CANCELLED', 'CANCELLING', 'FAILED'}


def test_usage_is_tenant_scoped(client, login):
    a = login(client, 'owner@example.com')
    b_login = client.post('/auth/login', json={"email": "owner@example.com", "password": "dev-password"}, headers={"Content-Type": "application/json"}).json()
    sw = client.post('/tenants/switch', headers={"Authorization": f"Bearer {b_login['token']}"}, json={"tenant_id": "20000000-0000-0000-0000-000000000001"}).json()
    b = {"Authorization": f"Bearer {sw['token']}"}

    client.get('/tasks', headers=a)
    ua = client.get('/tenant/usage', headers=a).json()
    ub = client.get('/tenant/usage', headers=b).json()
    assert ua['usage'] != ub['usage'] or ub['usage'] == {}


def test_tool_call_quota_blocks_execution(client, login):
    headers = login(client)
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("SET app.tenant_id='10000000-0000-0000-0000-000000000001'"))
        db.execute(text("UPDATE tenant_plans SET limits_json = jsonb_set(limits_json, '{tool_calls_per_day}', '0'::jsonb) WHERE tenant_id='10000000-0000-0000-0000-000000000001'"))
        db.commit()
    finally:
        db.close()
    task = client.post('/tasks', headers=headers, json={"title": "quota", "description": "email task", "risk_level": "LOW"}).json()
    run = client.post(f"/tasks/{task['id']}/run", headers=headers, json={"approve": True}).json()
    detail = client.get(f"/runs/{run['run_id']}", headers=headers).json()
    assert detail['status'] in {'FAILED', 'CANCELLED', 'CANCELLING'}
