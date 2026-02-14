from datetime import datetime, timezone


def truncate(text: str | None, max_len: int = 280) -> str:
    if not text:
        return ""
    x = str(text).strip()
    return x if len(x) <= max_len else x[: max_len - 1] + "…"


def token_set(query: str) -> set[str]:
    return {t for t in query.lower().replace("\n", " ").split(" ") if t}


def overlap_score(query: str, *texts: str | None) -> float:
    q = token_set(query)
    if not q:
        return 0.0
    corpus = " ".join([t or "" for t in texts]).lower()
    hits = sum(1 for t in q if t in corpus)
    return min(1.0, hits / max(1, len(q)))


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def to_iso(value: datetime | None) -> str | None:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
