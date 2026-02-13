from sqlalchemy import text
from app.db import engine
from app.integrations.notion import oauth as notion_oauth
from .conftest import login


def _connect_notion(client, headers, monkeypatch):
    monkeypatch.setattr(notion_oauth, '_exchange', lambda code: {
        'access_token': 'secret-token',
        'workspace_id': 'ws1',
        'workspace_name': 'Acme Docs',
        'bot_id': 'bot1',
    })
    client.post('/integrations/notion/connect', headers=headers)
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text('SELECT state FROM notion_oauth_states ORDER BY created_at DESC LIMIT 1')).scalar()
    return client.get(f'/integrations/notion/callback?state={state}&code=abc')


def test_notion_connect_status_disconnect(client, monkeypatch):
    headers = login(client)
    assert client.get('/integrations/notion/status', headers=headers).json()['connected'] is False
    assert _connect_notion(client, headers, monkeypatch).status_code == 200
    status = client.get('/integrations/notion/status', headers=headers).json()
    assert status['connected'] is True
    assert status['workspace_name'] == 'Acme Docs'
    assert client.post('/integrations/notion/disconnect', headers=headers).json()['connected'] is False


def test_notion_rls_isolation(client, monkeypatch):
    headers = login(client)
    _connect_notion(client, headers, monkeypatch)
    switch = client.post('/tenants/switch', headers=headers, json={'tenant_id': '20000000-0000-0000-0000-000000000001'})
    beta_headers = {'Authorization': f"Bearer {switch.json()['token']}"}
    assert client.get('/integrations/notion/status', headers=beta_headers).json()['connected'] is False


def test_notion_sync_tool(client, monkeypatch):
    headers = login(client)
    _connect_notion(client, headers, monkeypatch)

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan':['sync'],'tool_calls':[{'tool':'notion_sync_page','args':{'page_id':'p1'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status':'success','summary':'ok','follow_up_questions':[]})

    from app.integrations.notion.notion_client import NotionClient
    monkeypatch.setattr(NotionClient, 'retrieve_page', lambda self, page_id: {'id': page_id, 'url': 'https://notion.so/p1', 'properties': {'title': {'type':'title', 'title':[{'plain_text':'Page One'}]}}})
    monkeypatch.setattr(NotionClient, 'retrieve_block_children', lambda self, block_id, start_cursor=None, page_size=100: {'results':[{'type':'paragraph','paragraph':{'rich_text':[{'plain_text':'hello world'}]}}], 'has_more': False})

    t = client.post('/tasks', headers=headers, json={'title':'notion','description':'sync notion', 'risk_level':'LOW'}).json()
    r = client.post(f"/tasks/{t['id']}/run", headers=headers, json={'approve':False}).json()
    assert client.get(f"/runs/{r['run_id']}", headers=headers).status_code == 200

    docs = client.get('/knowledge/notion-docs', headers=headers).json()
    assert len(docs) == 1
    assert docs[0]['title'] == 'Page One'
