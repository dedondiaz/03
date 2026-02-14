import json
import csv
import io
import hashlib
import secrets
from datetime import timedelta
import os
from datetime import datetime, timezone
from redis import Redis
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import text
from .db import init_db, SessionLocal
from .auth import db_dep, login, require_session, switch_tenant
from .schemas import LoginRequest, LoginResponse, TenantSwitchRequest, TaskCreate, TaskOut, RunTaskRequest, RunDetail, SearchResponse
from .worker import queue, execute_run_job, enqueue_run_job, dlq
from .tools import registry
from .integrations.google.oauth import create_connect_url, callback_store_tokens, get_google_status, disconnect_google
from .integrations.slack.oauth import slack_connect_url, slack_callback, slack_status, slack_disconnect
from .integrations.jira.oauth import jira_connect_url, jira_callback, jira_status, jira_disconnect
from .integrations.notion.oauth import notion_connect_url, notion_callback, notion_status, notion_disconnect
from .integrations.microsoft.oauth import microsoft_connect_url, microsoft_callback, microsoft_status, microsoft_disconnect
from .search import SearchService
from .workflows import WorkflowService, WorkflowValidationError
from .automations import AutomationService, AutomationValidationError
from .usage import check_quota_or_raise, QuotaExceededError, meter_api_request, get_limits
from .browser_automation import get_policy as get_automation_policy, upsert_policy as upsert_automation_policy, create_session as create_automation_session, list_sessions as list_automation_sessions, delete_session as delete_automation_session, ARTIFACTS_ROOT
from .observability import mark_connected

app = FastAPI(title="Multi-tenant AI assistant platform")
redis_conn = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


def _minute_key(tenant_id: str) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    key = f"rl:{tenant_id}:{now.strftime('%Y-%m-%d-%H-%M')}"
    retry_after = 60 - now.second
    return key, retry_after


def _require_owner(db, user_id: str, tenant_id: str):
    role = db.execute(text("SELECT role FROM memberships WHERE user_id=:user_id AND tenant_id=:tenant_id"), {"user_id": user_id, "tenant_id": tenant_id}).fetchone()
    if not role or role.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required")


def _require_admin(db, user_id: str, tenant_id: str):
    role = db.execute(text("SELECT role FROM memberships WHERE user_id=:user_id AND tenant_id=:tenant_id"), {"user_id": user_id, "tenant_id": tenant_id}).fetchone()
    if not role or role.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Admin role required")
    return role.role


@app.on_event("startup")
def startup():
    init_db()




@app.middleware("http")
async def tenant_rate_limit_and_meter(request: Request, call_next):
    if os.getenv("TESTING") == "1":
        return await call_next(request)
    if request.url.path.startswith("/health") or request.url.path.startswith("/auth/login"):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return await call_next(request)
    token = auth.split(" ", 1)[1].strip()
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT tenant_id FROM sessions WHERE token=:token"), {"token": token}).fetchone()
        if not row:
            return await call_next(request)
        tenant_id = str(row.tenant_id)
        check_quota_or_raise(db, tenant_id, "api_requests", 1)
        key, retry_after = _minute_key(tenant_id)
        count = redis_conn.incr(key)
        if count == 1:
            redis_conn.expire(key, retry_after)
        limit = int(get_limits(db, tenant_id).get("api_requests_per_minute", 120))
        if count > limit:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=429, content={"error": "quota_exceeded", "kind": "api_requests", "retry_after": retry_after}, headers={"Retry-After": str(retry_after)})
        meter_api_request(db, tenant_id, 1)
        db.commit()
    except QuotaExceededError as exc:
        from fastapi.responses import JSONResponse
        db.rollback()
        return JSONResponse(status_code=429, content=exc.as_dict())
    except Exception:
        db.rollback()
    finally:
        db.close()
    return await call_next(request)

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/auth/login", response_model=LoginResponse)
def auth_login(body: LoginRequest, db=Depends(db_dep)):
    return login(db, body.email, body.password)


@app.post("/tenants/switch", response_model=LoginResponse)
def tenant_switch(body: TenantSwitchRequest, session=Depends(require_session)):
    return switch_tenant(session["db"], session["token"], session["user_id"], body.tenant_id)


@app.get("/tenants/me")
def my_tenants(session=Depends(require_session)):
    db = session["db"]
    rows = db.execute(text("SELECT tenant_id, role FROM memberships WHERE user_id=:user_id"), {"user_id": session["user_id"]}).fetchall()
    return [{"tenant_id": str(r.tenant_id), "role": r.role, "active": str(r.tenant_id) == session["tenant_id"]} for r in rows]




@app.get("/tenant/members")
def tenant_members(session=Depends(require_session)):
    rows = session["db"].execute(text("""
      SELECT u.id, u.email, m.role
      FROM memberships m JOIN users u ON u.id=m.user_id
      WHERE m.tenant_id=:tenant_id
      ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, u.email
    """), {"tenant_id": session["tenant_id"]}).fetchall()
    return [{"user_id": str(r.id), "email": r.email, "role": r.role} for r in rows]


@app.post("/tenant/members/invite")
def invite_member(body: dict, session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    email = (body.get("email") or "").strip().lower()
    role = (body.get("role") or "member").strip().lower()
    if role not in {"admin", "member"}:
        raise HTTPException(status_code=400, detail="Invalid role")
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    exp = datetime.now(timezone.utc) + timedelta(days=7)
    db.execute(text("""
      INSERT INTO tenant_invites (tenant_id, email, role, token_hash, expires_at, created_by)
      VALUES (:tenant_id, :email, :role, :token_hash, :expires_at, :created_by)
    """), {"tenant_id": session["tenant_id"], "email": email, "role": role, "token_hash": token_hash, "expires_at": exp, "created_by": session["user_id"]})
    db.commit()
    return {"invite_token": token, "invite_link": f"/accept-invite?token={token}", "expires_at": exp.isoformat()}


@app.post("/tenant/members/accept")
def accept_member_invite(body: dict, session=Depends(require_session)):
    db = session["db"]
    token = body.get("invite_token") or ""
    h = hashlib.sha256(token.encode()).hexdigest()
    inv = db.execute(text("""
      SELECT tenant_id, email, role, expires_at, accepted_at
      FROM tenant_invites
      WHERE token_hash=:h
    """), {"h": h}).fetchone()
    if not inv or inv.accepted_at is not None or inv.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invalid or expired invite")
    me = db.execute(text("SELECT email FROM users WHERE id=:id"), {"id": session["user_id"]}).fetchone()
    if not me or me.email.lower() != inv.email.lower():
        raise HTTPException(status_code=403, detail="Invite email mismatch")
    db.execute(text("""
      INSERT INTO memberships (user_id, tenant_id, role)
      VALUES (:user_id, :tenant_id, :role)
      ON CONFLICT (user_id, tenant_id) DO UPDATE SET role=EXCLUDED.role
    """), {"user_id": session["user_id"], "tenant_id": str(inv.tenant_id), "role": inv.role})
    db.execute(text("UPDATE tenant_invites SET accepted_at=now() WHERE token_hash=:h"), {"h": h})
    db.commit()
    return {"accepted": True, "tenant_id": str(inv.tenant_id), "role": inv.role}


@app.put("/tenant/members/{user_id}")
def update_member_role(user_id: str, body: dict, session=Depends(require_session)):
    db = session["db"]
    actor_role = _require_admin(db, session["user_id"], session["tenant_id"])
    new_role = (body.get("role") or "member").strip().lower()
    if new_role not in {"admin", "member", "owner"}:
        raise HTTPException(status_code=400, detail="Invalid role")
    target = db.execute(text("SELECT role FROM memberships WHERE tenant_id=:tenant_id AND user_id=:user_id"), {"tenant_id": session["tenant_id"], "user_id": user_id}).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")
    if actor_role != "owner" and (new_role in {"admin", "owner"} or target.role == "owner"):
        raise HTTPException(status_code=403, detail="Owner role required")
    db.execute(text("UPDATE memberships SET role=:role WHERE tenant_id=:tenant_id AND user_id=:user_id"), {"role": new_role, "tenant_id": session["tenant_id"], "user_id": user_id})
    db.commit()
    return {"user_id": user_id, "role": new_role}


@app.delete("/tenant/members/{user_id}")
def remove_member(user_id: str, session=Depends(require_session)):
    db = session["db"]
    actor_role = _require_admin(db, session["user_id"], session["tenant_id"])
    target = db.execute(text("SELECT role FROM memberships WHERE tenant_id=:tenant_id AND user_id=:user_id"), {"tenant_id": session["tenant_id"], "user_id": user_id}).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="Member not found")
    if target.role == "owner":
        owners = db.execute(text("SELECT count(*) FROM memberships WHERE tenant_id=:tenant_id AND role='owner'"), {"tenant_id": session["tenant_id"]}).scalar() or 0
        if int(owners) <= 1:
            raise HTTPException(status_code=400, detail="Cannot remove last owner")
        if actor_role != "owner":
            raise HTTPException(status_code=403, detail="Owner role required")
    db.execute(text("DELETE FROM memberships WHERE tenant_id=:tenant_id AND user_id=:user_id"), {"tenant_id": session["tenant_id"], "user_id": user_id})
    db.commit()
    return {"removed": True}

@app.get("/tenant/policy")
def get_policy(session=Depends(require_session)):
    db = session["db"]
    row = db.execute(text("SELECT allowed_email_domains FROM tenant_policies WHERE tenant_id=:tenant_id"), {"tenant_id": session["tenant_id"]}).fetchone()
    return {"allowed_email_domains": (row.allowed_email_domains if row else [])}


@app.put("/tenant/policy")
def put_policy(body: dict, session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    allowed = [d.lower().strip() for d in body.get("allowed_email_domains", []) if d]
    db.execute(
        text(
            """
            INSERT INTO tenant_policies (tenant_id, allowed_email_domains, updated_at)
            VALUES (:tenant_id, :allowed, now())
            ON CONFLICT (tenant_id) DO UPDATE SET allowed_email_domains=:allowed, updated_at=now()
            """
        ),
        {"tenant_id": session["tenant_id"], "allowed": allowed},
    )
    db.commit()
    return {"allowed_email_domains": allowed}




@app.get("/tenant/automation-policy")
def get_tenant_automation_policy(session=Depends(require_session)):
    return get_automation_policy(session["db"])


@app.put("/tenant/automation-policy")
def put_tenant_automation_policy(body: dict, session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    out = upsert_automation_policy(db, session["tenant_id"], body)
    db.commit()
    return out


@app.get("/automation/sessions")
def get_automation_sessions(session=Depends(require_session)):
    return {"sessions": list_automation_sessions(session["db"])}


@app.post("/automation/sessions")
def post_automation_sessions(body: dict, session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    sid = create_automation_session(db, session["tenant_id"], session["user_id"], body.get("domain", ""), body.get("storage_state", {}))
    db.commit()
    return {"id": sid}


@app.delete("/automation/sessions/{session_id}")
def delete_automation_session_route(session_id: str, session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    delete_automation_session(db, session_id)
    db.commit()
    return {"deleted": True}


@app.get("/automation/runs/{run_id}")
def get_automation_run(run_id: str, session=Depends(require_session)):
    row = session["db"].execute(text("SELECT id, status, final_url, result_json, errors, created_at FROM browser_automation_runs WHERE id=:id"), {"id": run_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return {"id": str(row.id), "status": row.status, "final_url": row.final_url, "result": row.result_json, "errors": row.errors, "created_at": row.created_at.isoformat()}


@app.get("/automation/runs/{run_id}/artifacts")
def get_automation_run_artifacts(run_id: str, session=Depends(require_session)):
    rows = session["db"].execute(text("SELECT id, kind, step_index, mime_type, byte_size, created_at FROM browser_automation_artifacts WHERE browser_run_id=:id ORDER BY created_at"), {"id": run_id}).fetchall()
    return {"artifacts": [{"id": str(r.id), "kind": r.kind, "step_index": r.step_index, "mime_type": r.mime_type, "byte_size": r.byte_size, "created_at": r.created_at.isoformat()} for r in rows]}


@app.get("/automation/artifacts/{artifact_id}/download")
def download_automation_artifact(artifact_id: str, session=Depends(require_session)):
    row = session["db"].execute(text("SELECT file_path, mime_type FROM browser_automation_artifacts WHERE id=:id"), {"id": artifact_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    path = (ARTIFACTS_ROOT / row.file_path).resolve()
    if not path.exists() or not str(path).startswith(str(ARTIFACTS_ROOT.resolve())):
        raise HTTPException(status_code=404, detail="Artifact missing")
    return FileResponse(str(path), media_type=row.mime_type)


@app.get("/tenant/calendar-settings")
def get_calendar_settings(session=Depends(require_session)):
    db = session["db"]
    row = db.execute(text("SELECT timezone, work_start, work_end, work_days, slot_granularity_minutes, meeting_buffer_minutes, default_calendar_id FROM tenant_calendar_settings WHERE tenant_id=:tenant_id"), {"tenant_id": session["tenant_id"]}).fetchone()
    if not row:
        return {"timezone": "Asia/Kolkata", "work_start": "10:00", "work_end": "18:00", "work_days": [1,2,3,4,5], "slot_granularity_minutes": 15, "meeting_buffer_minutes": 10, "default_calendar_id": "primary"}
    return {
        "timezone": row.timezone,
        "work_start": str(row.work_start),
        "work_end": str(row.work_end),
        "work_days": row.work_days,
        "slot_granularity_minutes": row.slot_granularity_minutes,
        "meeting_buffer_minutes": row.meeting_buffer_minutes,
        "default_calendar_id": row.default_calendar_id,
    }


@app.put("/tenant/calendar-settings")
def put_calendar_settings(body: dict, session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    payload = {
        "tenant_id": session["tenant_id"],
        "timezone": body.get("timezone", "Asia/Kolkata"),
        "work_start": body.get("work_start", "10:00"),
        "work_end": body.get("work_end", "18:00"),
        "work_days": body.get("work_days", [1,2,3,4,5]),
        "slot_granularity_minutes": int(body.get("slot_granularity_minutes", 15)),
        "meeting_buffer_minutes": int(body.get("meeting_buffer_minutes", 10)),
        "default_calendar_id": body.get("default_calendar_id", "primary"),
    }
    db.execute(text("""
        INSERT INTO tenant_calendar_settings (tenant_id, timezone, work_start, work_end, work_days, slot_granularity_minutes, meeting_buffer_minutes, default_calendar_id, updated_at)
        VALUES (:tenant_id, :timezone, :work_start, :work_end, :work_days, :slot_granularity_minutes, :meeting_buffer_minutes, :default_calendar_id, now())
        ON CONFLICT (tenant_id) DO UPDATE SET
          timezone=:timezone,
          work_start=:work_start,
          work_end=:work_end,
          work_days=:work_days,
          slot_granularity_minutes=:slot_granularity_minutes,
          meeting_buffer_minutes=:meeting_buffer_minutes,
          default_calendar_id=:default_calendar_id,
          updated_at=now()
    """), payload)
    db.commit()
    return payload



@app.get("/tenant/slack-policy")
def get_slack_policy(session=Depends(require_session)):
    db = session["db"]
    row = db.execute(text("SELECT allowed_channel_ids, allow_external_shared FROM tenant_slack_policies WHERE tenant_id=:tenant_id"), {"tenant_id": session["tenant_id"]}).fetchone()
    return {"allowed_channel_ids": (row.allowed_channel_ids if row else []), "allow_external_shared": (row.allow_external_shared if row else False)}


@app.put("/tenant/slack-policy")
def put_slack_policy(body: dict, session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    allowed = [c.strip() for c in body.get("allowed_channel_ids", []) if c]
    allow_ext = bool(body.get("allow_external_shared", False))
    db.execute(text("""
      INSERT INTO tenant_slack_policies (tenant_id, allowed_channel_ids, allow_external_shared, updated_at)
      VALUES (:tenant_id, :allowed, :allow_ext, now())
      ON CONFLICT (tenant_id) DO UPDATE SET allowed_channel_ids=:allowed, allow_external_shared=:allow_ext, updated_at=now()
    """), {"tenant_id": session["tenant_id"], "allowed": allowed, "allow_ext": allow_ext})
    db.commit()
    return {"allowed_channel_ids": allowed, "allow_external_shared": allow_ext}




@app.get("/tenant/jira-policy")
def get_jira_policy(session=Depends(require_session)):
    db = session["db"]
    row = db.execute(text("SELECT allowed_project_keys, allow_write FROM tenant_jira_policies WHERE tenant_id=:tenant_id"), {"tenant_id": session["tenant_id"]}).fetchone()
    return {"allowed_project_keys": (row.allowed_project_keys if row else []), "allow_write": (row.allow_write if row else True)}


@app.put("/tenant/jira-policy")
def put_jira_policy(body: dict, session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    keys = [k.strip().upper() for k in body.get("allowed_project_keys", []) if k]
    allow_write = bool(body.get("allow_write", True))
    db.execute(text("""
      INSERT INTO tenant_jira_policies (tenant_id, allowed_project_keys, allow_write, updated_at)
      VALUES (:tenant_id, :keys, :allow_write, now())
      ON CONFLICT (tenant_id) DO UPDATE SET allowed_project_keys=:keys, allow_write=:allow_write, updated_at=now()
    """), {"tenant_id": session["tenant_id"], "keys": keys, "allow_write": allow_write})
    db.commit()
    return {"allowed_project_keys": keys, "allow_write": allow_write}






@app.get("/tenant/policies")
def get_tenant_policies(session=Depends(require_session)):
    db = session["db"]
    email = get_policy(session)
    slack = get_slack_policy(session)
    jira = get_jira_policy(session)
    cal = get_calendar_settings(session)
    auto = get_tenant_automation_policy(session)
    notion = db.execute(text("SELECT allowed_parent_ids FROM tenant_notion_policies WHERE tenant_id=:tenant_id"), {"tenant_id": session["tenant_id"]}).fetchone()
    limits = get_limits(db, session["tenant_id"])
    return {
      "email_domains": email,
      "slack_policy": slack,
      "jira_policy": jira,
      "notion_policy": {"allowed_parent_ids": (notion.allowed_parent_ids if notion else [])},
      "calendar_settings": cal,
      "automation_policy": auto,
      "plan_limits": limits,
    }


@app.put("/tenant/policies")
def put_tenant_policies(body: dict, session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    if body.get("email_domains") is not None:
        put_policy({"allowed_email_domains": body.get("email_domains", {}).get("allowed_email_domains", [])}, session)
    if body.get("slack_policy") is not None:
        put_slack_policy(body.get("slack_policy") or {}, session)
    if body.get("jira_policy") is not None:
        put_jira_policy(body.get("jira_policy") or {}, session)
    if body.get("calendar_settings") is not None:
        put_calendar_settings(body.get("calendar_settings") or {}, session)
    if body.get("automation_policy") is not None:
        put_tenant_automation_policy(body.get("automation_policy") or {}, session)
    if body.get("notion_policy") is not None:
        allowed = [x for x in (body.get("notion_policy") or {}).get("allowed_parent_ids", []) if x]
        db.execute(text("""
          INSERT INTO tenant_notion_policies (tenant_id, allowed_parent_ids, updated_at)
          VALUES (:tenant_id, :allowed, now())
          ON CONFLICT (tenant_id) DO UPDATE SET allowed_parent_ids=:allowed, updated_at=now()
        """), {"tenant_id": session["tenant_id"], "allowed": allowed})
        db.commit()
    return get_tenant_policies(session)

@app.post("/integrations/notion/connect")
def notion_connect(session=Depends(require_session)):
    return {"authorization_url": notion_connect_url(session["db"], session["tenant_id"], session["user_id"])}


@app.get("/integrations/notion/callback")
def notion_oauth_callback(state: str = Query(...), code: str = Query(...), db=Depends(db_dep)):
    try:
        return notion_callback(db, state, code)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Notion OAuth state")


@app.get("/integrations/notion/status")
def get_notion_status(session=Depends(require_session)):
    return notion_status(session["db"])


@app.post("/integrations/notion/disconnect")
def do_notion_disconnect(session=Depends(require_session)):
    out = notion_disconnect(session["db"])
    mark_connected(session["db"], session["tenant_id"], "notion", False)
    session["db"].commit()
    return out


@app.get("/knowledge/notion-docs")
def list_notion_docs(session=Depends(require_session)):
    rows = session["db"].execute(text("SELECT notion_page_id, title, source_url, last_synced_at FROM notion_documents ORDER BY updated_at DESC LIMIT 200")).fetchall()
    return [{"page_id": r.notion_page_id, "title": r.title, "source_url": r.source_url, "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None} for r in rows]

@app.post("/integrations/jira/connect")
def jira_connect(session=Depends(require_session)):
    return {"authorization_url": jira_connect_url(session["db"], session["tenant_id"], session["user_id"], session["token"])}


@app.get("/integrations/jira/callback")
def jira_oauth_callback(state: str = Query(...), code: str = Query(...), db=Depends(db_dep)):
    try:
        return jira_callback(db, state, code)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Jira OAuth state")


@app.get("/integrations/jira/status")
def get_jira_status(session=Depends(require_session)):
    return jira_status(session["db"])


@app.post("/integrations/jira/disconnect")
def do_jira_disconnect(session=Depends(require_session)):
    out = jira_disconnect(session["db"])
    mark_connected(session["db"], session["tenant_id"], "jira", False)
    session["db"].commit()
    return out

@app.post("/integrations/slack/connect")
def slack_connect(session=Depends(require_session)):
    return {"authorization_url": slack_connect_url(session["db"], session["tenant_id"], session["user_id"], session["token"])}


@app.get("/integrations/slack/callback")
def slack_oauth_callback(state: str = Query(...), code: str = Query(...), db=Depends(db_dep)):
    try:
        return slack_callback(db, state, code)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Slack OAuth state")


@app.get("/integrations/slack/status")
def get_slack_status(session=Depends(require_session)):
    return slack_status(session["db"])


@app.post("/integrations/slack/disconnect")
def do_slack_disconnect(session=Depends(require_session)):
    out = slack_disconnect(session["db"])
    mark_connected(session["db"], session["tenant_id"], "slack", False)
    session["db"].commit()
    return out



@app.post("/integrations/microsoft/connect")
def microsoft_connect(session=Depends(require_session)):
    return {"authorization_url": microsoft_connect_url(session["db"], session["tenant_id"], session["user_id"], session["token"])}


@app.get("/integrations/microsoft/callback")
def microsoft_oauth_callback(state: str = Query(...), code: str = Query(...), db=Depends(db_dep)):
    try:
        return microsoft_callback(db, state, code)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Microsoft OAuth state")


@app.get("/integrations/microsoft/status")
def get_microsoft_status(session=Depends(require_session)):
    return microsoft_status(session["db"])


@app.post("/integrations/microsoft/disconnect")
def do_microsoft_disconnect(session=Depends(require_session)):
    out = microsoft_disconnect(session["db"])
    mark_connected(session["db"], session["tenant_id"], "microsoft", False)
    session["db"].execute(text("INSERT INTO audit_logs (tenant_id, event_type, payload) VALUES (:tenant_id, 'microsoft_disconnected', CAST(:payload AS jsonb))"), {"tenant_id": session["tenant_id"], "payload": json.dumps({"connected": False})})
    session["db"].commit()
    return out

@app.post("/integrations/google/connect")
def google_connect(session=Depends(require_session)):
    url = create_connect_url(session["db"], session["tenant_id"], session["user_id"])
    return {"authorization_url": url}


@app.get("/integrations/google/callback")
def google_callback(state: str = Query(...), code: str = Query(...), db=Depends(db_dep)):
    try:
        return callback_store_tokens(db, state, code)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")


@app.get("/integrations/google/status")
def google_status(session=Depends(require_session)):
    return get_google_status(session["db"])


@app.post("/integrations/google/disconnect")
def google_disconnect(session=Depends(require_session)):
    disconnect_google(session["db"])
    mark_connected(session["db"], session["tenant_id"], "google", False)
    session["db"].commit()
    return {"connected": False}




@app.get("/search", response_model=SearchResponse)
def unified_search_api(q: str = Query(..., min_length=1), sources: str | None = Query(default=None), limit: int = Query(default=20, ge=1, le=50), session=Depends(require_session)):
    source_list = [s.strip() for s in sources.split(",")] if sources else None
    service = SearchService(session["db"], session["tenant_id"])
    return service.search(query=q, sources=source_list, limit=limit)


@app.get("/search/sources")
def search_sources(session=Depends(require_session)):
    service = SearchService(session["db"], session["tenant_id"])
    return service.source_status()



@app.get("/workflows/templates")
def list_workflow_templates(session=Depends(require_session)):
    svc = WorkflowService(session["db"], session["tenant_id"], session["user_id"])
    svc.seed_templates()
    session["db"].commit()
    return svc.list_templates()


@app.post("/workflows/runs")
def create_workflow_run(body: dict, session=Depends(require_session)):
    db = session["db"]
    svc = WorkflowService(db, session["tenant_id"], session["user_id"])
    try:
        wf = svc.create_run(str(body.get("template_id", "")), body.get("input") or {})
    except WorkflowValidationError as exc:
        detail = str(exc)
        if "quota_exceeded" in detail:
            raise HTTPException(status_code=429, detail=detail)
        raise HTTPException(status_code=400, detail=detail)
    db.execute(text("INSERT INTO audit_logs (tenant_id, run_id, event_type, payload) VALUES (:tenant_id, :run_id, 'workflow_run_created', CAST(:payload AS jsonb))"), {
        "tenant_id": session["tenant_id"],
        "run_id": wf["linked_run_id"],
        "payload": json.dumps({"workflow_run_id": wf["id"], "template_id": wf["template_id"]}),
    })
    db.commit()
    enqueue_run_job(wf["linked_run_id"], session["tenant_id"], session["user_id"], job_id=f"tenant:{session['tenant_id']}:run:{wf['linked_run_id']}")
    wf = svc.refresh_status(wf["id"])
    db.commit()
    return wf


@app.get("/workflows/runs")
def list_workflow_runs(session=Depends(require_session)):
    svc = WorkflowService(session["db"], session["tenant_id"], session["user_id"])
    out = svc.list_runs()
    session["db"].commit()
    return out


@app.get("/workflows/runs/{workflow_run_id}")
def get_workflow_run(workflow_run_id: str, session=Depends(require_session)):
    svc = WorkflowService(session["db"], session["tenant_id"], session["user_id"])
    try:
        wf = svc.refresh_status(workflow_run_id)
    except WorkflowValidationError:
        raise HTTPException(status_code=404, detail="Workflow run not found")

    linked = None
    if wf.get("linked_run_id"):
        run = session["db"].execute(text("SELECT id, status, plan_json, verifier_status, verifier_summary FROM task_runs WHERE id=:id"), {"id": wf["linked_run_id"]}).fetchone()
        if run:
            invocations = session["db"].execute(text("SELECT tool_name, args_json, result_json, status, started_at, finished_at, idempotency_key, error_text, external_ref_type, external_ref_id FROM tool_invocations WHERE run_id=:run_id ORDER BY started_at"), {"run_id": wf["linked_run_id"]}).fetchall()
            linked = {
                "run_id": str(run.id),
                "status": run.status,
                "plan": run.plan_json,
                "tool_invocations": [{"tool_name": row.tool_name, "args": row.args_json, "result": row.result_json, "status": row.status, "started_at": row.started_at.isoformat() if row.started_at else None, "finished_at": row.finished_at.isoformat() if row.finished_at else None, "idempotency_key": row.idempotency_key, "error": row.error_text, "external_ref_type": row.external_ref_type, "external_ref_id": row.external_ref_id} for row in invocations],
                "verifier": {"status": run.verifier_status, "summary": run.verifier_summary},
            }
    session["db"].commit()
    return {"workflow_run": wf, "linked_run": linked}



@app.get("/automations/rules")
def list_automation_rules(session=Depends(require_session)):
    svc = AutomationService(session["db"], session["tenant_id"], session["user_id"])
    return svc.list_rules()


@app.post("/automations/rules")
def create_automation_rule(body: dict, session=Depends(require_session)):
    svc = AutomationService(session["db"], session["tenant_id"], session["user_id"])
    try:
        out = svc.create_rule(body)
    except AutomationValidationError as exc:
        msg = str(exc)
        code = 403 if msg == "Owner role required" else 400
        raise HTTPException(status_code=code, detail=msg)
    session["db"].commit()
    return out


@app.get("/automations/rules/{rule_id}")
def get_automation_rule(rule_id: str, session=Depends(require_session)):
    svc = AutomationService(session["db"], session["tenant_id"], session["user_id"])
    try:
        return svc.get_rule(rule_id)
    except AutomationValidationError:
        raise HTTPException(status_code=404, detail="Automation rule not found")


@app.put("/automations/rules/{rule_id}")
def update_automation_rule(rule_id: str, body: dict, session=Depends(require_session)):
    svc = AutomationService(session["db"], session["tenant_id"], session["user_id"])
    try:
        out = svc.update_rule(rule_id, body)
    except AutomationValidationError as exc:
        msg = str(exc)
        code = 403 if msg == "Owner role required" else 400
        raise HTTPException(status_code=code, detail=msg)
    session["db"].commit()
    return out


@app.post("/automations/rules/{rule_id}/toggle")
def toggle_automation_rule(rule_id: str, body: dict, session=Depends(require_session)):
    svc = AutomationService(session["db"], session["tenant_id"], session["user_id"])
    try:
        out = svc.toggle_rule(rule_id, bool(body.get("enabled", False)))
    except AutomationValidationError as exc:
        msg = str(exc)
        code = 403 if msg == "Owner role required" else 400
        raise HTTPException(status_code=code, detail=msg)
    session["db"].commit()
    return out


@app.get("/automations/executions")
def list_automation_executions(rule_id: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=200), session=Depends(require_session)):
    svc = AutomationService(session["db"], session["tenant_id"], session["user_id"])
    return svc.list_executions(rule_id=rule_id, limit=limit)





@app.get("/audit/list")
def audit_list(from_date: str | None = Query(default=None, alias="from"), to_date: str | None = Query(default=None, alias="to"), tool_name: str | None = Query(default=None), status: str | None = Query(default=None), limit: int = Query(default=200, ge=1, le=5000), session=Depends(require_session)):
    rows = session["db"].execute(text("""
      SELECT id, run_id, tool_name, status, args_json, result_json, error_text, started_at, finished_at
      FROM tool_invocations
      WHERE (:tool_name IS NULL OR tool_name=:tool_name)
        AND (:status IS NULL OR status=:status)
        AND (:from_date IS NULL OR started_at >= CAST(:from_date AS date))
        AND (:to_date IS NULL OR started_at < (CAST(:to_date AS date) + interval '1 day'))
      ORDER BY started_at DESC
      LIMIT :limit
    """), {"tool_name": tool_name, "status": status, "from_date": from_date, "to_date": to_date, "limit": int(limit)}).fetchall()
    return [{"id": str(r.id), "run_id": str(r.run_id), "tool_name": r.tool_name, "status": r.status, "error": (r.error_text or "")[:180], "started_at": r.started_at.isoformat() if r.started_at else None, "finished_at": r.finished_at.isoformat() if r.finished_at else None} for r in rows]


@app.get("/audit/export")
def audit_export(from_date: str | None = Query(default=None, alias="from"), to_date: str | None = Query(default=None, alias="to"), tool_name: str | None = Query(default=None), status: str | None = Query(default=None), limit: int = Query(default=5000, ge=1, le=50000), session=Depends(require_session)):
    rows = audit_list(from_date=from_date, to_date=to_date, tool_name=tool_name, status=status, limit=limit, session=session)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["id", "run_id", "tool_name", "status", "error", "started_at", "finished_at"])
    w.writeheader()
    for r in rows:
        w.writerow(r)
    out = io.BytesIO(buf.getvalue().encode("utf-8"))
    headers = {"Content-Disposition": "attachment; filename=audit_export.csv"}
    return StreamingResponse(out, media_type="text/csv", headers=headers)


@app.get("/tenant/usage/summary")
def tenant_usage_summary(days: int = Query(default=7, ge=1, le=30), session=Depends(require_session)):
    db = session["db"]
    limits = get_limits(db, session["tenant_id"])
    rows = db.execute(text("""
      SELECT day, api_requests_count, tool_calls_count, llm_tokens_count, workflow_runs_count, automation_runs_count, web_automation_runs_count, web_automation_runtime_seconds
      FROM tenant_usage_daily
      WHERE day >= current_date - (CAST(:days AS int) - 1)
      ORDER BY day ASC
    """), {"days": int(days)}).fetchall()
    daily = [{"day": str(r.day), "api_requests_count": int(r.api_requests_count), "tool_calls_count": int(r.tool_calls_count), "llm_tokens_count": int(r.llm_tokens_count), "workflow_runs_count": int(r.workflow_runs_count), "automation_runs_count": int(r.automation_runs_count), "web_automation_runs_count": int(r.web_automation_runs_count), "web_automation_runtime_seconds": int(r.web_automation_runtime_seconds)} for r in rows]
    today = daily[-1] if daily else {"api_requests_count":0,"tool_calls_count":0,"llm_tokens_count":0,"workflow_runs_count":0,"automation_runs_count":0,"web_automation_runs_count":0,"web_automation_runtime_seconds":0}
    pct = {
      "api_requests": (today["api_requests_count"] / max(1, int(limits.get("api_requests_per_minute", 120)))) * 100.0,
      "tool_calls": (today["tool_calls_count"] / max(1, int(limits.get("tool_calls_per_day", 200)))) * 100.0,
      "llm_tokens": (today["llm_tokens_count"] / max(1, int(limits.get("llm_tokens_per_day", 200000)))) * 100.0,
      "workflow_runs": (today["workflow_runs_count"] / max(1, int(limits.get("workflow_runs_per_day", 100)))) * 100.0,
      "automation_runs": (today["automation_runs_count"] / max(1, int(limits.get("automation_runs_per_day", 100)))) * 100.0,
      "web_automation_runs": (today["web_automation_runs_count"] / max(1, int(limits.get("web_automation_runs_per_day", 30)))) * 100.0,
      "web_automation_runtime_seconds": (today["web_automation_runtime_seconds"] / max(1, int(limits.get("web_automation_runtime_seconds_per_day", 1200)))) * 100.0,
    }
    return {"limits_json": limits, "daily": daily, "percent_used_today": pct}

@app.get("/ops/metrics/summary")
def ops_metrics_summary(session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    out = {
        "runs_last_24h": {},
        "runs_last_7d": {},
        "top_failing_tools_last_24h": [],
        "automation_outcomes_last_24h": {},
        "web_automation_outcomes_last_24h": {},
        "queue": {"runs": queue.count, "dlq": dlq.count},
    }
    rows = db.execute(text("SELECT status, count(*) c FROM task_runs WHERE created_at >= now()-interval '24 hours' GROUP BY status")).fetchall()
    out["runs_last_24h"] = {r.status: int(r.c) for r in rows}
    rows = db.execute(text("SELECT status, count(*) c FROM task_runs WHERE created_at >= now()-interval '7 days' GROUP BY status")).fetchall()
    out["runs_last_7d"] = {r.status: int(r.c) for r in rows}
    rows = db.execute(text("SELECT tool_name, count(*) c FROM tool_invocations WHERE status='FAILED' AND started_at >= now()-interval '24 hours' GROUP BY tool_name ORDER BY c DESC LIMIT 10")).fetchall()
    out["top_failing_tools_last_24h"] = [{"tool_name": r.tool_name, "count": int(r.c)} for r in rows]
    rows = db.execute(text("SELECT status, count(*) c FROM automation_executions WHERE created_at >= now()-interval '24 hours' GROUP BY status")).fetchall()
    out["automation_outcomes_last_24h"] = {r.status: int(r.c) for r in rows}
    rows = db.execute(text("SELECT status, count(*) c FROM browser_automation_runs WHERE created_at >= now()-interval '24 hours' GROUP BY status")).fetchall()
    out["web_automation_outcomes_last_24h"] = {r.status: int(r.c) for r in rows}
    return out


@app.get("/ops/tenants/health")
def ops_tenant_health(limit: int = Query(default=20, ge=1, le=100), session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    health_rows = db.execute(text("SELECT integration, connected, last_success_at, last_error_at, last_error_code, last_error_message_redacted, consecutive_failures, updated_at FROM integration_health ORDER BY integration")).fetchall()
    usage = db.execute(text("SELECT api_requests_count, tool_calls_count, llm_tokens_count, workflow_runs_count, automation_runs_count, web_automation_runs_count, web_automation_runtime_seconds FROM tenant_usage_daily WHERE day=current_date")).fetchone()
    limits = db.execute(text("SELECT limits_json FROM tenant_plans WHERE tenant_id=:tenant_id"), {"tenant_id": session["tenant_id"]}).fetchone()
    failures = db.execute(text("SELECT id, status, verifier_summary, created_at FROM task_runs WHERE status='FAILED' ORDER BY created_at DESC LIMIT :l"), {"l": limit}).fetchall()
    return {
      "integration_health": [{"integration": r.integration, "connected": r.connected, "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None, "last_error_at": r.last_error_at.isoformat() if r.last_error_at else None, "last_error_code": r.last_error_code, "last_error_message": r.last_error_message_redacted, "consecutive_failures": r.consecutive_failures, "updated_at": r.updated_at.isoformat()} for r in health_rows],
      "usage_today": {
          "usage": {"api_requests_count": int(usage.api_requests_count if usage else 0), "tool_calls_count": int(usage.tool_calls_count if usage else 0), "llm_tokens_count": int(usage.llm_tokens_count if usage else 0), "workflow_runs_count": int(usage.workflow_runs_count if usage else 0), "automation_runs_count": int(usage.automation_runs_count if usage else 0), "web_automation_runs_count": int(usage.web_automation_runs_count if usage else 0), "web_automation_runtime_seconds": int(usage.web_automation_runtime_seconds if usage else 0)},
          "limits": limits.limits_json if limits else {},
      },
      "recent_failures": [{"run_id": str(r.id), "status": r.status, "reason": (r.verifier_summary or "")[:180], "created_at": r.created_at.isoformat()} for r in failures]
    }


@app.get("/ops/runs/recent")
def ops_recent_runs(status: str | None = Query(default=None), limit: int = Query(default=30, ge=1, le=200), session=Depends(require_session)):
    db = session["db"]
    _require_admin(db, session["user_id"], session["tenant_id"])
    rows = db.execute(text("""
      SELECT tr.id, tr.status, tr.created_at, wr.id AS workflow_run_id
      FROM task_runs tr
      LEFT JOIN workflow_runs wr ON wr.linked_run_id=tr.id
      WHERE (:status IS NULL OR tr.status=:status)
      ORDER BY tr.created_at DESC LIMIT :limit
    """), {"status": status, "limit": int(limit)}).fetchall()
    return [{"run_id": str(r.id), "status": r.status, "created_at": r.created_at.isoformat(), "workflow_run_id": str(r.workflow_run_id) if r.workflow_run_id else None} for r in rows]

@app.get("/tools")
def list_tools(session=Depends(require_session)):
    return [{"name": t.name, "description": t.description, "json_schema": t.json_schema, "risk_level": t.risk_level, "idempotent": t.idempotent, "tenant_scoped": t.tenant_scoped} for t in registry.list_specs()]


@app.post("/tasks", response_model=TaskOut)
def create_task(body: TaskCreate, session=Depends(require_session)):
    db = session["db"]
    row = db.execute(text("INSERT INTO tasks (tenant_id, created_by, title, description, risk_level) VALUES (:tenant_id, :created_by, :title, :description, :risk_level) RETURNING id, tenant_id, title, description, risk_level"), {"tenant_id": session["tenant_id"], "created_by": session["user_id"], "title": body.title, "description": body.description, "risk_level": body.risk_level}).fetchone()
    db.commit()
    return {k: str(v) for k, v in row._mapping.items()}


@app.get("/tasks", response_model=list[TaskOut])
def list_tasks(session=Depends(require_session)):
    rows = session["db"].execute(text("SELECT id, tenant_id, title, description, risk_level FROM tasks ORDER BY created_at DESC")).fetchall()
    return [{k: str(v) for k, v in r._mapping.items()} for r in rows]


@app.post("/tasks/{task_id}/run")
def run_task(task_id: str, body: RunTaskRequest, session=Depends(require_session)):
    db = session["db"]
    task = db.execute(text("SELECT id, risk_level FROM tasks WHERE id=:id"), {"id": task_id}).fetchone()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    needs_approval = task.risk_level == "HIGH"
    if needs_approval and not body.approve:
        run = db.execute(text("INSERT INTO task_runs (tenant_id, task_id, status, approval_required, created_by) VALUES (:tenant_id, :task_id, 'PENDING_APPROVAL', TRUE, :created_by) RETURNING id"), {"tenant_id": session["tenant_id"], "task_id": task_id, "created_by": session["user_id"]}).fetchone()
        db.execute(text("INSERT INTO audit_logs (tenant_id, run_id, event_type, payload) VALUES (:tenant_id, :run_id, 'approval_required', CAST(:payload AS jsonb))"), {"tenant_id": session["tenant_id"], "run_id": str(run.id), "payload": json.dumps({"risk": "HIGH"})})
        db.commit()
        return {"run_id": str(run.id), "status": "PENDING_APPROVAL"}

    run = db.execute(text("INSERT INTO task_runs (tenant_id, task_id, status, approval_required, created_by) VALUES (:tenant_id, :task_id, 'QUEUED', :approval_required, :created_by) RETURNING id"), {"tenant_id": session["tenant_id"], "task_id": task_id, "approval_required": needs_approval, "created_by": session["user_id"]}).fetchone()
    if needs_approval:
        db.execute(text("INSERT INTO approvals (tenant_id, run_id, approved_by, approved) VALUES (:tenant_id, :run_id, :approved_by, TRUE)"), {"tenant_id": session["tenant_id"], "run_id": str(run.id), "approved_by": session["user_id"]})
    db.execute(text("INSERT INTO audit_logs (tenant_id, run_id, event_type, payload) VALUES (:tenant_id, :run_id, 'run_queued', CAST(:payload AS jsonb))"), {"tenant_id": session["tenant_id"], "run_id": str(run.id), "payload": json.dumps({"status": "QUEUED"})})
    db.commit()
    enqueue_run_job(str(run.id), session["tenant_id"], session["user_id"], job_id=f"tenant:{session['tenant_id']}:run:{run.id}")
    return {"run_id": str(run.id), "status": "QUEUED"}




@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, session=Depends(require_session)):
    db = session["db"]
    row = db.execute(text("SELECT status FROM task_runs WHERE id=:id"), {"id": run_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    status = str(row.status)
    if status in {"COMPLETED", "FAILED", "CANCELLED"}:
        return {"run_id": run_id, "status": status}
    new_status = "CANCELLED" if status in {"QUEUED", "PENDING_APPROVAL"} else "CANCELLING"
    db.execute(text("UPDATE task_runs SET cancel_requested=TRUE, status=:status WHERE id=:id"), {"id": run_id, "status": new_status})
    db.execute(text("INSERT INTO audit_logs (tenant_id, run_id, event_type, payload) VALUES (:tenant_id,:run_id,'run_cancel_requested',CAST(:payload AS jsonb))"), {"tenant_id": session["tenant_id"], "run_id": run_id, "payload": json.dumps({"status": new_status})})
    db.commit()
    return {"run_id": run_id, "status": new_status}


@app.post("/workflows/runs/{workflow_run_id}/cancel")
def cancel_workflow_run(workflow_run_id: str, session=Depends(require_session)):
    db = session["db"]
    wf = db.execute(text("SELECT linked_run_id, status FROM workflow_runs WHERE id=:id"), {"id": workflow_run_id}).fetchone()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow run not found")
    db.execute(text("UPDATE workflow_runs SET cancel_requested=TRUE, status='cancelled', updated_at=now() WHERE id=:id"), {"id": workflow_run_id})
    if wf.linked_run_id:
        r = db.execute(text("SELECT status FROM task_runs WHERE id=:id"), {"id": str(wf.linked_run_id)}).fetchone()
        if r and str(r.status) not in {"COMPLETED","FAILED","CANCELLED"}:
            ns = "CANCELLED" if str(r.status) in {"QUEUED","PENDING_APPROVAL"} else "CANCELLING"
            db.execute(text("UPDATE task_runs SET cancel_requested=TRUE, status=:s WHERE id=:id"), {"id": str(wf.linked_run_id), "s": ns})
    db.commit()
    return {"workflow_run_id": workflow_run_id, "status": "cancelled"}


@app.get("/tenant/usage")
def tenant_usage(day: str | None = Query(default=None), session=Depends(require_session)):
    d = day or datetime.now(timezone.utc).date().isoformat()
    row = session["db"].execute(text("SELECT day, api_requests_count, tool_calls_count, llm_tokens_count, workflow_runs_count, automation_runs_count, web_automation_runs_count, web_automation_runtime_seconds FROM tenant_usage_daily WHERE day=:day"), {"day": d}).fetchone()
    if not row:
        return {"day": d, "usage": {}}
    return {"day": str(row.day), "usage": {"api_requests_count": row.api_requests_count, "tool_calls_count": row.tool_calls_count, "llm_tokens_count": row.llm_tokens_count, "workflow_runs_count": row.workflow_runs_count, "automation_runs_count": row.automation_runs_count, "web_automation_runs_count": row.web_automation_runs_count, "web_automation_runtime_seconds": row.web_automation_runtime_seconds}}

@app.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, session=Depends(require_session)):
    db = session["db"]
    run = db.execute(text("SELECT id, status, plan_json, verifier_status, verifier_summary FROM task_runs WHERE id=:id"), {"id": run_id}).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    invocations = db.execute(text("SELECT tool_name, args_json, result_json, status, started_at, finished_at, idempotency_key, error_text, external_ref_type, external_ref_id FROM tool_invocations WHERE run_id=:run_id ORDER BY started_at"), {"run_id": run_id}).fetchall()
    return {
        "run_id": str(run.id),
        "status": run.status,
        "plan": run.plan_json,
        "tool_invocations": [{"tool_name": row.tool_name, "args": row.args_json, "result": row.result_json, "status": row.status, "started_at": row.started_at.isoformat() if row.started_at else None, "finished_at": row.finished_at.isoformat() if row.finished_at else None, "idempotency_key": row.idempotency_key, "error": row.error_text, "external_ref_type": row.external_ref_type, "external_ref_id": row.external_ref_id} for row in invocations],
        "verifier": {"status": run.verifier_status, "summary": run.verifier_summary},
    }
