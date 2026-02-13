import json
import time
from datetime import datetime, timedelta, timezone
from urllib import request, parse, error
from sqlalchemy import text
from app.security.crypto import decrypt_str, encrypt_str


class JiraNotConnectedError(Exception):
    pass


class JiraClient:
    def __init__(self, db):
        self.db = db
        row = db.execute(text("SELECT cloud_id, access_token_enc, refresh_token_enc, token_expires_at FROM jira_oauth_credentials WHERE is_primary=TRUE LIMIT 1")).fetchone()
        if not row:
            raise JiraNotConnectedError("jira_not_connected")
        self.cloud_id = row.cloud_id
        self.access_token = decrypt_str(row.access_token_enc)
        self.refresh_token = decrypt_str(row.refresh_token_enc) if row.refresh_token_enc else None
        self.expires_at = row.token_expires_at
        self._refresh_if_needed()

    def _refresh_if_needed(self):
        if not self.refresh_token or not self.expires_at or self.expires_at > datetime.now(timezone.utc):
            return
        from os import getenv
        payload = {
            "grant_type": "refresh_token",
            "client_id": getenv("JIRA_CLIENT_ID", ""),
            "client_secret": getenv("JIRA_CLIENT_SECRET", ""),
            "refresh_token": self.refresh_token,
        }
        req = request.Request("https://auth.atlassian.com/oauth/token", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json"}, method="POST")
        with request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        self.access_token = body.get("access_token", self.access_token)
        new_refresh = body.get("refresh_token")
        exp = body.get("expires_in")
        self.db.execute(text("UPDATE jira_oauth_credentials SET access_token_enc=:a, refresh_token_enc=COALESCE(:r, refresh_token_enc), token_expires_at=:e, updated_at=now() WHERE is_primary=TRUE"), {
            "a": encrypt_str(self.access_token),
            "r": encrypt_str(new_refresh) if new_refresh else None,
            "e": datetime.now(timezone.utc)+timedelta(seconds=int(exp)) if exp else None,
        })
        self.db.commit()

    def _api(self, method: str, path: str, body: dict | None = None, retries: int = 3):
        url = f"https://api.atlassian.com/ex/jira/{self.cloud_id}/rest/api/3/{path}"
        data = None if body is None else json.dumps(body).encode()
        for i in range(retries):
            req = request.Request(url, data=data, headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json", "Content-Type": "application/json"}, method=method)
            try:
                with request.urlopen(req, timeout=20) as resp:
                    if resp.status == 204:
                        return {}
                    out = json.loads(resp.read().decode())
                    return out
            except error.HTTPError as exc:
                if exc.code == 429 and i < retries - 1:
                    wait = int(exc.headers.get("Retry-After", "1"))
                    time.sleep(wait)
                    continue
                if exc.code >= 500 and i < retries - 1:
                    time.sleep(1 + i)
                    continue
                raise

    def list_projects(self):
        return self._api("GET", "project/search")

    def search_issues(self, jql: str, max_results: int = 20, fields: list[str] | None = None):
        return self._api("POST", "search", {"jql": jql, "maxResults": max_results, "fields": fields or ["summary", "status", "assignee", "updated"]})

    def get_issue(self, issue_key: str, fields: list[str] | None = None):
        q = parse.urlencode({"fields": ",".join(fields)}) if fields else ""
        return self._api("GET", f"issue/{issue_key}{'?' + q if q else ''}")

    def create_issue(self, project_key: str, issue_type: str, summary: str, description: str | None = None, priority: str | None = None, assignee_account_id: str | None = None, labels: list[str] | None = None):
        fields = {"project": {"key": project_key}, "issuetype": {"name": issue_type}, "summary": summary}
        if description:
            fields["description"] = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]}
        if priority:
            fields["priority"] = {"name": priority}
        if assignee_account_id:
            fields["assignee"] = {"id": assignee_account_id}
        if labels:
            fields["labels"] = labels
        return self._api("POST", "issue", {"fields": fields})

    def add_comment(self, issue_key: str, body_text: str):
        body = {"body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": body_text}]}]}}
        return self._api("POST", f"issue/{issue_key}/comment", body)

    def list_transitions(self, issue_key: str):
        return self._api("GET", f"issue/{issue_key}/transitions")

    def transition_issue(self, issue_key: str, transition_id: str):
        return self._api("POST", f"issue/{issue_key}/transitions", {"transition": {"id": transition_id}})
