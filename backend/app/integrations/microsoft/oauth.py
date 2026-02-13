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

AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


class MicrosoftNotConnectedError(Exception):
    pass


def _cfg() -> dict:
    return {
        "client_id": os.getenv("MICROSOFT_CLIENT_ID", ""),
        "client_secret": os.getenv("MICROSOFT_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("MICROSOFT_REDIRECT_URI", "http://localhost:8000/integrations/microsoft/callback"),
        "scopes": os.getenv("MICROSOFT_SCOPES", "offline_access User.Read Mail.Read Mail.Send Calendars.ReadWrite").split(),
    }


def _challenge(verifier: str) -> str:
    d = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def microsoft_connect_url(db, tenant_id: str, user_id: str, session_id: str) -> str:
    cfg = _cfg()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    db.execute(text("""
      INSERT INTO microsoft_oauth_states (tenant_id, user_id, session_id, state, code_verifier_enc, expires_at)
      VALUES (:tenant_id, :user_id, :session_id, :state, :verifier, :expires)
    """), {
      "tenant_id": tenant_id,
      "user_id": user_id,
      "session_id": session_id,
      "state": state,
      "verifier": encrypt_str(verifier),
      "expires": datetime.now(timezone.utc) + timedelta(minutes=15),
    })
    db.commit()
    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": cfg["redirect_uri"],
        "response_mode": "query",
        "scope": " ".join(cfg["scopes"]),
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _exchange_code(code: str, verifier: str) -> dict:
    cfg = _cfg()
    payload = urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg["redirect_uri"],
        "code_verifier": verifier,
    }).encode("utf-8")
    req = request.Request(TOKEN_URL, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def microsoft_callback(db, state: str, code: str) -> dict:
    row = db.execute(text("SELECT tenant_id, user_id, session_id, code_verifier_enc, expires_at, used_at FROM microsoft_oauth_states WHERE state=:state"), {"state": state}).fetchone()
    if not row or row.used_at is not None or row.expires_at < datetime.now(timezone.utc):
        raise ValueError("invalid_state")
    verifier = decrypt_str(row.code_verifier_enc)
    token = _exchange_code(code, verifier)
    access = token.get("access_token")
    if not access:
        raise ValueError("oauth_failed")
    refresh = token.get("refresh_token")
    expires_in = int(token.get("expires_in", 3600))
    scopes = (token.get("scope") or "").split()
    claims = token.get("id_token_claims") or {}

    db.execute(text("UPDATE microsoft_oauth_states SET used_at=now() WHERE state=:state"), {"state": state})
    db.execute(text("""
      INSERT INTO microsoft_oauth_credentials (tenant_id, account_id, user_principal_name, tenant_directory_id, access_token_enc, refresh_token_enc, token_expires_at, scopes, is_primary, updated_at)
      VALUES (:tenant_id,:account_id,:upn,:tid,:access,:refresh,:exp,:scopes,TRUE,now())
      ON CONFLICT (tenant_id, is_primary) DO UPDATE SET
        account_id=EXCLUDED.account_id,
        user_principal_name=EXCLUDED.user_principal_name,
        tenant_directory_id=EXCLUDED.tenant_directory_id,
        access_token_enc=EXCLUDED.access_token_enc,
        refresh_token_enc=COALESCE(EXCLUDED.refresh_token_enc, microsoft_oauth_credentials.refresh_token_enc),
        token_expires_at=EXCLUDED.token_expires_at,
        scopes=EXCLUDED.scopes,
        updated_at=now()
    """), {
      "tenant_id": str(row.tenant_id),
      "account_id": claims.get("oid") or claims.get("sub"),
      "upn": claims.get("preferred_username") or claims.get("upn"),
      "tid": claims.get("tid"),
      "access": encrypt_str(access),
      "refresh": encrypt_str(refresh) if refresh else None,
      "exp": datetime.now(timezone.utc) + timedelta(seconds=expires_in),
      "scopes": scopes,
    })
    mark_connected(db, str(row.tenant_id), "microsoft", True)
    mark_success(db, str(row.tenant_id), "microsoft")
    db.commit()
    return {"connected": True}


def microsoft_status(db) -> dict:
    row = db.execute(text("SELECT account_id, user_principal_name, tenant_directory_id, scopes, token_expires_at FROM microsoft_oauth_credentials WHERE is_primary=TRUE LIMIT 1")).fetchone()
    if not row:
        return {"connected": False}
    return {
      "connected": True,
      "account_id": row.account_id,
      "user_principal_name": row.user_principal_name,
      "tenant_directory_id": row.tenant_directory_id,
      "scopes": row.scopes,
      "token_expires_at": row.token_expires_at.isoformat() if row.token_expires_at else None,
    }


def microsoft_disconnect(db) -> dict:
    db.execute(text("DELETE FROM microsoft_oauth_credentials WHERE is_primary=TRUE"))
    try:
        tenant_row = db.execute(text("SELECT current_setting('app.tenant_id', true) AS tenant_id")).fetchone()
        if tenant_row and tenant_row.tenant_id:
            mark_connected(db, str(tenant_row.tenant_id), "microsoft", False)
    except Exception:
        pass
    db.commit()
    return {"connected": False}


def require_microsoft_connected(db) -> dict:
    row = db.execute(text("SELECT access_token_enc, refresh_token_enc, token_expires_at FROM microsoft_oauth_credentials WHERE is_primary=TRUE LIMIT 1")).fetchone()
    if not row:
        raise MicrosoftNotConnectedError("microsoft_not_connected")
    return {
      "access_token": decrypt_str(row.access_token_enc),
      "refresh_token": decrypt_str(row.refresh_token_enc) if row.refresh_token_enc else None,
      "expires_at": row.token_expires_at,
    }
