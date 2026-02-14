from datetime import datetime, timezone
from sqlalchemy import text

from app.integrations.slack.slack_client import SlackClient
from .common import truncate, overlap_score


def search(db, tenant_id: str, query: str, limit: int) -> tuple[list[dict], list[dict]]:
    row = db.execute(
        text("SELECT allowed_channel_ids FROM tenant_slack_policies WHERE tenant_id=:tenant_id"),
        {"tenant_id": tenant_id},
    ).fetchone()
    allowed = (row.allowed_channel_ids if row else []) or []
    if not allowed:
        return [], [{"source": "slack", "code": "not_configured", "message": "Slack not configured: set allowed channels in Settings"}]

    c = SlackClient(db)
    results = []
    for channel_id in allowed[:10]:
        out = c.conversations_history(channel_id=channel_id, limit=20)
        for msg in out.get("messages", []):
            text_body = (msg.get("text") or "").strip()
            if not text_body:
                continue
            score = overlap_score(query, text_body)
            if score <= 0:
                continue
            ts = msg.get("ts")
            occurred = None
            try:
                occurred = datetime.fromtimestamp(float(ts), tz=timezone.utc) if ts else None
            except Exception:
                occurred = None
            results.append(
                {
                    "source": "slack",
                    "item_type": "message",
                    "title": f"Slack message in {channel_id}",
                    "snippet": truncate(text_body, 280),
                    "url": f"https://slack.com/app_redirect?channel={channel_id}&message_ts={ts}" if ts else None,
                    "external_ref_type": "slack_message",
                    "external_ref_id": f"{channel_id}:{ts}",
                    "occurred_at": occurred,
                    "metadata": {"channel_id": channel_id, "user": msg.get("user")},
                    "score": float(round(0.3 + 0.7 * score, 4)),
                }
            )
    return results[:limit], []
