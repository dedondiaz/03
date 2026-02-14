import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from sqlalchemy import text
from .llm.client import LLMClient
from .tools import registry
from .integrations.google.oauth import GoogleNotConnectedError
from .integrations.slack.slack_client import SlackNotConnectedError
from .integrations.jira.jira_client import JiraNotConnectedError
from .integrations.microsoft.oauth import MicrosoftNotConnectedError
from .browser_automation import AutomationPolicyViolation, AutomationRunnerUnavailable, AutomationSessionNotFound
from .usage import check_quota_or_raise, QuotaExceededError, meter_tool_call, meter_llm_tokens
from .observability import mark_success, mark_failure

MAX_STEPS = int(os.getenv("RUN_MAX_STEPS", "10"))
TOOL_TIMEOUT_S = int(os.getenv("TOOL_CALL_TIMEOUT_S", "8"))


def _log(db, tenant_id: str, run_id: str, event_type: str, payload: dict):
    db.execute(
        text("INSERT INTO audit_logs (tenant_id, run_id, event_type, payload) VALUES (:tenant_id, :run_id, :event_type, CAST(:payload AS jsonb))"),
        {"tenant_id": tenant_id, "run_id": run_id, "event_type": event_type, "payload": json.dumps(payload)},
    )


def _role_rank(role: str) -> int:
    return {"member": 1, "owner": 2}.get(role, 0)


def execute_run(db, run_id: str, tenant_id: str, user_id: str):
    db.execute(text("SET app.tenant_id = :tenant_id"), {"tenant_id": tenant_id})
    db.execute(text("SET app.user_id = :user_id"), {"user_id": user_id})

    task = db.execute(
        text(
            """
            SELECT t.id, t.title, t.description, t.risk_level, m.role
            FROM tasks t
            JOIN memberships m ON m.tenant_id=t.tenant_id AND m.user_id=:user_id
            JOIN task_runs r ON r.task_id=t.id
            WHERE r.id=:run_id
            """
        ),
        {"run_id": run_id, "user_id": user_id},
    ).fetchone()
    if not task:
        return
    cancel = db.execute(text("SELECT cancel_requested FROM task_runs WHERE id=:id"), {"id": run_id}).fetchone()
    if cancel and cancel.cancel_requested:
        db.execute(text("UPDATE task_runs SET status='CANCELLED', verifier_status='failed', verifier_summary='Run cancelled' WHERE id=:id"), {"id": run_id})
        db.commit()
        return

    subject_row = db.execute(text("SELECT subject_email FROM google_oauth_credentials WHERE provider='google' LIMIT 1")).fetchone()
    subject_email = subject_row.subject_email if subject_row else None

    llm = LLMClient()
    planner_est = max(1, (len(task.title or "") + len(task.description or "")) // 4)
    try:
        check_quota_or_raise(db, tenant_id, "llm_tokens", planner_est)
    except QuotaExceededError as exc:
        _log(db, tenant_id, run_id, "quota_exceeded", exc.as_dict())
        db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary='Quota exceeded' WHERE id=:id"), {"id": run_id})
        db.commit()
        return
    plan = llm.planner({"id": str(task.id), "title": task.title, "description": task.description, "risk_level": task.risk_level}, registry.llm_tools())
    meter_llm_tokens(db, tenant_id, planner_est)
    db.execute(text("UPDATE task_runs SET plan_json=CAST(:plan AS jsonb), status='RUNNING' WHERE id=:id"), {"plan": json.dumps(plan), "id": run_id})
    _log(db, tenant_id, run_id, "planner", plan)

    outputs = []
    integration_seen: set[str] = set()
    for idx, item in enumerate(plan.get("tool_calls", [])[:MAX_STEPS]):
        latest = db.execute(text("SELECT cancel_requested, status FROM task_runs WHERE id=:id"), {"id": run_id}).fetchone()
        if latest and latest.cancel_requested:
            db.execute(text("UPDATE task_runs SET status='CANCELLED', verifier_status='failed', verifier_summary='Run cancelled' WHERE id=:id"), {"id": run_id})
            _log(db, tenant_id, run_id, "run_cancelled", {"at_step": idx + 1})
            db.commit()
            return

        tool = registry.get(item["tool"])
        args = item.get("args", {})
        safe_args = tool.redact_args(args) if tool.redact_args else args

        if _role_rank(task.role) < _role_rank(tool.min_role):
            raise PermissionError(f"Role {task.role} cannot run {tool.name}")

        effective_risk = tool.risk_evaluator(args, {"db": db, "tenant_id": tenant_id, "user_id": user_id, "subject_email": subject_email, "run_id": run_id}) if tool.risk_evaluator else tool.risk_level

        if effective_risk == "HIGH":
            approved = db.execute(
                text("SELECT 1 FROM approvals WHERE run_id=:run_id AND tenant_id=:tenant_id AND approved=TRUE"),
                {"run_id": run_id, "tenant_id": tenant_id},
            ).fetchone()
            if not approved:
                _log(db, tenant_id, run_id, "approval_required", {"tool": tool.name, "risk": "HIGH"})
                db.execute(text("UPDATE task_runs SET status='PENDING_APPROVAL' WHERE id=:id"), {"id": run_id})
                db.commit()
                return

        idem = None
        if tool.idempotent:
            if tool.idempotency_builder:
                idem = tool.idempotency_builder(run_id, args)
            else:
                idem_src = f"{run_id}:{tool.name}:{json.dumps(args, sort_keys=True)}"
                idem = hashlib.sha256(idem_src.encode()).hexdigest()
            existing = db.execute(
                text("SELECT result_json FROM tool_invocations WHERE run_id=:run_id AND tenant_id=:tenant_id AND idempotency_key=:k AND status='SUCCESS'"),
                {"run_id": run_id, "tenant_id": tenant_id, "k": idem},
            ).fetchone()
            if existing:
                outputs.append({"tool": tool.name, "result": existing.result_json, "deduped": True})
                _log(db, tenant_id, run_id, "tool_deduped", {"tool": tool.name, "idempotency_key": idem})
                continue

        try:
            check_quota_or_raise(db, tenant_id, "tool_calls", 1)
        except QuotaExceededError as exc:
            _log(db, tenant_id, run_id, "quota_exceeded", exc.as_dict())
            db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary=:summary WHERE id=:id"), {"id": run_id, "summary": "Quota exceeded"})
            db.commit()
            return

        inv = db.execute(
            text(
                """
                INSERT INTO tool_invocations (tenant_id, run_id, tool_name, args_json, status, started_at, idempotency_key)
                VALUES (:tenant_id, :run_id, :tool_name, CAST(:args_json AS jsonb), 'RUNNING', :started_at, :idempotency_key)
                RETURNING id
                """
            ),
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "tool_name": tool.name,
                "args_json": json.dumps(safe_args),
                "started_at": datetime.now(timezone.utc),
                "idempotency_key": idem,
            },
        ).fetchone()
        _log(db, tenant_id, run_id, "tool_call", {"step": idx + 1, "tool": tool.name, "args": safe_args, "risk": effective_risk})

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(tool.handler, args, {"db": db, "tenant_id": tenant_id, "user_id": user_id, "subject_email": subject_email, "run_id": run_id})
                result = fut.result(timeout=TOOL_TIMEOUT_S)
            ext_type = None
            ext_id = None
            if isinstance(result, dict):
                if result.get('event_id') and tool.name.startswith('ms_calendar_'):
                    ext_type, ext_id = 'ms_calendar_event', str(result.get('event_id'))
                elif result.get('message_id') and tool.name.startswith('ms_mail_'):
                    ext_type, ext_id = 'ms_mail_message', str(result.get('message_id'))
                elif result.get('event_id'):
                    ext_type, ext_id = 'calendar_event', str(result.get('event_id'))
                elif result.get('message_id'):
                    ext_type, ext_id = 'gmail_message', str(result.get('message_id'))
                elif result.get('note_id'):
                    ext_type, ext_id = 'note', str(result.get('note_id'))
                elif result.get('channel_id') and result.get('ts'):
                    ext_type, ext_id = 'slack_message', f"{result.get('channel_id')}:{result.get('ts')}"
                elif result.get('issue_key') and result.get('comment_id'):
                    ext_type, ext_id = 'jira_comment', f"{result.get('issue_key')}:{result.get('comment_id')}"
                elif result.get('issue_key'):
                    ext_type, ext_id = 'jira_issue', str(result.get('issue_key'))
            persisted_result = tool.redact_result(result) if (tool.redact_result and isinstance(result, dict)) else result
            db.execute(
                text("UPDATE tool_invocations SET status='SUCCESS', result_json=CAST(:result AS jsonb), external_ref_type=:ext_type, external_ref_id=:ext_id, finished_at=now() WHERE id=:id"),
                {"id": str(inv.id), "result": json.dumps(persisted_result), "ext_type": ext_type, "ext_id": ext_id},
            )
            _log(db, tenant_id, run_id, "tool_output", {"tool": tool.name, "result": persisted_result})
            meter_tool_call(db, tenant_id, tool.name, 1)
            integ = ("google" if tool.name.startswith("gmail") or tool.name.startswith("calendar") else
                     "slack" if tool.name.startswith("slack_") else
                     "jira" if tool.name.startswith("jira_") else
                     "notion" if tool.name.startswith("notion_") else
                     "microsoft" if tool.name.startswith("ms_") else
                     "web_automation" if tool.name.startswith("web_automation") else None)
            if integ and integ not in integration_seen:
                mark_success(db, tenant_id, integ)
                integration_seen.add(integ)
            outputs.append({"tool": tool.name, "result": result})
        except AutomationPolicyViolation as exc:
            msg = str(exc)
            db.execute(text("UPDATE tool_invocations SET status='FAILED', error_text=:error, finished_at=now() WHERE id=:id"), {"id": str(inv.id), "error": msg})
            _log(db, tenant_id, run_id, "error", {"tool": tool.name, "message": msg})
            db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary=:summary WHERE id=:id"), {"id": run_id, "summary": "Automation blocked by policy"})
            db.commit()
            return
        except AutomationSessionNotFound:
            msg = "automation_session_not_found"
            db.execute(text("UPDATE tool_invocations SET status='FAILED', error_text=:error, finished_at=now() WHERE id=:id"), {"id": str(inv.id), "error": msg})
            _log(db, tenant_id, run_id, "error", {"tool": tool.name, "message": msg})
            db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary=:summary WHERE id=:id"), {"id": run_id, "summary": "Automation session missing"})
            db.commit()
            return
        except AutomationRunnerUnavailable:
            msg = "automation_runner_unavailable"
            db.execute(text("UPDATE tool_invocations SET status='FAILED', error_text=:error, finished_at=now() WHERE id=:id"), {"id": str(inv.id), "error": msg})
            _log(db, tenant_id, run_id, "error", {"tool": tool.name, "message": msg})
            db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary=:summary WHERE id=:id"), {"id": run_id, "summary": "Automation runner unavailable"})
            db.commit()
            return

        except GoogleNotConnectedError:
            msg = "google_not_connected"
            if "google" not in integration_seen:
                mark_failure(db, tenant_id, "google", "not_connected", "google_not_connected")
                integration_seen.add("google")
            db.execute(text("UPDATE tool_invocations SET status='FAILED', error_text=:error, finished_at=now() WHERE id=:id"), {"id": str(inv.id), "error": msg})
            _log(db, tenant_id, run_id, "error", {"tool": tool.name, "message": msg})
            db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary=:summary WHERE id=:id"), {"id": run_id, "summary": "Google is not connected for this tenant"})
            db.commit()
            return
        except SlackNotConnectedError:
            msg = "slack_not_connected"
            if "slack" not in integration_seen:
                mark_failure(db, tenant_id, "slack", "not_connected", "slack_not_connected")
                integration_seen.add("slack")
            db.execute(text("UPDATE tool_invocations SET status='FAILED', error_text=:error, finished_at=now() WHERE id=:id"), {"id": str(inv.id), "error": msg})
            _log(db, tenant_id, run_id, "error", {"tool": tool.name, "message": msg})
            db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary=:summary WHERE id=:id"), {"id": run_id, "summary": "Slack is not connected for this tenant"})
            db.commit()
            return
        except JiraNotConnectedError:
            msg = "jira_not_connected"
            if "jira" not in integration_seen:
                mark_failure(db, tenant_id, "jira", "not_connected", "jira_not_connected")
                integration_seen.add("jira")
            db.execute(text("UPDATE tool_invocations SET status='FAILED', error_text=:error, finished_at=now() WHERE id=:id"), {"id": str(inv.id), "error": msg})
            _log(db, tenant_id, run_id, "error", {"tool": tool.name, "message": msg})
            db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary=:summary WHERE id=:id"), {"id": run_id, "summary": "Jira is not connected for this tenant"})
            db.commit()
            return
        except MicrosoftNotConnectedError:
            msg = "microsoft_not_connected"
            if "microsoft" not in integration_seen:
                mark_failure(db, tenant_id, "microsoft", "not_connected", "microsoft_not_connected")
                integration_seen.add("microsoft")
            db.execute(text("UPDATE tool_invocations SET status='FAILED', error_text=:error, finished_at=now() WHERE id=:id"), {"id": str(inv.id), "error": msg})
            _log(db, tenant_id, run_id, "error", {"tool": tool.name, "message": msg})
            db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary=:summary WHERE id=:id"), {"id": run_id, "summary": "Microsoft is not connected for this tenant"})
            db.commit()
            return
        except FuturesTimeout:
            db.execute(text("UPDATE tool_invocations SET status='FAILED', error_text=:error, finished_at=now() WHERE id=:id"), {"id": str(inv.id), "error": f"timeout after {TOOL_TIMEOUT_S}s"})
            _log(db, tenant_id, run_id, "error", {"tool": tool.name, "message": f"timeout after {TOOL_TIMEOUT_S}s"})
            db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary=:summary WHERE id=:id"), {"id": run_id, "summary": "Execution failed: timeout"})
            db.commit()
            return
        except Exception as exc:
            db.execute(text("UPDATE tool_invocations SET status='FAILED', error_text=:error, finished_at=now() WHERE id=:id"), {"id": str(inv.id), "error": str(exc)[:200]})
            _log(db, tenant_id, run_id, "error", {"tool": tool.name, "message": str(exc)[:200]})
            db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary=:summary WHERE id=:id"), {"id": run_id, "summary": "Execution failed"})
            db.commit()
            return

    verify_est = max(1, (len(task.title or "") + len(task.description or "") + len(json.dumps(outputs))) // 4)
    try:
        check_quota_or_raise(db, tenant_id, "llm_tokens", verify_est)
    except QuotaExceededError as exc:
        _log(db, tenant_id, run_id, "quota_exceeded", exc.as_dict())
        db.execute(text("UPDATE task_runs SET status='FAILED', verifier_status='failed', verifier_summary='Quota exceeded' WHERE id=:id"), {"id": run_id})
        db.commit()
        return
    verify = llm.verifier({"title": task.title, "description": task.description}, outputs)
    meter_llm_tokens(db, tenant_id, verify_est)
    rchk = db.execute(text("SELECT cancel_requested FROM task_runs WHERE id=:id"), {"id": run_id}).fetchone()
    final_status = "CANCELLED" if (rchk and rchk.cancel_requested) else ("COMPLETED" if verify.get("status") == "success" else "FAILED")
    db.execute(text("UPDATE task_runs SET status=:status, verifier_status=:v_status, verifier_summary=:summary WHERE id=:id"), {"status": final_status, "v_status": verify.get("status"), "summary": verify.get("summary"), "id": run_id})
    _log(db, tenant_id, run_id, "verifier", verify)
    db.commit()
