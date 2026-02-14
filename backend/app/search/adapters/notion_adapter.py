from sqlalchemy import text

from .common import truncate, overlap_score, parse_dt


def search(db, query: str, limit: int) -> list[dict]:
    q = f"%{query}%"
    rows = db.execute(
        text(
            """
            SELECT notion_page_id, title, content, source_url, last_synced_at
            FROM notion_documents
            WHERE title ILIKE :q OR content ILIKE :q
            ORDER BY last_synced_at DESC
            LIMIT :lim
            """
        ),
        {"q": q, "lim": min(limit * 3, 150)},
    ).fetchall()
    results = []
    for r in rows:
        score = 0.25 + 0.75 * overlap_score(query, r.title, r.content)
        results.append(
            {
                "source": "notion",
                "item_type": "doc",
                "title": r.title,
                "snippet": truncate(r.content, 280),
                "url": r.source_url,
                "external_ref_type": "notion_page",
                "external_ref_id": r.notion_page_id,
                "occurred_at": parse_dt(r.last_synced_at),
                "metadata": {},
                "score": float(round(score, 4)),
            }
        )
    return results[:limit]
