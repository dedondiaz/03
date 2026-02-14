from datetime import datetime, timezone
from sqlalchemy import text


def _bump(db, tenant_id: str, day: str, field: str, amount: int):
    db.execute(text(f"""
        INSERT INTO tenant_usage_daily (tenant_id, day, {field}, created_at, updated_at)
        VALUES (:tenant_id, :day, :amount, now(), now())
        ON CONFLICT (tenant_id, day) DO UPDATE
        SET {field} = tenant_usage_daily.{field} + :amount,
            updated_at = now()
    """), {"tenant_id": tenant_id, "day": day, "amount": int(amount)})


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def meter_api_request(db, tenant_id: str, amount: int = 1):
    _bump(db, tenant_id, _today(), "api_requests_count", amount)


def meter_tool_call(db, tenant_id: str, tool_name: str, amount: int = 1):
    _bump(db, tenant_id, _today(), "tool_calls_count", amount)


def meter_llm_tokens(db, tenant_id: str, token_count: int):
    _bump(db, tenant_id, _today(), "llm_tokens_count", token_count)


def meter_workflow_run(db, tenant_id: str, amount: int = 1):
    _bump(db, tenant_id, _today(), "workflow_runs_count", amount)


def meter_automation_run(db, tenant_id: str, amount: int = 1):
    _bump(db, tenant_id, _today(), "automation_runs_count", amount)


def meter_web_automation_runtime(db, tenant_id: str, seconds: int, runs: int = 0):
    if runs:
        _bump(db, tenant_id, _today(), "web_automation_runs_count", runs)
    if seconds:
        _bump(db, tenant_id, _today(), "web_automation_runtime_seconds", seconds)
