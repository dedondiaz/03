from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from sqlalchemy import text


@dataclass
class QuotaExceededError(Exception):
    kind: str
    limit: int
    used: int
    reset_at: str

    def as_dict(self):
        return {"error": "quota_exceeded", "kind": self.kind, "limit": self.limit, "used": self.used, "reset_at": self.reset_at}


DEFAULT_LIMITS = {
    "api_requests_per_minute": 120,
    "tool_calls_per_day": 200,
    "llm_tokens_per_day": 200000,
    "workflow_runs_per_day": 100,
    "automation_runs_per_day": 100,
    "web_automation_runs_per_day": 30,
    "web_automation_runtime_seconds_per_day": 1200,
}

KIND_TO_FIELD = {
    "api_requests": ("api_requests_per_minute", "api_requests_count"),
    "tool_calls": ("tool_calls_per_day", "tool_calls_count"),
    "llm_tokens": ("llm_tokens_per_day", "llm_tokens_count"),
    "workflow_runs": ("workflow_runs_per_day", "workflow_runs_count"),
    "automation_runs": ("automation_runs_per_day", "automation_runs_count"),
    "web_automation_runs": ("web_automation_runs_per_day", "web_automation_runs_count"),
    "web_automation_runtime_seconds": ("web_automation_runtime_seconds_per_day", "web_automation_runtime_seconds"),
}


def get_limits(db, tenant_id: str) -> dict:
    row = db.execute(text("SELECT limits_json FROM tenant_plans WHERE tenant_id=:tenant_id"), {"tenant_id": tenant_id}).fetchone()
    limits = dict(DEFAULT_LIMITS)
    if row and row.limits_json:
        limits.update(row.limits_json)
    return limits


def check_quota_or_raise(db, tenant_id: str, kind: str, amount: int = 1):
    limit_key, usage_field = KIND_TO_FIELD[kind]
    limits = get_limits(db, tenant_id)
    limit = int(limits.get(limit_key, DEFAULT_LIMITS[limit_key]))
    if limit <= 0:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    row = db.execute(text(f"SELECT {usage_field} FROM tenant_usage_daily WHERE tenant_id=:tenant_id AND day=:day"), {"tenant_id": tenant_id, "day": today}).fetchone()
    used = int(getattr(row, usage_field, 0) if row else 0)
    if used + int(amount) > limit:
        reset_at = datetime.combine(datetime.now(timezone.utc).date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).isoformat()
        raise QuotaExceededError(kind=kind, limit=limit, used=used, reset_at=reset_at)
