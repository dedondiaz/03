import json
import time
from datetime import datetime, timedelta, timezone
from urllib import parse, request, error
from sqlalchemy import text
from app.security.crypto import decrypt_str, encrypt_str


class SlackNotConnectedError(Exception):
    pass


class SlackClient:
    def __init__(self, db):
        self.db = db
        row = db.execute(text("SELECT access_token_enc, refresh_token_enc, token_expires_at FROM slack_oauth_credentials WHERE is_primary=TRUE LIMIT 1")).fetchone()
        if not row:
            raise SlackNotConnectedError("slack_not_connected")
        self.access_token = decrypt_str(row.access_token_enc)
        self.refresh_token = decrypt_str(row.refresh_token_enc) if row.refresh_token_enc else None
        self.expires_at = row.token_expires_at
        self._refresh_if_needed()

    def _refresh_if_needed(self):
        if not self.refresh_token or not self.expires_at or self.expires_at > datetime.now(timezone.utc):
            return
        from os import getenv
        payload = parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": getenv("SLACK_CLIENT_ID", ""),
            "client_secret": getenv("SLACK_CLIENT_SECRET", ""),
        }).encode("utf-8")
        req = request.Request("https://slack.com/api/oauth.v2.access", data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            return
        self.access_token = body["access_token"]
        new_refresh = body.get("refresh_token")
        exp = body.get("expires_in")
        self.db.execute(text("UPDATE slack_oauth_credentials SET access_token_enc=:a, refresh_token_enc=COALESCE(:r, refresh_token_enc), token_expires_at=:e, updated_at=now() WHERE is_primary=TRUE"), {
            "a": encrypt_str(self.access_token),
            "r": encrypt_str(new_refresh) if new_refresh else None,
            "e": datetime.now(timezone.utc) + timedelta(seconds=int(exp)) if exp else None,
        })
        self.db.commit()

    def _api(self, method: str, endpoint: str, payload: dict | None = None, retries: int = 3):
        url = f"https://slack.com/api/{endpoint}"
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json; charset=utf-8"}
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        for attempt in range(retries):
            req = request.Request(url, data=body, headers=headers, method=method)
            try:
                with request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("ok"):
                        return data
                    if data.get("error") == "ratelimited":
                        time.sleep(1 + attempt)
                        continue
                    return data
            except error.HTTPError as exc:
                if exc.code == 429 and attempt < retries - 1:
                    wait = int(exc.headers.get("Retry-After", "1"))
                    time.sleep(wait)
                    continue
                raise
        return {"ok": False, "error": "request_failed"}

    def conversations_list(self, types: str = "public_channel,private_channel,im,mpim", limit: int = 100, cursor: str | None = None):
        return self._api("POST", "conversations.list", {"types": types, "limit": limit, "cursor": cursor} if cursor else {"types": types, "limit": limit})

    def conversations_info(self, channel_id: str):
        return self._api("POST", "conversations.info", {"channel": channel_id})

    def conversations_history(self, channel_id: str, limit: int = 20, cursor: str | None = None, oldest: str | None = None, latest: str | None = None):
        payload = {"channel": channel_id, "limit": limit}
        if cursor: payload["cursor"] = cursor
        if oldest: payload["oldest"] = oldest
        if latest: payload["latest"] = latest
        return self._api("POST", "conversations.history", payload)

    def chat_post_message(self, channel_id: str, text: str, thread_ts: str | None = None):
        payload = {"channel": channel_id, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return self._api("POST", "chat.postMessage", payload)

    def users_lookup_by_email(self, email: str):
        return self._api("POST", "users.lookupByEmail", {"email": email})

    def conversations_open(self, user_id: str):
        return self._api("POST", "conversations.open", {"users": user_id})
