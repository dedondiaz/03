from sqlalchemy import text

_ALLOWED = {"google", "slack", "jira", "notion", "microsoft", "web_automation"}


def _safe_integration(integration: str) -> str:
    key = (integration or "").strip().lower()
    if key not in _ALLOWED:
        raise ValueError("invalid_integration")
    return key


def mark_connected(db, tenant_id: str, integration: str, connected: bool = True):
    key = _safe_integration(integration)
    db.execute(text("""
      INSERT INTO integration_health (tenant_id, integration, connected, updated_at)
      VALUES (:tenant_id, :integration, :connected, now())
      ON CONFLICT (tenant_id, integration)
      DO UPDATE SET connected=:connected, updated_at=now(),
        consecutive_failures=CASE WHEN :connected THEN 0 ELSE integration_health.consecutive_failures END
    """), {"tenant_id": tenant_id, "integration": key, "connected": bool(connected)})


def mark_success(db, tenant_id: str, integration: str):
    key = _safe_integration(integration)
    db.execute(text("""
      INSERT INTO integration_health (tenant_id, integration, connected, last_success_at, consecutive_failures, updated_at)
      VALUES (:tenant_id, :integration, TRUE, now(), 0, now())
      ON CONFLICT (tenant_id, integration)
      DO UPDATE SET connected=TRUE, last_success_at=now(), consecutive_failures=0, updated_at=now()
    """), {"tenant_id": tenant_id, "integration": key})


def mark_failure(db, tenant_id: str, integration: str, code: str, message_redacted: str):
    key = _safe_integration(integration)
    db.execute(text("""
      INSERT INTO integration_health (tenant_id, integration, connected, last_error_at, last_error_code, last_error_message_redacted, consecutive_failures, updated_at)
      VALUES (:tenant_id, :integration, TRUE, now(), :code, :msg, 1, now())
      ON CONFLICT (tenant_id, integration)
      DO UPDATE SET connected=TRUE, last_error_at=now(), last_error_code=:code, last_error_message_redacted=:msg,
        consecutive_failures=integration_health.consecutive_failures + 1, updated_at=now()
    """), {"tenant_id": tenant_id, "integration": key, "code": (code or "error")[:80], "msg": (message_redacted or "integration_error")[:180]})
