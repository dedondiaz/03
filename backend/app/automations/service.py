import json
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import text

from app.workflows.service import WorkflowService, WorkflowValidationError, _validate_schema


class AutomationValidationError(Exception):
    pass


def _parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    try:
        h, m = value.split(":", 1)
        return time(hour=int(h), minute=int(m))
    except Exception:
        raise AutomationValidationError("time must be HH:MM")


def compute_next_run_at(cron_expr: str, tz_name: str, from_dt: datetime | None = None) -> datetime:
    if not cron_expr:
        raise AutomationValidationError("schedule_cron is required")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        raise AutomationValidationError("invalid timezone")
    base = (from_dt or datetime.now(timezone.utc)).astimezone(tz)
    try:
        nxt = croniter(cron_expr, base).get_next(datetime)
    except Exception:
        raise AutomationValidationError("invalid cron expression")
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    return nxt.astimezone(timezone.utc)


@dataclass
class RuleDecision:
    status: str
    reason: str | None = None


class AutomationService:
    def __init__(self, db, tenant_id: str, user_id: str):
        self.db = db
        self.tenant_id = tenant_id
        self.user_id = user_id

    def _require_owner(self):
        row = self.db.execute(text("SELECT role FROM memberships WHERE user_id=:u AND tenant_id=:t"), {"u": self.user_id, "t": self.tenant_id}).fetchone()
        if not row or row.role != "owner":
            raise AutomationValidationError("Owner role required")

    def _validate_rule_payload(self, payload: dict, partial: bool = False) -> dict:
        out = dict(payload or {})
        if not partial or "timezone" in out:
            out["timezone"] = out.get("timezone") or "Asia/Kolkata"
            try:
                ZoneInfo(out["timezone"])
            except Exception:
                raise AutomationValidationError("invalid timezone")
        if not partial or "trigger_type" in out:
            out["trigger_type"] = out.get("trigger_type", "schedule")
            if out["trigger_type"] not in {"schedule", "gmail_poll"}:
                raise AutomationValidationError("invalid trigger_type")
        if out.get("trigger_type", "schedule") == "schedule":
            if (not partial) or ("schedule_cron" in out):
                if not out.get("schedule_cron"):
                    raise AutomationValidationError("schedule_cron required for schedule trigger")
                compute_next_run_at(out["schedule_cron"], out.get("timezone", "Asia/Kolkata"), datetime.now(timezone.utc))
        if "quiet_hours_start" in out:
            _parse_hhmm(out.get("quiet_hours_start"))
        if "quiet_hours_end" in out:
            _parse_hhmm(out.get("quiet_hours_end"))
        if not partial or "max_runs_per_day" in out:
            out["max_runs_per_day"] = int(out.get("max_runs_per_day", 3))
            if out["max_runs_per_day"] < 1 or out["max_runs_per_day"] > 100:
                raise AutomationValidationError("max_runs_per_day out of range")
        if not partial or "max_concurrent_runs" in out:
            out["max_concurrent_runs"] = int(out.get("max_concurrent_runs", 1))
            if out["max_concurrent_runs"] < 1 or out["max_concurrent_runs"] > 20:
                raise AutomationValidationError("max_concurrent_runs out of range")
        return out

    def _validate_template_input(self, template_id: str, input_json: dict) -> dict:
        t = self.db.execute(text("SELECT input_schema_json, enabled FROM workflow_templates WHERE id=:id"), {"id": template_id}).fetchone()
        if not t or not t.enabled:
            raise AutomationValidationError("template not found")
        try:
            return _validate_schema(t.input_schema_json, input_json)
        except WorkflowValidationError as exc:
            raise AutomationValidationError(str(exc))

    def list_rules(self) -> list[dict]:
        rows = self.db.execute(text("SELECT id, name, template_id, trigger_type, schedule_cron, timezone, enabled, max_runs_per_day, max_concurrent_runs, last_run_at, next_run_at, last_error FROM automation_rules ORDER BY created_at DESC")).fetchall()
        return [{
            "id": str(r.id), "name": r.name, "template_id": r.template_id, "trigger_type": r.trigger_type,
            "schedule_cron": r.schedule_cron, "timezone": r.timezone, "enabled": r.enabled,
            "max_runs_per_day": r.max_runs_per_day, "max_concurrent_runs": r.max_concurrent_runs,
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
            "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
            "last_error": r.last_error,
        } for r in rows]

    def get_rule(self, rule_id: str) -> dict:
        r = self.db.execute(text("SELECT * FROM automation_rules WHERE id=:id"), {"id": rule_id}).fetchone()
        if not r:
            raise AutomationValidationError("rule not found")
        return {k: (v.isoformat() if hasattr(v, "isoformat") and v else v) for k, v in r._mapping.items()}

    def create_rule(self, payload: dict) -> dict:
        self._require_owner()
        data = self._validate_rule_payload(payload)
        template_id = str(payload.get("template_id", ""))
        if not template_id:
            raise AutomationValidationError("template_id is required")
        validated_input = self._validate_template_input(template_id, payload.get("input") or {})
        next_run = compute_next_run_at(data["schedule_cron"], data["timezone"], datetime.now(timezone.utc)) if data["trigger_type"] == "schedule" else None
        row = self.db.execute(text("""
            INSERT INTO automation_rules (
                tenant_id, created_by, name, template_id, input_json, trigger_type, schedule_cron, timezone,
                quiet_hours_start, quiet_hours_end, enabled, max_runs_per_day, max_concurrent_runs, next_run_at, updated_at
            ) VALUES (
                :tenant_id, :created_by, :name, :template_id, :input_json::jsonb, :trigger_type, :schedule_cron, :timezone,
                :quiet_hours_start, :quiet_hours_end, :enabled, :max_runs_per_day, :max_concurrent_runs, :next_run_at, now()
            ) RETURNING id
        """), {
            "tenant_id": self.tenant_id,
            "created_by": self.user_id,
            "name": str(payload.get("name") or "Automation Rule"),
            "template_id": template_id,
            "input_json": json.dumps(validated_input),
            "trigger_type": data["trigger_type"],
            "schedule_cron": data.get("schedule_cron"),
            "timezone": data["timezone"],
            "quiet_hours_start": data.get("quiet_hours_start"),
            "quiet_hours_end": data.get("quiet_hours_end"),
            "enabled": bool(payload.get("enabled", True)),
            "max_runs_per_day": data["max_runs_per_day"],
            "max_concurrent_runs": data["max_concurrent_runs"],
            "next_run_at": next_run,
        }).fetchone()
        return self.get_rule(str(row.id))

    def update_rule(self, rule_id: str, payload: dict) -> dict:
        self._require_owner()
        existing = self.get_rule(rule_id)
        merged = {**existing, **(payload or {})}
        data = self._validate_rule_payload(merged, partial=False)
        validated_input = self._validate_template_input(str(merged["template_id"]), merged.get("input_json") if "input_json" in merged else (payload.get("input") or existing.get("input_json") or {}))
        next_run = compute_next_run_at(data["schedule_cron"], data["timezone"], datetime.now(timezone.utc)) if data["trigger_type"] == "schedule" and bool(merged.get("enabled", True)) else None
        self.db.execute(text("""
            UPDATE automation_rules SET
              name=:name,
              template_id=:template_id,
              input_json=:input_json::jsonb,
              trigger_type=:trigger_type,
              schedule_cron=:schedule_cron,
              timezone=:timezone,
              quiet_hours_start=:quiet_hours_start,
              quiet_hours_end=:quiet_hours_end,
              enabled=:enabled,
              max_runs_per_day=:max_runs_per_day,
              max_concurrent_runs=:max_concurrent_runs,
              next_run_at=:next_run_at,
              updated_at=now()
            WHERE id=:id
        """), {
            "id": rule_id,
            "name": str(merged.get("name") or existing["name"]),
            "template_id": str(merged["template_id"]),
            "input_json": json.dumps(validated_input),
            "trigger_type": data["trigger_type"],
            "schedule_cron": data.get("schedule_cron"),
            "timezone": data["timezone"],
            "quiet_hours_start": data.get("quiet_hours_start"),
            "quiet_hours_end": data.get("quiet_hours_end"),
            "enabled": bool(merged.get("enabled", True)),
            "max_runs_per_day": data["max_runs_per_day"],
            "max_concurrent_runs": data["max_concurrent_runs"],
            "next_run_at": next_run,
        })
        return self.get_rule(rule_id)

    def toggle_rule(self, rule_id: str, enabled: bool) -> dict:
        self._require_owner()
        rule = self.get_rule(rule_id)
        next_run = None
        if enabled and rule.get("trigger_type") == "schedule":
            next_run = compute_next_run_at(rule.get("schedule_cron"), rule.get("timezone") or "Asia/Kolkata", datetime.now(timezone.utc))
        self.db.execute(text("UPDATE automation_rules SET enabled=:enabled, next_run_at=:next_run_at, updated_at=now() WHERE id=:id"), {"id": rule_id, "enabled": bool(enabled), "next_run_at": next_run})
        return self.get_rule(rule_id)

    def list_executions(self, rule_id: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        if rule_id:
            rows = self.db.execute(text("SELECT id, rule_id, workflow_run_id, status, scheduled_for, started_at, finished_at, reason, created_at FROM automation_executions WHERE rule_id=:rule_id ORDER BY created_at DESC LIMIT :limit"), {"rule_id": rule_id, "limit": limit}).fetchall()
        else:
            rows = self.db.execute(text("SELECT id, rule_id, workflow_run_id, status, scheduled_for, started_at, finished_at, reason, created_at FROM automation_executions ORDER BY created_at DESC LIMIT :limit"), {"limit": limit}).fetchall()
        return [{
            "id": str(r.id), "rule_id": str(r.rule_id), "workflow_run_id": str(r.workflow_run_id) if r.workflow_run_id else None,
            "status": r.status,
            "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "reason": r.reason,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]


def _in_quiet_hours(now_utc: datetime, tz_name: str, start_hhmm: str | None, end_hhmm: str | None) -> bool:
    s = _parse_hhmm(start_hhmm)
    e = _parse_hhmm(end_hhmm)
    if not s or not e:
        return False
    local = now_utc.astimezone(ZoneInfo(tz_name)).time()
    if s <= e:
        return s <= local < e
    return local >= s or local < e


def _day_bounds(now_utc: datetime, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    local = now_utc.astimezone(tz)
    start_local = datetime(local.year, local.month, local.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def run_scheduler_tick(db, now_dt: datetime | None = None) -> list[dict]:
    now_utc = (now_dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    due = db.execute(text("SELECT * FROM automation_rules WHERE enabled=TRUE AND next_run_at IS NOT NULL AND next_run_at <= :now ORDER BY next_run_at ASC LIMIT 200"), {"now": now_utc}).fetchall()
    events: list[dict] = []
    for rule in due:
        tenant_id = str(rule.tenant_id)
        user_id = str(rule.created_by)
        status = "queued"
        reason = None
        workflow_run_id = None

        if _in_quiet_hours(now_utc, rule.timezone or "Asia/Kolkata", rule.quiet_hours_start, rule.quiet_hours_end):
            status = "skipped_quiet_hours"
            reason = "within quiet hours"
        else:
            day_start, day_end = _day_bounds(now_utc, rule.timezone or "Asia/Kolkata")
            count_today = db.execute(text("SELECT count(*) FROM automation_executions WHERE rule_id=:rule_id AND created_at>=:start AND created_at<:end"), {"rule_id": str(rule.id), "start": day_start, "end": day_end}).scalar() or 0
            if int(count_today) >= int(rule.max_runs_per_day):
                status = "skipped_quota"
                reason = "daily quota reached"
            else:
                active = db.execute(text("SELECT count(*) FROM workflow_runs WHERE triggered_by_rule_id=:rule_id AND status IN ('queued','running','waiting_approval')"), {"rule_id": str(rule.id)}).scalar() or 0
                if int(active) >= int(rule.max_concurrent_runs):
                    status = "skipped_quota"
                    reason = "max concurrency reached"
                else:
                    try:
                        check_quota_or_raise(db, tenant_id, "automation_runs", 1)
                        db.execute(text("SET app.tenant_id = :tenant_id"), {"tenant_id": tenant_id})
                        db.execute(text("SET app.user_id = :user_id"), {"user_id": user_id})
                        wfsvc = WorkflowService(db, tenant_id, user_id)
                        created = wfsvc.create_run(str(rule.template_id), dict(rule.input_json or {}), triggered_by_rule_id=str(rule.id))
                        workflow_run_id = created["id"]
                        db.execute(text("UPDATE workflow_runs SET triggered_by_rule_id=:rule_id WHERE id=:id"), {"rule_id": str(rule.id), "id": workflow_run_id})
                        status = "enqueued"
                        reason = None
                        meter_automation_run(db, tenant_id, 1)
                    except QuotaExceededError:
                        status = "skipped_quota"
                        reason = "tenant automation quota exceeded"
                    except Exception as exc:
                        status = "failed"
                        reason = str(exc)[:200]

        next_run = compute_next_run_at(rule.schedule_cron, rule.timezone or "Asia/Kolkata", now_utc) if rule.trigger_type == "schedule" and rule.schedule_cron else None
        db.execute(text("UPDATE automation_rules SET last_run_at=:last_run_at, next_run_at=:next_run_at, last_error=:last_error, updated_at=now() WHERE id=:id"), {
            "id": str(rule.id),
            "last_run_at": now_utc if status == "enqueued" else rule.last_run_at,
            "next_run_at": next_run,
            "last_error": reason if status == "failed" else None,
        })
        exec_row = db.execute(text("""
            INSERT INTO automation_executions (tenant_id, rule_id, workflow_run_id, status, scheduled_for, started_at, finished_at, reason)
            VALUES (:tenant_id, :rule_id, :workflow_run_id, :status, :scheduled_for, :started_at, :finished_at, :reason)
            RETURNING id
        """), {
            "tenant_id": tenant_id,
            "rule_id": str(rule.id),
            "workflow_run_id": workflow_run_id,
            "status": status,
            "scheduled_for": rule.next_run_at,
            "started_at": now_utc,
            "finished_at": now_utc,
            "reason": reason,
        }).fetchone()
        db.execute(text("INSERT INTO audit_logs (tenant_id, run_id, event_type, payload) VALUES (:tenant_id, :run_id, :event_type, :payload::jsonb)"), {
            "tenant_id": tenant_id,
            "run_id": workflow_run_id,
            "event_type": "automation_execution",
            "payload": json.dumps({"rule_id": str(rule.id), "execution_id": str(exec_row.id), "status": status, "reason": reason}),
        })
        events.append({"rule_id": str(rule.id), "execution_id": str(exec_row.id), "status": status, "workflow_run_id": workflow_run_id})
    return events
