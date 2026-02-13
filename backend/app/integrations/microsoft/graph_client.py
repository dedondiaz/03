import json
import time
from datetime import datetime, timedelta, timezone
from urllib import request, parse, error

from sqlalchemy import text

from app.security.crypto import encrypt_str
from .oauth import require_microsoft_connected, MicrosoftNotConnectedError

BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


class MicrosoftGraphClient:
    def __init__(self, db):
        self.db = db
        creds = require_microsoft_connected(db)
        self.access_token = creds["access_token"]
        self.refresh_token = creds["refresh_token"]
        self.expires_at = creds["expires_at"]
        self._refresh_if_needed()

    def _refresh_if_needed(self):
        if self.expires_at and self.expires_at > datetime.now(timezone.utc) + timedelta(seconds=30):
            return
        if not self.refresh_token:
            raise MicrosoftNotConnectedError("microsoft_not_connected")
        from os import getenv
        payload = parse.urlencode({
            "client_id": getenv("MICROSOFT_CLIENT_ID", ""),
            "client_secret": getenv("MICROSOFT_CLIENT_SECRET", ""),
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "scope": getenv("MICROSOFT_SCOPES", "offline_access User.Read Mail.Read Mail.Send Calendars.ReadWrite"),
        }).encode("utf-8")
        req = request.Request(TOKEN_URL, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        self.access_token = body.get("access_token", self.access_token)
        new_refresh = body.get("refresh_token")
        exp = datetime.now(timezone.utc) + timedelta(seconds=int(body.get("expires_in", 3600)))
        self.db.execute(text("""
          UPDATE microsoft_oauth_credentials SET
            access_token_enc=:a,
            refresh_token_enc=COALESCE(:r, refresh_token_enc),
            token_expires_at=:e,
            updated_at=now()
          WHERE is_primary=TRUE
        """), {"a": encrypt_str(self.access_token), "r": encrypt_str(new_refresh) if new_refresh else None, "e": exp})
        self.db.commit()

    def _api(self, method: str, path: str, body: dict | None = None, retries: int = 3):
        url = f"{BASE}/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        for i in range(retries):
            req = request.Request(url, data=data, headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}, method=method)
            try:
                with request.urlopen(req, timeout=20) as resp:
                    if resp.status == 204:
                        return {}
                    out = json.loads(resp.read().decode())
                    return out
            except error.HTTPError as exc:
                if exc.code == 401 and i == 0:
                    self.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
                    self._refresh_if_needed()
                    continue
                if exc.code == 429 and i < retries - 1:
                    wait = int(exc.headers.get("Retry-After", "1"))
                    time.sleep(wait)
                    continue
                if exc.code >= 500 and i < retries - 1:
                    time.sleep(1 + i)
                    continue
                raise

    def mail_search(self, query: str, top: int = 10):
        q = parse.quote(query)
        return self._api("GET", f"me/messages?$search=\"{q}\"&$top={min(max(1, int(top)), 25)}")

    def mail_get_message(self, message_id: str):
        return self._api("GET", f"me/messages/{message_id}")

    def mail_create_draft(self, to: list[str], cc: list[str] | None, bcc: list[str] | None, subject: str, body_text: str):
        def recips(xs):
            return [{"emailAddress": {"address": x}} for x in (xs or [])]
        body = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": recips(to),
            "ccRecipients": recips(cc),
            "bccRecipients": recips(bcc),
        }
        return self._api("POST", "me/messages", body)

    def mail_send(self, draft_id: str | None = None, payload: dict | None = None):
        if draft_id:
            self._api("POST", f"me/messages/{draft_id}/send", {})
            return {"message_id": draft_id}
        msg = self.mail_create_draft(payload.get("to") or [], payload.get("cc"), payload.get("bcc"), payload.get("subject") or "", payload.get("body_text") or "")
        self._api("POST", f"me/messages/{msg.get('id')}/send", {})
        return {"message_id": msg.get("id")}

    def calendar_find_meeting_times(self, attendees: list[str], time_min: str, time_max: str, duration_minutes: int, timezone_name: str, max_candidates: int = 5):
        body = {
            "attendees": [{"type": "required", "emailAddress": {"address": e}} for e in attendees],
            "timeConstraint": {"timeslots": [{"start": {"dateTime": time_min, "timeZone": timezone_name}, "end": {"dateTime": time_max, "timeZone": timezone_name}}]},
            "meetingDuration": f"PT{int(duration_minutes)}M",
            "maxCandidates": min(max(1, int(max_candidates)), 10),
        }
        return self._api("POST", "me/findMeetingTimes", body)

    def calendar_create_event(self, payload: dict):
        return self._api("POST", "me/events", payload)

    def calendar_update_event(self, event_id: str, patch: dict):
        return self._api("PATCH", f"me/events/{event_id}", patch)

    def calendar_cancel_event(self, event_id: str):
        return self._api("DELETE", f"me/events/{event_id}")
