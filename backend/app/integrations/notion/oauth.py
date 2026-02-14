import base64
import json
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib import request

from sqlalchemy import text
from app.observability.health import mark_connected, mark_success, mark_failure

from app.security.crypto import encrypt_str

NOTION_AUTH_URL = "https://api.notion.com/v1/oauth/authorize"
NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"


def _cfg() -> dict:
    return {
        "client_id": os.getenv("NOTION_CLIENT_ID", ""),
        "client_secret": os.getenv("NOTION_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("NOTION_REDIRECT_URI", "http://localhost:8000/integrations/notion/callback"),
    }


def notion_connect_url(db, tenant_id: str, user_id: str) -> str:
    state = secrets.token_urlsafe(32)
    db.execute(
        text(
            """
            INSERT INTO notion_oauth_states (tenant_id, user_id, state, expires_at)
            VALUES (:tenant_id, :user_id, :state, now() + interval '15 minutes')
            """
        ),
        {"tenant_id": tenant_id, "user_id": user_id, "state": state},
    )
    db.commit()
    params = {
        "client_id": _cfg()["client_id"],
        "response_type": "code",
        "owner": "user",
        "redirect_uri": _cfg()["redirect_uri"],
        "state": state,
    }
    return f"{NOTION_AUTH_URL}?{urlencode(params)}"


def _exchange(code: str) -> dict:
    cfg = _cfg()
    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    payload = json.dumps({"grant_type": "authorization_code", "code": code, "redirect_uri": cfg["redirect_uri"]}).encode("utf-8")
    req = request.Request(NOTION_TOKEN_URL, data=payload, headers={"Authorization": f"Basic {basic}", "Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def notion_callback(db, state: str, code: str) -> dict:
    row = db.execute(text("SELECT tenant_id, user_id, used_at, expires_at FROM notion_oauth_states WHERE state=:state"), {"state": state}).fetchone()
    if not row or row.used_at is not None or row.expires_at < datetime.now(timezone.utc):
        raise ValueError("invalid_state")

    token = _exchange(code)
    access_token = token.get("access_token")
    if not access_token:
        raise ValueError("oauth_failed")

    db.execute(text("UPDATE notion_oauth_states SET used_at=now() WHERE state=:state"), {"state": state})
    db.execute(
        text(
            """
            INSERT INTO notion_credentials (tenant_id, access_token_enc, workspace_id, workspace_name, bot_id, connected_by, updated_at)
            VALUES (:tenant_id, :token, :workspace_id, :workspace_name, :bot_id, :connected_by, now())
            ON CONFLICT (tenant_id)
            DO UPDATE SET access_token_enc=EXCLUDED.access_token_enc, workspace_id=EXCLUDED.workspace_id, workspace_name=EXCLUDED.workspace_name, bot_id=EXCLUDED.bot_id, connected_by=EXCLUDED.connected_by, updated_at=now()
            """
        ),
        {
            "tenant_id": str(row.tenant_id),
            "token": encrypt_str(access_token),
            "workspace_id": token.get("workspace_id"),
            "workspace_name": token.get("workspace_name"),
            "bot_id": token.get("bot_id"),
            "connected_by": str(row.user_id),
        },
    )
    mark_connected(db, str(row.tenant_id), "notion", True)
    mark_success(db, str(row.tenant_id), "notion")
    db.commit()
    return {"connected": True}


def notion_status(db) -> dict:
    row = db.execute(text("SELECT workspace_id, workspace_name, bot_id FROM notion_credentials LIMIT 1")).fetchone()
    if not row:
        return {"connected": False}
    return {"connected": True, "workspace_id": row.workspace_id, "workspace_name": row.workspace_name, "bot_id": row.bot_id}


def notion_disconnect(db) -> dict:
    db.execute(text("DELETE FROM notion_credentials"))
    db.execute(text("DELETE FROM notion_documents"))
    try:
        tenant_row = db.execute(text("SELECT current_setting('app.tenant_id', true) AS tenant_id")).fetchone()
        if tenant_row and tenant_row.tenant_id:
            mark_connected(db, str(tenant_row.tenant_id), "notion", False)
    except Exception:
        pass
    db.commit()
    return {"connected": False}
