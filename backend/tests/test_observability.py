from sqlalchemy import text

from .conftest import login
from app.observability.health import mark_success, mark_failure


def test_integration_health_rls_isolation(client):
    a = login(client, 'owner@example.com')
    login_b = client.post('/auth/login', json={"email": "owner@example.com", "password": "dev-password"}).json()
    sw = client.post('/tenants/switch', headers={"Authorization": f"Bearer {login_b['token']}"}, json={"tenant_id": "20000000-0000-0000-0000-000000000001"}).json()
    b = {"Authorization": f"Bearer {sw['token']}"}

    from app.db import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("SET app.tenant_id='10000000-0000-0000-0000-000000000001'"))
        mark_failure(db, '10000000-0000-0000-0000-000000000001', 'google', 'x', 'redacted')
        db.commit()
    finally:
        db.close()

    ha = client.get('/ops/tenants/health', headers=a).json()
    hb = client.get('/ops/tenants/health', headers=b).json()
    ga = [x for x in ha['integration_health'] if x['integration'] == 'google'][0]
    gb = [x for x in hb['integration_health'] if x['integration'] == 'google'][0]
    assert ga['consecutive_failures'] != gb['consecutive_failures']


def test_mark_success_failure_logic():
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("SET app.tenant_id='10000000-0000-0000-0000-000000000001'"))
        mark_failure(db, '10000000-0000-0000-0000-000000000001', 'slack', 'api_error', 'safe')
        mark_failure(db, '10000000-0000-0000-0000-000000000001', 'slack', 'api_error', 'safe')
        row = db.execute(text("SELECT consecutive_failures FROM integration_health WHERE integration='slack'" )).fetchone()
        assert int(row.consecutive_failures) >= 2
        mark_success(db, '10000000-0000-0000-0000-000000000001', 'slack')
        row2 = db.execute(text("SELECT consecutive_failures, last_success_at FROM integration_health WHERE integration='slack'" )).fetchone()
        assert int(row2.consecutive_failures) == 0
        assert row2.last_success_at is not None
        db.commit()
    finally:
        db.close()


def test_ops_endpoints_shape_and_redaction(client):
    headers = login(client)
    s = client.get('/ops/metrics/summary', headers=headers)
    h = client.get('/ops/tenants/health', headers=headers)
    r = client.get('/ops/runs/recent?limit=5', headers=headers)
    assert s.status_code == 200
    assert h.status_code == 200
    assert r.status_code == 200
    body = h.json()
    assert 'integration_health' in body and 'usage_today' in body
    # redacted field should never expose raw tokens
    for item in body['integration_health']:
        msg = item.get('last_error_message') or ''
        assert 'Bearer ' not in msg and 'token' not in msg.lower()
