import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib import request
from sqlalchemy import text
from app.observability.health import mark_connected, mark_success, mark_failure
from app.security.crypto import encrypt_str, decrypt_str

SLACK_AUTH_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"


def _cfg() -> dict:
    return {
        "client_id": os.getenv("SLACK_CLIENT_ID", ""),
        "client_secret": os.getenv("SLACK_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("SLACK_REDIRECT_URI", "http://localhost:8000/integrations/slack/callback"),
        "scopes": os.getenv("SLACK_SCOPES", "channels:read,groups:read,chat:write,users:read.email").split(","),
    }


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def slack_connect_url(db, tenant_id: str, user_id: str, session_id: str) -> str:
    cfg = _cfg()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    db.execute(
        text("INSERT INTO slack_oauth_states (tenant_id, user_id, session_id, state, code_verifier_enc, expires_at) VALUES (:tenant_id,:user_id,:session_id,:state,:verifier,:expires_at)"),
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "session_id": session_id,
            "state": state,
            "verifier": encrypt_str(verifier),
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
        },
    )
    db.commit()
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "scope": ",".join(cfg["scopes"]),
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{SLACK_AUTH_URL}?{urlencode(params)}"


def _exchange(code: str, verifier: str) -> dict:
    cfg = _cfg()
    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    payload = urlencode({"code": code, "redirect_uri": cfg["redirect_uri"], "code_verifier": verifier}).encode("utf-8")
    req = request.Request(SLACK_TOKEN_URL, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"}, method="POST")
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def slack_callback(db, state: str, code: str) -> dict:
    row = db.execute(text("SELECT tenant_id, user_id, session_id, code_verifier_enc, expires_at, used_at FROM slack_oauth_states WHERE state=:state"), {"state": state}).fetchone()
    if not row or row.used_at is not None or row.expires_at < datetime.now(timezone.utc):
        raise ValueError("invalid_state")
    verifier = decrypt_str(row.code_verifier_enc)
    token = _exchange(code, verifier)
    if not token.get("ok"):
        raise ValueError("oauth_failed")

    bot_token = token.get("access_token", "")
    refresh = token.get("refresh_token")
    expires_in = token.get("expires_in")
    scopes = (token.get("scope") or "").split(",") if token.get("scope") else []

    db.execute(text("UPDATE slack_oauth_states SET used_at=now() WHERE state=:state"), {"state": state})
    db.execute(
        text(
            """
            INSERT INTO slack_oauth_credentials
            (tenant_id, team_id, team_name, enterprise_id, app_id, bot_user_id, access_token_enc, refresh_token_enc, token_expires_at, scopes, is_primary, updated_at)
            VALUES
            (:tenant_id, :team_id, :team_name, :enterprise_id, :app_id, :bot_user_id, :access_token_enc, :refresh_token_enc, :token_expires_at, :scopes, TRUE, now())
            ON CONFLICT (tenant_id, is_primary) DO UPDATE SET
              team_id=EXCLUDED.team_id,
              team_name=EXCLUDED.team_name,
              enterprise_id=EXCLUDED.enterprise_id,
              app_id=EXCLUDED.app_id,
              bot_user_id=EXCLUDED.bot_user_id,
              access_token_enc=EXCLUDED.access_token_enc,
              refresh_token_enc=COALESCE(EXCLUDED.refresh_token_enc, slack_oauth_credentials.refresh_token_enc),
              token_expires_at=EXCLUDED.token_expires_at,
              scopes=EXCLUDED.scopes,
              updated_at=now()
            """
        ),
        {
            "tenant_id": str(row.tenant_id),
            "team_id": (token.get("team") or {}).get("id"),
            "team_name": (token.get("team") or {}).get("name"),
            "enterprise_id": (token.get("enterprise") or {}).get("id") if token.get("enterprise") else None,
            "app_id": token.get("app_id"),
            "bot_user_id": token.get("bot_user_id"),
            "access_token_enc": encrypt_str(bot_token),
            "refresh_token_enc": encrypt_str(refresh) if refresh else None,
            "token_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))) if expires_in else None,
            "scopes": scopes,
        },
    )
    mark_connected(db, str(row.tenant_id), "slack", True)
    mark_success(db, str(row.tenant_id), "slack")
    db.commit()
    return {"connected": True}


def slack_status(db) -> dict:
    row = db.execute(text("SELECT team_id, team_name, bot_user_id, scopes FROM slack_oauth_credentials WHERE is_primary=TRUE LIMIT 1")).fetchone()
    if not row:
        return {"connected": False}
    return {"connected": True, "team_id": row.team_id, "team_name": row.team_name, "bot_user_id": row.bot_user_id, "scopes": row.scopes}


def slack_disconnect(db) -> dict:
    db.execute(text("DELETE FROM slack_oauth_credentials WHERE is_primary=TRUE"))
    try:
        tenant_row = db.execute(text("SELECT current_setting('app.tenant_id', true) AS tenant_id")).fetchone()
        if tenant_row and tenant_row.tenant_id:
            mark_connected(db, str(tenant_row.tenant_id), "slack", False)
    except Exception:
        pass
    db.commit()
    return {"connected": False}
