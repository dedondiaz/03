from sqlalchemy import text

from app.integrations.jira.jira_client import JiraClient
from .common import truncate, overlap_score, parse_dt


def _looks_like_jql(query: str) -> bool:
    x = query.upper()
    return "=" in query or "ORDER BY" in x or "PROJECT=" in x


def _to_jql(query: str) -> str:
    if _looks_like_jql(query):
        return query
    safe = query.replace('"', '\\"')
    return f'text ~ "{safe}" ORDER BY updated DESC'


def search(db, query: str, limit: int) -> list[dict]:
    jql = _to_jql(query)
    out = JiraClient(db).search_issues(jql=jql, max_results=min(limit, 50), fields=["summary", "status", "assignee", "updated"]) 
    site = db.execute(text("SELECT site_url FROM jira_oauth_credentials WHERE is_primary=TRUE LIMIT 1")).fetchone()
    site_url = site.site_url if site else None
    results = []
    for it in out.get("issues", []):
        fields = it.get("fields") or {}
        summary = fields.get("summary") or "(no summary)"
        key = it.get("key") or ""
        score = 0.35 + 0.65 * overlap_score(query, summary, key)
        results.append(
            {
                "source": "jira",
                "item_type": "issue",
                "title": f"{key}: {summary}" if key else summary,
                "snippet": truncate(summary, 280),
                "url": f"{site_url}/browse/{key}" if site_url and key else None,
                "external_ref_type": "jira_issue",
                "external_ref_id": key,
                "occurred_at": parse_dt(fields.get("updated")),
                "metadata": {
                    "project_key": key.split("-")[0] if "-" in key else None,
                    "status": ((fields.get("status") or {}).get("name")),
                    "assignee": ((fields.get("assignee") or {}).get("displayName")),
                },
                "score": float(round(score, 4)),
            }
        )
    return results
