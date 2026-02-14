import secrets
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session
from .db import SessionLocal

bearer = HTTPBearer()


def db_dep():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def login(db: Session, email: str, password: str):
    row = db.execute(text("SELECT id FROM users WHERE email=:email AND password=:password"), {"email": email, "password": password}).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    membership = db.execute(
        text("SELECT tenant_id FROM memberships WHERE user_id=:user_id ORDER BY role DESC LIMIT 1"),
        {"user_id": str(row.id)},
    ).fetchone()
    token = secrets.token_urlsafe(32)
    db.execute(
        text("INSERT INTO sessions (token, user_id, tenant_id) VALUES (:token, :user_id, :tenant_id)"),
        {"token": token, "user_id": str(row.id), "tenant_id": str(membership.tenant_id)},
    )
    db.commit()
    return {"token": token, "tenant_id": str(membership.tenant_id)}


def require_session(credentials: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(db_dep)):
    token = credentials.credentials
    row = db.execute(text("SELECT user_id, tenant_id FROM sessions WHERE token=:token"), {"token": token}).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid session")

    db.execute(text("SET app.tenant_id = :tenant_id"), {"tenant_id": str(row.tenant_id)})
    db.execute(text("SET app.user_id = :user_id"), {"user_id": str(row.user_id)})
    return {"user_id": str(row.user_id), "tenant_id": str(row.tenant_id), "token": token, "db": db}


def switch_tenant(db: Session, token: str, user_id: str, tenant_id: str):
    member = db.execute(
        text("SELECT 1 FROM memberships WHERE user_id=:user_id AND tenant_id=:tenant_id"),
        {"user_id": user_id, "tenant_id": tenant_id},
    ).fetchone()
    if not member:
        raise HTTPException(status_code=403, detail="Not a member of tenant")

    new_token = secrets.token_urlsafe(32)
    db.execute(
        text("INSERT INTO sessions (token, user_id, tenant_id) VALUES (:token, :user_id, :tenant_id)"),
        {"token": new_token, "user_id": user_id, "tenant_id": tenant_id},
    )
    db.execute(text("DELETE FROM sessions WHERE token=:token"), {"token": token})
    db.commit()
    return {"token": new_token, "tenant_id": tenant_id}
