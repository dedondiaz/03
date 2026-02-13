import json
import os
from urllib import request

from sqlalchemy import text

from app.security.crypto import decrypt_str

NOTION_VERSION = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"


class NotionClient:
    def __init__(self, db):
        self.db = db

    def _token(self) -> str:
        row = self.db.execute(text("SELECT access_token_enc FROM notion_credentials LIMIT 1")).fetchone()
        if not row:
            raise ValueError("notion_not_connected")
        return decrypt_str(row.access_token_enc)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(f"{NOTION_API_BASE}{path}", data=data, headers=self._headers(), method=method)
        try:
            with request.urlopen(req, timeout=int(os.getenv("TOOL_CALL_TIMEOUT_S", "8"))) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            return out
        except Exception:
            raise

    def search_pages(self, query: str, page_size: int = 20, start_cursor: str | None = None) -> dict:
        payload = {
            "query": query,
            "filter": {"value": "page", "property": "object"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": max(1, min(int(page_size), 100)),
        }
        if start_cursor:
            payload["start_cursor"] = start_cursor
        return self._request("POST", "/search", payload)

    def retrieve_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def retrieve_block_children(self, block_id: str, start_cursor: str | None = None, page_size: int = 100) -> dict:
        query = f"?page_size={max(1, min(int(page_size), 100))}"
        if start_cursor:
            query += f"&start_cursor={start_cursor}"
        return self._request("GET", f"/blocks/{block_id}/children{query}")
