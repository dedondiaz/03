import json
from datetime import datetime, timedelta, timezone
from urllib import parse, request
from sqlalchemy import text
from app.security.crypto import encrypt_str
from .oauth import require_google_connected


class CalendarClient:
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
        self.db.execute(text("UPDATE google_oauth_credentials SET access_token_enc=:tok, expiry=:expiry, updated_at=now() WHERE provider='google'"), {"tok": encrypt_str(self.access_token), "expiry": expiry})
        self.db.commit()

    def _request(self, method: str, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = request.Request(
            f"https://www.googleapis.com/calendar/v3/{path}",
            data=data,
            headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"},
            method=method,
        )
        with request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def list_calendars(self):
        return self._request("GET", "users/me/calendarList")

    def freebusy(self, query: dict):
        return self._request("POST", "freeBusy", query)

    def create_event(self, calendar_id: str, event: dict):
        return self._request("POST", f"calendars/{parse.quote(calendar_id, safe='')}/events", event)

    def update_event(self, calendar_id: str, event_id: str, changes: dict):
        return self._request("PATCH", f"calendars/{parse.quote(calendar_id, safe='')}/events/{parse.quote(event_id, safe='')}", changes)

    def delete_event(self, calendar_id: str, event_id: str):
        self._request("DELETE", f"calendars/{parse.quote(calendar_id, safe='')}/events/{parse.quote(event_id, safe='')}")
        return {"cancelled": True, "event_id": event_id}
