import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib import request
from sqlalchemy import text
from app.observability.health import mark_connected, mark_success, mark_failure
from app.security.crypto import encrypt_str

AUTH_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"


def _cfg():
    return {
        "client_id": os.getenv("JIRA_CLIENT_ID", ""),
        "client_secret": os.getenv("JIRA_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("JIRA_REDIRECT_URI", "http://localhost:8000/integrations/jira/callback"),
        "scopes": os.getenv("JIRA_SCOPES", "read:jira-work,write:jira-work,read:jira-user,offline_access").split(","),
    }


def jira_connect_url(db, tenant_id: str, user_id: str, session_id: str) -> str:
    cfg = _cfg()
    state = secrets.token_urlsafe(32)
    db.execute(
        text("INSERT INTO jira_oauth_states (tenant_id, user_id, session_id, state, expires_at) VALUES (:tenant_id,:user_id,:session_id,:state,:expires_at)"),
        {"tenant_id": tenant_id, "user_id": user_id, "session_id": session_id, "state": state, "expires_at": datetime.now(timezone.utc)+timedelta(minutes=15)},
    )
    db.commit()
    params = {
        "audience": "api.atlassian.com",
        "client_id": cfg["client_id"],
        "scope": " ".join(cfg["scopes"]),
        "redirect_uri": cfg["redirect_uri"],
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _token_exchange(code: str) -> dict:
    cfg = _cfg()
    payload = {
        "grant_type": "authorization_code",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "redirect_uri": cfg["redirect_uri"],
    }
    req = request.Request(TOKEN_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _accessible_resources(access_token: str) -> list[dict]:
    req = request.Request(RESOURCES_URL, headers={"Authorization": f"Bearer {access_token}"}, method="GET")
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def jira_callback(db, state: str, code: str) -> dict:
    row = db.execute(text("SELECT tenant_id, user_id, session_id, expires_at, used_at FROM jira_oauth_states WHERE state=:state"), {"state": state}).fetchone()
    if not row or row.used_at is not None or row.expires_at < datetime.now(timezone.utc):
        raise ValueError("invalid_state")

    token = _token_exchange(code)
    access_token = token.get("access_token")
    refresh_token = token.get("refresh_token")
    expires_in = token.get("expires_in")
    scope = token.get("scope", "")

    resources = _accessible_resources(access_token)
    site = resources[0] if resources else None  # TODO: support multi-site selection
    if not site:
        raise ValueError("no_site")

    db.execute(text("UPDATE jira_oauth_states SET used_at=now() WHERE state=:state"), {"state": state})
    db.execute(
        text(
            """
            INSERT INTO jira_oauth_credentials
            (tenant_id, cloud_id, site_url, site_name, access_token_enc, refresh_token_enc, token_expires_at, scopes, is_primary, updated_at)
            VALUES
            (:tenant_id,:cloud_id,:site_url,:site_name,:access_token_enc,:refresh_token_enc,:token_expires_at,:scopes,TRUE,now())
            ON CONFLICT (tenant_id, is_primary) DO UPDATE SET
              cloud_id=EXCLUDED.cloud_id,
              site_url=EXCLUDED.site_url,
              site_name=EXCLUDED.site_name,
              access_token_enc=EXCLUDED.access_token_enc,
              refresh_token_enc=COALESCE(EXCLUDED.refresh_token_enc, jira_oauth_credentials.refresh_token_enc),
              token_expires_at=EXCLUDED.token_expires_at,
              scopes=EXCLUDED.scopes,
              updated_at=now()
            """
        ),
        {
            "tenant_id": str(row.tenant_id),
            "cloud_id": site.get("id"),
            "site_url": site.get("url"),
            "site_name": site.get("name"),
            "access_token_enc": encrypt_str(access_token),
            "refresh_token_enc": encrypt_str(refresh_token) if refresh_token else None,
            "token_expires_at": datetime.now(timezone.utc) + timedelta(seconds=int(expires_in)) if expires_in else None,
            "scopes": (site.get("scopes") or scope.split()),
        },
    )
    mark_connected(db, str(row.tenant_id), "jira", True)
    mark_success(db, str(row.tenant_id), "jira")
    db.commit()
    return {"connected": True}


def jira_status(db) -> dict:
    row = db.execute(text("SELECT cloud_id, site_url, site_name, scopes FROM jira_oauth_credentials WHERE is_primary=TRUE LIMIT 1")).fetchone()
    if not row:
        return {"connected": False}
    return {"connected": True, "cloud_id": row.cloud_id, "site_url": row.site_url, "site_name": row.site_name, "scopes": row.scopes}


def jira_disconnect(db) -> dict:
    db.execute(text("DELETE FROM jira_oauth_credentials WHERE is_primary=TRUE"))
    try:
        tenant_row = db.execute(text("SELECT current_setting('app.tenant_id', true) AS tenant_id")).fetchone()
        if tenant_row and tenant_row.tenant_id:
            mark_connected(db, str(tenant_row.tenant_id), "jira", False)
    except Exception:
        pass
    db.commit()
    return {"connected": False}
