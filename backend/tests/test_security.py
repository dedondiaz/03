from sqlalchemy import text
from app.db import engine


def test_rls_isolation_between_tenants_tasks_notes_and_invocations(client, login):
    headers = login(client)
    task = client.post("/tasks", headers=headers, json={"title": "note", "description": "create note for tenant", "risk_level": "HIGH"})
    task_id = task.json()["id"]
    run = client.post(f"/tasks/{task_id}/run", headers=headers, json={"approve": True}).json()

    switch = client.post(
        "/tenants/switch",
        headers=headers,
        json={"tenant_id": "20000000-0000-0000-0000-000000000001"},
    )
    beta_headers = {"Authorization": f"Bearer {switch.json()['token']}"}

    assert client.get("/tasks", headers=beta_headers).json() == []
    assert client.get(f"/runs/{run['run_id']}", headers=beta_headers).status_code == 404

    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '20000000-0000-0000-0000-000000000001'"))
        assert conn.execute(text("SELECT count(*) FROM notes")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM tool_invocations")).scalar() == 0


def test_run_persists_tool_invocations_and_verifier(client, login):
    headers = login(client)
    task = client.post("/tasks", headers=headers, json={"title": "Search", "description": "Tenant isolation", "risk_level": "LOW"})
    task_id = task.json()["id"]
    run = client.post(f"/tasks/{task_id}/run", headers=headers, json={"approve": False}).json()

    detail = client.get(f"/runs/{run['run_id']}", headers=headers)
    assert detail.status_code == 200
    payload = detail.json()

    assert payload["plan"] is not None
    assert len(payload["tool_invocations"]) >= 1
    assert payload["verifier"]["status"] in {"success", "needs_input", "failed"}
