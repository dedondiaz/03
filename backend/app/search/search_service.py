import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime

from sqlalchemy import text

from app.integrations.google.oauth import GoogleNotConnectedError
from app.integrations.slack.slack_client import SlackNotConnectedError
from app.integrations.jira.jira_client import JiraNotConnectedError
from .adapters import gmail_adapter, jira_adapter, notion_adapter, slack_adapter
from .adapters.common import to_iso, parse_dt

SOURCE_PRIORITY = {"gmail": 4, "jira": 3, "notion": 2, "slack": 1}
VALID_SOURCES = ["gmail", "slack", "jira", "notion"]
PER_SOURCE_TIMEOUT_S = float(os.getenv("SEARCH_SOURCE_TIMEOUT_S", "3"))
OVERALL_TIMEOUT_S = float(os.getenv("SEARCH_OVERALL_TIMEOUT_S", "6"))


class SearchService:
    def __init__(self, db, tenant_id: str):
        self.db = db
        self.tenant_id = tenant_id

    def source_status(self) -> dict:
        gmail_connected = self.db.execute(text("SELECT 1 FROM google_oauth_credentials WHERE provider='google' LIMIT 1")).fetchone() is not None
        slack_connected = self.db.execute(text("SELECT 1 FROM slack_oauth_credentials WHERE is_primary=TRUE LIMIT 1")).fetchone() is not None
        channels_row = self.db.execute(text("SELECT allowed_channel_ids FROM tenant_slack_policies WHERE tenant_id=:tenant_id"), {"tenant_id": self.tenant_id}).fetchone()
        configured_channels = len((channels_row.allowed_channel_ids if channels_row else []) or [])
        jira_connected = self.db.execute(text("SELECT 1 FROM jira_oauth_credentials WHERE is_primary=TRUE LIMIT 1")).fetchone() is not None
        notion_connected = self.db.execute(text("SELECT 1 FROM notion_credentials LIMIT 1")).fetchone() is not None
        local_docs = self.db.execute(text("SELECT count(*) FROM notion_documents")).scalar() or 0
        return {
            "gmail": {"connected": gmail_connected},
            "slack": {"connected": slack_connected, "configured_channels": configured_channels},
            "jira": {"connected": jira_connected},
            "notion": {"connected": notion_connected, "local_docs": int(local_docs)},
        }

    def search(self, query: str, sources: list[str] | None = None, limit: int = 20) -> dict:
        started = datetime.utcnow()
        use_sources = [s for s in (sources or VALID_SOURCES) if s in VALID_SOURCES]
        limit = max(1, min(int(limit), 50))

        tasks = {}
        warnings = []
        with ThreadPoolExecutor(max_workers=len(use_sources) or 1) as pool:
            for source in use_sources:
                if source == "gmail":
                    tasks[source] = pool.submit(gmail_adapter.search, self.db, query, limit)
                elif source == "slack":
                    tasks[source] = pool.submit(slack_adapter.search, self.db, self.tenant_id, query, limit)
                elif source == "jira":
                    tasks[source] = pool.submit(jira_adapter.search, self.db, query, limit)
                elif source == "notion":
                    tasks[source] = pool.submit(notion_adapter.search, self.db, query, limit)

            results = []
            for source, fut in tasks.items():
                try:
                    data = fut.result(timeout=PER_SOURCE_TIMEOUT_S)
                    if source == "slack":
                        src_results, src_warnings = data
                        results.extend(src_results)
                        warnings.extend(src_warnings)
                    else:
                        results.extend(data)
                except FuturesTimeout:
                    warnings.append({"source": source, "code": "timeout", "message": f"{source} search timed out"})
                except (GoogleNotConnectedError, SlackNotConnectedError, JiraNotConnectedError):
                    warnings.append({"source": source, "code": "not_connected", "message": f"{source} is not connected"})
                except Exception as exc:
                    warnings.append({"source": source, "code": "error", "message": str(exc)[:120]})

        def sort_key(item: dict):
            occurred = parse_dt(item.get("occurred_at"))
            return (
                float(item.get("score", 0.0)),
                occurred.timestamp() if occurred else 0.0,
                SOURCE_PRIORITY.get(item.get("source"), 0),
            )

        merged = sorted(results, key=sort_key, reverse=True)[:limit]
        for r in merged:
            r["occurred_at"] = to_iso(parse_dt(r.get("occurred_at")))
        took_ms = int((datetime.utcnow() - started).total_seconds() * 1000)
        return {"results": merged, "warnings": warnings, "took_ms": took_ms}
