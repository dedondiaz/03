import base64
import json
from datetime import datetime, timezone, timedelta
from urllib import request, parse
from sqlalchemy import text
from .oauth import require_google_connected, exchange_code
from app.security.crypto import encrypt_str


class GmailClient:
    def __init__(self, db):
        self.db = db
        creds = require_google_connected(db)
        self.access_token = creds["access_token"]
        self.refresh_token = creds["refresh_token"]
        self.expiry = creds["expiry"]
        self._refresh_if_needed()

    def _refresh_if_needed(self):
        if self.expiry and self.expiry > datetime.now(timezone.utc) + timedelta(seconds=30):
            return
        if not self.refresh_token:
            return
        # using oauth token endpoint grant refresh
        data = parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            }
        ).encode("utf-8")
        # exchange_code helper is for auth code; do direct request for refresh flow
        from os import getenv
        payload = parse.urlencode(
            {
                "client_id": getenv("GOOGLE_CLIENT_ID", ""),
                "client_secret": getenv("GOOGLE_CLIENT_SECRET", ""),
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            }
        ).encode("utf-8")
        req = request.Request("https://oauth2.googleapis.com/token", data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        self.access_token = body["access_token"]
        expiry = datetime.now(timezone.utc) + timedelta(seconds=int(body.get("expires_in", 3600)))
        self.db.execute(
            text("UPDATE google_oauth_credentials SET access_token_enc=:tok, expiry=:expiry, updated_at=now() WHERE provider='google'"),
            {"tok": encrypt_str(self.access_token), "expiry": expiry},
        )
        self.db.commit()

    def _request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = request.Request(
            f"https://gmail.googleapis.com/gmail/v1/{path}",
            data=data,
            headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with request.urlopen(req, timeout=20) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            return out
        except Exception:
            raise

    def search(self, query: str, max_results: int = 10):
        q = parse.urlencode({"q": query, "maxResults": max_results})
        ids = self._request("GET", f"users/me/messages?{q}")
        out = []
        for msg in ids.get("messages", []):
            full = self.read(msg["id"])
            out.append({
                "id": full["id"],
                "thread_id": full["thread_id"],
                "from": full["headers"].get("From"),
                "subject": full["headers"].get("Subject"),
                "snippet": full.get("snippet"),
                "date": full["headers"].get("Date"),
            })
        return {"messages": out}

    def read(self, message_id: str):
        data = self._request("GET", f"users/me/messages/{message_id}?format=full")
        headers = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
        body_text = ""
        body_html = None
        payload = data.get("payload", {})
        if payload.get("body", {}).get("data"):
            body_text = base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", errors="ignore")
        for part in payload.get("parts", []) or []:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                body_text = base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", errors="ignore")
            if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
                body_html = base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", errors="ignore")
        return {
            "id": data.get("id"),
            "thread_id": data.get("threadId"),
            "headers": headers,
            "body_text": body_text,
            "body_html": body_html,
            "snippet": data.get("snippet"),
        }

    def create_draft(self, to: list[str], subject: str, body_text: str, cc: list[str] | None = None, bcc: list[str] | None = None):
        headers = [f"To: {', '.join(to)}", f"Subject: {subject}"]
        if cc:
            headers.append(f"Cc: {', '.join(cc)}")
        if bcc:
            headers.append(f"Bcc: {', '.join(bcc)}")
        message = "\r\n".join(headers) + "\r\n\r\n" + body_text
        raw = base64.urlsafe_b64encode(message.encode("utf-8")).decode("utf-8")
        data = self._request("POST", "users/me/drafts", {"message": {"raw": raw}})
        return {"draft_id": data["id"]}

    def send(self, draft_id: str | None = None, to: list[str] | None = None, subject: str | None = None, body_text: str | None = None, cc: list[str] | None = None, bcc: list[str] | None = None):
        if draft_id:
            data = self._request("POST", "users/me/drafts/send", {"id": draft_id})
            return {"message_id": data.get("id")}
        payload = self.create_draft(to or [], subject or "", body_text or "", cc, bcc)
        return self.send(draft_id=payload["draft_id"])
