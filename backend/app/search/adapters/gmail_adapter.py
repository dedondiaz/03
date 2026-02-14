from app.integrations.google.gmail_client import GmailClient
from .common import truncate, overlap_score, parse_dt


def search(db, query: str, limit: int) -> list[dict]:
    out = GmailClient(db).search(query, max_results=min(limit, 20))
    results = []
    for m in out.get("messages", []):
        score = 0.4 + 0.6 * overlap_score(query, m.get("subject"), m.get("snippet"), m.get("from"))
        results.append(
            {
                "source": "gmail",
                "item_type": "email",
                "title": m.get("subject") or "(no subject)",
                "snippet": truncate(m.get("snippet"), 280),
                "url": f"https://mail.google.com/mail/u/0/#inbox/{m.get('id')}" if m.get("id") else None,
                "external_ref_type": "gmail_message",
                "external_ref_id": str(m.get("id") or ""),
                "occurred_at": parse_dt(m.get("date")),
                "metadata": {"from": truncate(m.get("from"), 120)},
                "score": float(round(score, 4)),
            }
        )
    return results
