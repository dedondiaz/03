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

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DEFAULT_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]


class GoogleNotConnectedError(Exception):
    pass


def _cfg() -> dict:
    return {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/integrations/google/callback"),
    }


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def create_connect_url(db, tenant_id: str, user_id: str) -> str:
    cfg = _cfg()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)

    db.execute(
        text(
            """
            INSERT INTO google_oauth_states (tenant_id, user_id, state, code_verifier, expires_at)
            VALUES (:tenant_id, :user_id, :state, :code_verifier, :expires_at)
            """
        ),
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "state": state,
            "code_verifier": verifier,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=15),
        },
    )
    db.commit()

    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": " ".join(DEFAULT_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, verifier: str) -> dict:
    cfg = _cfg()
    payload = urlencode(
        {
            "code": code,
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uri": cfg["redirect_uri"],
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    ).encode("utf-8")
    req = request.Request(GOOGLE_TOKEN_URL, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def callback_store_tokens(db, state: str, code: str) -> dict:
    row = db.execute(
        text(
            """
            SELECT tenant_id, user_id, code_verifier, used, expires_at
            FROM google_oauth_states WHERE state=:state
            """
        ),
        {"state": state},
    ).fetchone()
    if not row or row.used or row.expires_at < datetime.now(timezone.utc):
        raise ValueError("invalid_state")

    token = exchange_code(code, row.code_verifier)
    access_token = token.get("access_token", "")
    refresh_token = token.get("refresh_token", "")
    expires_in = token.get("expires_in", 3600)
    scope = token.get("scope", "")
    subject_email = token.get("id_token", "")[:12] + "..." if token.get("id_token") else None

    db.execute(text("UPDATE google_oauth_states SET used=TRUE WHERE state=:state"), {"state": state})
    db.execute(
        text(
            """
            INSERT INTO google_oauth_credentials
              (tenant_id, provider, scopes, subject_email, access_token_enc, refresh_token_enc, expiry, updated_at)
            VALUES
              (:tenant_id, 'google', :scopes, :subject_email, :access_token_enc, :refresh_token_enc, :expiry, now())
            ON CONFLICT (tenant_id, provider)
            DO UPDATE SET
              scopes=EXCLUDED.scopes,
              subject_email=EXCLUDED.subject_email,
              access_token_enc=EXCLUDED.access_token_enc,
              refresh_token_enc=COALESCE(EXCLUDED.refresh_token_enc, google_oauth_credentials.refresh_token_enc),
              expiry=EXCLUDED.expiry,
              updated_at=now()
            """
        ),
        {
            "tenant_id": str(row.tenant_id),
            "scopes": scope.split(),
            "subject_email": subject_email,
            "access_token_enc": encrypt_str(access_token),
            "refresh_token_enc": encrypt_str(refresh_token) if refresh_token else None,
            "expiry": datetime.now(timezone.utc) + timedelta(seconds=int(expires_in)),
        },
    )
    mark_connected(db, str(row.tenant_id), "google", True)
    mark_success(db, str(row.tenant_id), "google")
    db.commit()
    return {"connected": True}


def get_google_status(db) -> dict:
    row = db.execute(
        text("SELECT subject_email, scopes, expiry FROM google_oauth_credentials WHERE provider='google' LIMIT 1")
    ).fetchone()
    if not row:
        return {"connected": False}
    return {
        "connected": True,
        "email": row.subject_email,
        "scopes": row.scopes,
        "expiry": row.expiry.isoformat() if row.expiry else None,
    }


def disconnect_google(db):
    row = db.execute(text("SELECT access_token_enc FROM google_oauth_credentials WHERE provider='google' LIMIT 1")).fetchone()
    if row:
        token = decrypt_str(row.access_token_enc)
        try:
            req = request.Request(f"{GOOGLE_REVOKE_URL}?token={token}", method="POST")
            request.urlopen(req, timeout=10)
        except Exception:
            pass
    db.execute(text("DELETE FROM google_oauth_credentials WHERE provider='google'"))
    db.commit()


def require_google_connected(db) -> dict:
    row = db.execute(
        text("SELECT access_token_enc, refresh_token_enc, expiry, subject_email FROM google_oauth_credentials WHERE provider='google' LIMIT 1")
    ).fetchone()
    if not row:
        raise GoogleNotConnectedError("google_not_connected")
    return {
        "access_token": decrypt_str(row.access_token_enc),
        "refresh_token": decrypt_str(row.refresh_token_enc) if row.refresh_token_enc else None,
        "expiry": row.expiry,
        "subject_email": row.subject_email,
    }
