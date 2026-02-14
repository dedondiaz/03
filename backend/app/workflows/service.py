import json
from typing import Any

from sqlalchemy import text

from .compiler import compile_workflow_task
from app.usage import check_quota_or_raise, QuotaExceededError, meter_workflow_run
from .templates import TEMPLATES


class WorkflowValidationError(Exception):
    pass


def _sanitize(text_value: str | None, limit: int = 400) -> str | None:
    if text_value is None:
        return None
    x = str(text_value)
    return x if len(x) <= limit else x[: limit - 1] + "…"


def _validate_schema(schema: dict, payload: dict) -> dict:
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    additional = schema.get("additionalProperties", True)

    if not isinstance(payload, dict):
        raise WorkflowValidationError("input must be an object")

    out = {}
    for key, spec in props.items():
        if key not in payload and "default" in spec:
            out[key] = spec["default"]
    out.update(payload)

    for key in required:
        if key not in out:
            raise WorkflowValidationError(f"missing required field: {key}")

    if not additional:
        unknown = [k for k in out.keys() if k not in props]
        if unknown:
            raise WorkflowValidationError(f"unknown fields: {', '.join(unknown)}")

    for key, value in out.items():
        spec = props.get(key)
        if not spec:
            continue
        t = spec.get("type")
        if t == "integer":
            if not isinstance(value, int):
                raise WorkflowValidationError(f"{key} must be integer")
            if "minimum" in spec and value < int(spec["minimum"]):
                raise WorkflowValidationError(f"{key} below minimum")
            if "maximum" in spec and value > int(spec["maximum"]):
                raise WorkflowValidationError(f"{key} above maximum")
        elif t == "boolean":
            if not isinstance(value, bool):
                raise WorkflowValidationError(f"{key} must be boolean")
        elif t == "string":
            if not isinstance(value, str):
                raise WorkflowValidationError(f"{key} must be string")
            if spec.get("minLength") and len(value) < int(spec["minLength"]):
                raise WorkflowValidationError(f"{key} too short")
            if spec.get("enum") and value not in spec["enum"]:
                raise WorkflowValidationError(f"{key} must be one of {spec['enum']}")
        elif t == "array":
            if not isinstance(value, list):
                raise WorkflowValidationError(f"{key} must be array")
            if spec.get("minItems") and len(value) < int(spec["minItems"]):
                raise WorkflowValidationError(f"{key} requires more items")
            item_type = ((spec.get("items") or {}).get("type"))
            if item_type == "string" and any(not isinstance(v, str) for v in value):
                raise WorkflowValidationError(f"{key} items must be string")

    return out


class WorkflowService:
    def __init__(self, db, tenant_id: str, user_id: str):
        self.db = db
        self.tenant_id = tenant_id
        self.user_id = user_id

    def seed_templates(self):
        for t in TEMPLATES:
            self.db.execute(
                text(
                    """
                    INSERT INTO workflow_templates (id, name, description, input_schema_json, enabled, version, updated_at)
                    VALUES (:id, :name, :description, CAST(:schema AS jsonb), :enabled, :version, now())
                    ON CONFLICT (id) DO UPDATE SET name=:name, description=:description, input_schema_json=CAST(:schema AS jsonb), enabled=:enabled, version=:version, updated_at=now()
                    """
                ),
                {
                    "id": t["id"],
                    "name": t["name"],
                    "description": t["description"],
                    "schema": json.dumps(t["input_schema_json"]),
                    "enabled": bool(t.get("enabled", True)),
                    "version": int(t.get("version", 1)),
                },
            )

    def list_templates(self) -> list[dict[str, Any]]:
        rows = self.db.execute(text("SELECT id, name, description, input_schema_json, enabled, version FROM workflow_templates WHERE enabled=TRUE ORDER BY id")).fetchall()
        return [{"id": r.id, "name": r.name, "description": r.description, "input_schema_json": r.input_schema_json, "enabled": r.enabled, "version": r.version} for r in rows]

    def create_run(self, template_id: str, workflow_input: dict, triggered_by_rule_id: str | None = None) -> dict:
        try:
            check_quota_or_raise(self.db, self.tenant_id, "workflow_runs", 1)
        except QuotaExceededError as exc:
            raise WorkflowValidationError(exc.as_dict())
        t = self.db.execute(text("SELECT id, name, input_schema_json, enabled FROM workflow_templates WHERE id=:id"), {"id": template_id}).fetchone()
        if not t or not t.enabled:
            raise WorkflowValidationError("template not found")
        normalized = _validate_schema(t.input_schema_json, workflow_input or {})
        task_payload = compile_workflow_task(t.id, t.name, normalized)

        task = self.db.execute(
            text("INSERT INTO tasks (tenant_id, created_by, title, description, risk_level) VALUES (:tenant_id,:created_by,:title,:description,:risk_level) RETURNING id"),
            {
                "tenant_id": self.tenant_id,
                "created_by": self.user_id,
                "title": task_payload["title"],
                "description": task_payload["description"],
                "risk_level": task_payload.get("risk_level", "LOW"),
            },
        ).fetchone()
        run = self.db.execute(
            text("INSERT INTO task_runs (tenant_id, task_id, status, approval_required, created_by) VALUES (:tenant_id,:task_id,'QUEUED',FALSE,:created_by) RETURNING id"),
            {"tenant_id": self.tenant_id, "task_id": str(task.id), "created_by": self.user_id},
        ).fetchone()
        wf = self.db.execute(
            text(
                """
                INSERT INTO workflow_runs (tenant_id, template_id, input_json, status, linked_run_id, created_by, summary_text, triggered_by_rule_id)
                VALUES (:tenant_id, :template_id, CAST(:input_json AS jsonb), 'queued', :linked_run_id, :created_by, NULL, :triggered_by_rule_id)
                RETURNING id, template_id, input_json, status, linked_run_id, summary_text, created_at, updated_at
                """
            ),
            {
                "tenant_id": self.tenant_id,
                "template_id": template_id,
                "input_json": json.dumps(normalized),
                "linked_run_id": str(run.id),
                "created_by": self.user_id,
                "triggered_by_rule_id": triggered_by_rule_id,
            },
        ).fetchone()
        return {
            "id": str(wf.id),
            "template_id": wf.template_id,
            "input": wf.input_json,
            "status": wf.status,
            "linked_run_id": str(wf.linked_run_id),
            "summary_text": wf.summary_text,
            "created_at": wf.created_at.isoformat() if wf.created_at else None,
            "updated_at": wf.updated_at.isoformat() if wf.updated_at else None,
        }

    def map_status(self, run_status: str | None) -> str:
        if run_status == "PENDING_APPROVAL":
            return "waiting_approval"
        if run_status == "COMPLETED":
            return "completed"
        if run_status == "FAILED":
            return "failed"
        if run_status == "RUNNING":
            return "running"
        return "queued"

    def refresh_status(self, workflow_run_id: str) -> dict:
        row = self.db.execute(
            text(
                """
                SELECT wr.id, wr.template_id, wr.input_json, wr.linked_run_id, wr.created_at, wr.updated_at,
                       tr.status AS linked_status, tr.verifier_summary
                FROM workflow_runs wr
                LEFT JOIN task_runs tr ON tr.id = wr.linked_run_id
                WHERE wr.id=:id
                """
            ),
            {"id": workflow_run_id},
        ).fetchone()
        if not row:
            raise WorkflowValidationError("workflow run not found")
        mapped = self.map_status(row.linked_status)
        summary = _sanitize(row.verifier_summary)
        self.db.execute(
            text("UPDATE workflow_runs SET status=:status, summary_text=:summary, updated_at=now() WHERE id=:id"),
            {"id": workflow_run_id, "status": mapped, "summary": summary},
        )
        self.db.flush()
        return {
            "id": str(row.id),
            "template_id": row.template_id,
            "input": row.input_json,
            "status": mapped,
            "linked_run_id": str(row.linked_run_id) if row.linked_run_id else None,
            "summary_text": summary,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def list_runs(self) -> list[dict]:
        rows = self.db.execute(text("SELECT id FROM workflow_runs ORDER BY created_at DESC LIMIT 200")).fetchall()
        return [self.refresh_status(str(r.id)) for r in rows]
