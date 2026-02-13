import hashlib
import json
import os
import time
from pathlib import Path

import requests
from sqlalchemy import text

from app.security.crypto import encrypt_str, decrypt_str
from app.usage import check_quota_or_raise, QuotaExceededError, meter_web_automation_runtime
from app.observability.health import mark_success, mark_failure

AUTOMATION_RUNNER_URL = os.getenv("AUTOMATION_RUNNER_URL", "http://automation-runner:3000")
ARTIFACTS_ROOT = Path(os.getenv("AUTOMATION_ARTIFACTS_ROOT", "/artifacts"))


class AutomationError(Exception):
    pass


class AutomationPolicyViolation(AutomationError):
    pass


class AutomationSessionNotFound(AutomationError):
    pass


class AutomationRunnerUnavailable(AutomationError):
    pass


def trunc(v: str, n: int = 500) -> str:
    return (v[:n] + "...") if isinstance(v, str) and len(v) > n else v


def redact_steps(steps: list[dict]) -> list[dict]:
    out = []
    for s in steps:
        x = dict(s)
        if x.get("type") in {"fill", "press"} and "value" in x:
            x["value"] = "<redacted>"
        out.append(x)
    return out


def get_policy(db) -> dict:
    row = db.execute(text("SELECT allowed_domains, allowed_path_prefixes, allow_mutations, max_steps, max_runtime_seconds, retention_days FROM tenant_automation_policies WHERE tenant_id=current_setting('app.tenant_id', true)::uuid")).fetchone()
    if not row:
        return {
            "allowed_domains": [],
            "allowed_path_prefixes": [],
            "allow_mutations": False,
            "max_steps": 25,
            "max_runtime_seconds": 120,
            "retention_days": 14,
        }
    return {
        "allowed_domains": row.allowed_domains or [],
        "allowed_path_prefixes": row.allowed_path_prefixes or [],
        "allow_mutations": bool(row.allow_mutations),
        "max_steps": int(row.max_steps),
        "max_runtime_seconds": int(row.max_runtime_seconds),
        "retention_days": int(row.retention_days),
    }


def upsert_policy(db, tenant_id: str, payload: dict) -> dict:
    clean = {
        "allowed_domains": sorted({str(x).strip().lower() for x in payload.get("allowed_domains", []) if str(x).strip()}),
        "allowed_path_prefixes": [str(x).strip() for x in payload.get("allowed_path_prefixes", []) if str(x).strip()],
        "allow_mutations": bool(payload.get("allow_mutations", False)),
        "max_steps": max(1, min(int(payload.get("max_steps", 25)), 100)),
        "max_runtime_seconds": max(10, min(int(payload.get("max_runtime_seconds", 120)), 900)),
        "retention_days": max(1, min(int(payload.get("retention_days", 14)), 90)),
    }
    db.execute(text("""
        INSERT INTO tenant_automation_policies (tenant_id, allowed_domains, allowed_path_prefixes, allow_mutations, max_steps, max_runtime_seconds, retention_days, updated_at)
        VALUES (:tenant_id, :allowed_domains, :allowed_path_prefixes, :allow_mutations, :max_steps, :max_runtime_seconds, :retention_days, now())
        ON CONFLICT (tenant_id) DO UPDATE SET
          allowed_domains=:allowed_domains,
          allowed_path_prefixes=:allowed_path_prefixes,
          allow_mutations=:allow_mutations,
          max_steps=:max_steps,
          max_runtime_seconds=:max_runtime_seconds,
          retention_days=:retention_days,
          updated_at=now()
    """), {"tenant_id": tenant_id, **clean})
    return clean


def create_session(db, tenant_id: str, user_id: str, domain: str, storage_state: dict) -> str:
    row = db.execute(text("""
      INSERT INTO automation_sessions (tenant_id, domain, storage_state_enc, created_by)
      VALUES (:tenant_id, :domain, :enc, :created_by)
      RETURNING id
    """), {"tenant_id": tenant_id, "domain": domain.lower().strip(), "enc": encrypt_str(json.dumps(storage_state)), "created_by": user_id}).fetchone()
    return str(row.id)


def list_sessions(db) -> list[dict]:
    rows = db.execute(text("SELECT id, domain, created_by, created_at FROM automation_sessions ORDER BY created_at DESC")).fetchall()
    return [{"id": str(r.id), "domain": r.domain, "created_by": str(r.created_by), "created_at": r.created_at.isoformat()} for r in rows]


def delete_session(db, session_id: str) -> None:
    db.execute(text("DELETE FROM automation_sessions WHERE id=:id"), {"id": session_id})


def run_web_automation(args: dict, ctx: dict) -> dict:
    db = ctx["db"]
    policy = get_policy(db)
    if not policy["allowed_domains"]:
        raise AutomationPolicyViolation("automation_not_configured")

    steps = args.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise AutomationPolicyViolation("automation_policy_violation")

    storage_state = None
    session_id = args.get("session_id")
    if session_id:
        row = db.execute(text("SELECT storage_state_enc FROM automation_sessions WHERE id=:id"), {"id": session_id}).fetchone()
        if not row:
            raise AutomationSessionNotFound("automation_session_not_found")
        storage_state = json.loads(decrypt_str(row.storage_state_enc))

    try:
        check_quota_or_raise(db, ctx["tenant_id"], "web_automation_runs", 1)
    except QuotaExceededError:
        raise AutomationPolicyViolation("quota_exceeded")

    redacted_steps = redact_steps(steps)
    run_row = db.execute(text("""
      INSERT INTO browser_automation_runs (tenant_id, linked_run_id, session_id, status, policy_snapshot_json, steps_redacted_json)
      VALUES (:tenant_id, :linked_run_id, :session_id, 'RUNNING', :policy::jsonb, :steps::jsonb)
      RETURNING id
    """), {
        "tenant_id": ctx["tenant_id"],
        "linked_run_id": ctx.get("run_id"),
        "session_id": session_id,
        "policy": json.dumps(policy),
        "steps": json.dumps(redacted_steps),
    }).fetchone()
    browser_run_id = str(run_row.id)

    payload = {
        "tenant_id": ctx["tenant_id"],
        "browser_run_id": browser_run_id,
        "policy": policy,
        "steps": steps,
        "storage_state": storage_state,
        "headless": True,
        "record_trace": bool(args.get("record_trace", False)),
    }

    started = time.time()
    try:
        resp = requests.post(f"{AUTOMATION_RUNNER_URL}/run", json=payload, timeout=policy["max_runtime_seconds"] + 15)
        resp.raise_for_status()
        out = resp.json()
    except Exception as exc:
        db.execute(text("UPDATE browser_automation_runs SET status='FAILED', errors=:err WHERE id=:id"), {"id": browser_run_id, "err": json.dumps([trunc(str(exc), 220)])})
        mark_failure(db, ctx["tenant_id"], "web_automation", "runner_error", "web_automation_runner_error")
        raise AutomationRunnerUnavailable("automation_runner_unavailable") from exc

    artifacts = out.get("artifacts") or []
    for a in artifacts:
        db.execute(text("""
          INSERT INTO browser_automation_artifacts (tenant_id, browser_run_id, kind, step_index, file_path, sha256, byte_size, mime_type)
          VALUES (:tenant_id, :browser_run_id, :kind, :step_index, :file_path, :sha256, :byte_size, :mime_type)
        """), {
            "tenant_id": ctx["tenant_id"], "browser_run_id": browser_run_id,
            "kind": a.get("kind", "unknown"), "step_index": a.get("step_index"), "file_path": a.get("file_path"),
            "sha256": a.get("sha256", hashlib.sha256((a.get("file_path") or "").encode()).hexdigest()),
            "byte_size": int(a.get("byte_size", 0)), "mime_type": a.get("mime_type", "application/octet-stream"),
        })

    result_small = out.get("extracted") or {}
    if isinstance(result_small, dict):
        for k, v in list(result_small.items()):
            if isinstance(v, str):
                result_small[k] = trunc(v, 500)

    runtime_seconds = int(out.get("runtime_seconds", max(0, int(time.time() - started))))
    try:
        check_quota_or_raise(db, ctx["tenant_id"], "web_automation_runtime_seconds", runtime_seconds)
    except QuotaExceededError:
        raise AutomationPolicyViolation("quota_exceeded")
    meter_web_automation_runtime(db, ctx["tenant_id"], runtime_seconds, runs=1)

    db.execute(text("""
      UPDATE browser_automation_runs
      SET status=:status, result_json=:result::jsonb, final_url=:final_url, errors=:errors::jsonb, updated_at=now()
      WHERE id=:id
    """), {
        "id": browser_run_id,
        "status": out.get("status", "FAILED"),
        "result": json.dumps(result_small),
        "final_url": trunc(out.get("final_url"), 500),
        "errors": json.dumps([trunc(e, 240) for e in (out.get("errors") or [])]),
    })

    if out.get("status") == "COMPLETED":
        mark_success(db, ctx["tenant_id"], "web_automation")
    else:
        mark_failure(db, ctx["tenant_id"], "web_automation", "run_failed", "web_automation_run_failed")
    return {"browser_run_id": browser_run_id, "status": out.get("status"), "final_url": out.get("final_url"), "artifacts": artifacts, "extracted": result_small}
