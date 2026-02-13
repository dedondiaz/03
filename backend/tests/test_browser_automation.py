import json
from sqlalchemy import text

from .conftest import login


def test_automation_policy_owner_only(client):
    owner = login(client, 'owner@example.com')
    member = login(client, 'member@example.com')

    ok = client.put('/tenant/automation-policy', headers=owner, json={"allowed_domains": ["example.com"], "allow_mutations": False})
    assert ok.status_code == 200
    denied = client.put('/tenant/automation-policy', headers=member, json={"allowed_domains": ["bad.com"]})
    assert denied.status_code == 403


def test_automation_sessions_crud(client):
    owner = login(client, 'owner@example.com')
    body = {"domain": "example.com", "storage_state": {"cookies": [], "origins": []}}
    created = client.post('/automation/sessions', headers=owner, json=body)
    assert created.status_code == 200
    sid = created.json()['id']

    listed = client.get('/automation/sessions', headers=owner).json()['sessions']
    assert any(s['id'] == sid for s in listed)

    assert client.delete(f'/automation/sessions/{sid}', headers=owner).status_code == 200


def test_artifact_access_is_tenant_scoped(client):
    owner = login(client, 'owner@example.com')
    other = login(client, 'owner@example.com')
    switch = client.post('/tenants/switch', headers=other, json={"tenant_id": "20000000-0000-0000-0000-000000000001"})
    beta = {"Authorization": f"Bearer {switch.json()['token']}"}

    # create artifact row in alpha tenant
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        run = db.execute(text("INSERT INTO browser_automation_runs (tenant_id, status, policy_snapshot_json, steps_redacted_json) VALUES ('10000000-0000-0000-0000-000000000001','COMPLETED','{}'::jsonb,'[]'::jsonb) RETURNING id")).fetchone()
        art = db.execute(text("INSERT INTO browser_automation_artifacts (tenant_id, browser_run_id, kind, file_path, sha256, byte_size, mime_type) VALUES ('10000000-0000-0000-0000-000000000001', :run, 'screenshot', 'missing.png', 'x', 0, 'image/png') RETURNING id"), {"run": str(run.id)}).fetchone()
        db.commit()
        artifact_id = str(art.id)
    finally:
        db.close()

    forbidden = client.get(f'/automation/artifacts/{artifact_id}/download', headers=beta)
    assert forbidden.status_code in (403, 404)
