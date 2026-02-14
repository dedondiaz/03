from datetime import datetime, timezone

from sqlalchemy import text

from app.db import engine
from app.search.search_service import SearchService


def test_search_tenant_isolation_notion_docs(client, login):
    headers = login(client)
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        conn.execute(text("INSERT INTO notion_documents (tenant_id, notion_page_id, title, content) VALUES ('10000000-0000-0000-0000-000000000001','a1','Alpha Plan','alpha secret roadmap')"))
        conn.execute(text("SET app.tenant_id = '20000000-0000-0000-0000-000000000001'"))
        conn.execute(text("INSERT INTO notion_documents (tenant_id, notion_page_id, title, content) VALUES ('20000000-0000-0000-0000-000000000001','b1','Beta Plan','beta confidential')"))

    res = client.get('/search?q=plan&sources=notion&limit=20', headers=headers)
    assert res.status_code == 200
    out = res.json()
    ids = [r['external_ref_id'] for r in out['results']]
    assert 'a1' in ids
    assert 'b1' not in ids


def test_search_partial_connectivity_warnings(client, monkeypatch, login):
    headers = login(client)

    from app.search import search_service as ss
    monkeypatch.setattr(ss.gmail_adapter, 'search', lambda db, query, limit: [{
        'source': 'gmail', 'item_type': 'email', 'title': 'mail', 'snippet': 'snippet', 'url': None,
        'external_ref_type': 'gmail_message', 'external_ref_id': 'm1', 'occurred_at': datetime.now(timezone.utc), 'metadata': {}, 'score': 0.8
    }])
    monkeypatch.setattr(ss.jira_adapter, 'search', lambda db, query, limit: [{
        'source': 'jira', 'item_type': 'issue', 'title': 'issue', 'snippet': 'snippet', 'url': None,
        'external_ref_type': 'jira_issue', 'external_ref_id': 'ENG-1', 'occurred_at': datetime.now(timezone.utc), 'metadata': {}, 'score': 0.7
    }])

    res = client.get('/search?q=test&sources=gmail,slack,jira,notion', headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert any(r['source'] == 'gmail' for r in body['results'])
    assert any(r['source'] == 'jira' for r in body['results'])
    assert any(w['source'] == 'slack' for w in body['warnings'])


def test_search_ranking_merge_order(monkeypatch):
    class FakeDB:
        def execute(self, *_args, **_kwargs):
            class R:
                def fetchone(self):
                    return None
                def scalar(self):
                    return 0
            return R()

    svc = SearchService(FakeDB(), '10000000-0000-0000-0000-000000000001')
    from app.search import search_service as ss

    monkeypatch.setattr(ss.gmail_adapter, "search", lambda db, query, limit: [
        {'source': 'gmail', 'item_type': 'email', 'title': 'a', 'snippet': '', 'url': None, 'external_ref_type': 'gmail_message', 'external_ref_id': '1', 'occurred_at': datetime(2024, 1, 1, tzinfo=timezone.utc), 'metadata': {}, 'score': 0.9}
    ])
    monkeypatch.setattr(ss.slack_adapter, 'search', lambda db, tenant_id, query, limit: ([
        {'source': 'slack', 'item_type': 'message', 'title': 'b', 'snippet': '', 'url': None, 'external_ref_type': 'slack_message', 'external_ref_id': '2', 'occurred_at': datetime(2024, 1, 2, tzinfo=timezone.utc), 'metadata': {}, 'score': 0.9}
    ], []))
    monkeypatch.setattr(ss.jira_adapter, 'search', lambda db, query, limit: [
        {'source': 'jira', 'item_type': 'issue', 'title': 'c', 'snippet': '', 'url': None, 'external_ref_type': 'jira_issue', 'external_ref_id': '3', 'occurred_at': datetime(2024, 1, 3, tzinfo=timezone.utc), 'metadata': {}, 'score': 0.8}
    ])
    monkeypatch.setattr(ss.notion_adapter, 'search', lambda db, query, limit: [])

    out = svc.search('x', ['gmail', 'slack', 'jira'], 20)
    assert [r['source'] for r in out['results'][:2]] == ['gmail', 'slack']


def test_unified_search_tool_redaction_persistence(client, monkeypatch, login):
    headers = login(client)

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['search'], 'tool_calls': [{'tool': 'unified_search', 'args': {'query': 'q' * 800, 'sources': ['gmail', 'notion'], 'limit': 50}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    from app.tools import impl as tool_impl
    monkeypatch.setattr(tool_impl.SearchService, 'search', lambda self, query, sources=None, limit=20: {
        'results': [
            {'source': 'notion', 'item_type': 'doc', 'title': 'T', 'snippet': 'x' * 3000, 'url': None, 'external_ref_type': 'notion_page', 'external_ref_id': 'p1', 'occurred_at': None, 'metadata': {'channel_id': 'C1', 'unsafe': 'y' * 500}, 'score': 0.5}
            for _ in range(30)
        ],
        'warnings': [{'source': 'slack', 'code': 'not_connected', 'message': 'missing'}],
        'took_ms': 12,
    })

    task = client.post('/tasks', headers=headers, json={'title': 's', 'description': 'search', 'risk_level': 'LOW'}).json()
    run = client.post(f"/tasks/{task['id']}/run", headers=headers, json={'approve': False}).json()
    detail = client.get(f"/runs/{run['run_id']}", headers=headers).json()
    inv = detail['tool_invocations'][0]
    assert len(inv['args']['query']) <= 200
    assert len(inv['result']['results']) == 10
    assert len(inv['result']['results'][0]['snippet']) <= 180
    assert 'unsafe' not in inv['result']['results'][0]['metadata']


def test_slack_adapter_policy_enforcement(client, monkeypatch, login):
    headers = login(client)
    client.put('/tenant/slack-policy', headers=headers, json={'allowed_channel_ids': ['C1', 'C2'], 'allow_external_shared': False})

    from app.search.adapters import slack_adapter

    calls = []
    class FakeSlack:
        def __init__(self, db):
            self.db = db
        def conversations_history(self, channel_id, limit=20):
            calls.append(channel_id)
            return {'messages': [{'ts': '1710000000.000100', 'text': f'hello from {channel_id}', 'user': 'U1'}]}

    monkeypatch.setattr(slack_adapter, 'SlackClient', FakeSlack)

    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        results, warnings = slack_adapter.search(conn, '10000000-0000-0000-0000-000000000001', 'hello', 20)

    assert not warnings
    assert set(calls) == {'C1', 'C2'}
    assert all(r['metadata']['channel_id'] in {'C1', 'C2'} for r in results)
