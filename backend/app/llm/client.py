import json
import os
import time
from dataclasses import dataclass
from urllib import error, request

TRANSIENT_CODES = {408, 409, 429, 500, 502, 503, 504}


@dataclass
class LLMConfig:
    mode: str
    model: str
    api_key: str | None
    timeout_s: int
    retries: int


def get_config() -> LLMConfig:
    return LLMConfig(
        mode=os.getenv("LLM_MODE", "fake"),
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        api_key=os.getenv("OPENAI_API_KEY"),
        timeout_s=int(os.getenv("LLM_TIMEOUT_S", "20")),
        retries=int(os.getenv("LLM_RETRIES", "2")),
    )


def redact(value: str | None) -> str:
    if not value:
        return "<none>"
    if len(value) < 8:
        return "***"
    return value[:4] + "***" + value[-2:]


class LLMClient:
    def __init__(self):
        self.cfg = get_config()

    def planner(self, task: dict, tools: list[dict]) -> dict:
        if self.cfg.mode == "fake":
            calls = [{"tool": "web_search_stub", "args": {"query": task["title"]}}, {"tool": "echo_tool", "args": {"text": task["description"]}}]
            desc = task["description"].lower()
            if "note" in desc:
                calls.append({"tool": "create_note", "args": {"title": task["title"], "body": task["description"]}})
            if "gmail" in desc or "email" in desc:
                calls.append({"tool": "gmail_search", "args": {"query": task["title"], "max_results": 3}})
            if "calendar" in desc or "schedule" in desc:
                calls.append({"tool": "calendar_find_slots", "args": {"duration_minutes": 30, "time_min": "2026-01-01T10:00:00+00:00", "time_max": "2026-01-01T18:00:00+00:00", "attendees": [], "max_slots": 3}})
            if "slack" in desc:
                calls.append({"tool": "slack_list_conversations", "args": {"limit": 20}})
            return {"plan": ["analyze task", "collect context", "produce result"], "tool_calls": calls}
        return self._openai_json(
            system="Return valid JSON with keys: plan (array of strings), tool_calls (array with tool and args).",
            user=json.dumps({"task": task, "tools": tools}),
        )

    def verifier(self, task: dict, outputs: list[dict]) -> dict:
        if self.cfg.mode == "fake":
            if not outputs:
                return {"status": "needs_input", "summary": "No tool output available.", "follow_up_questions": ["Please provide more details."]}
            return {"status": "success", "summary": f"Completed task '{task['title']}' with {len(outputs)} tool calls.", "follow_up_questions": []}
        return self._openai_json(
            system=(
                "Return valid JSON keys: status(success|needs_input|failed), summary(string), "
                "follow_up_questions(array). Only add questions if required."
            ),
            user=json.dumps({"task": task, "tool_outputs": outputs}),
        )

    def _openai_json(self, system: str, user: str) -> dict:
        if not self.cfg.api_key:
            raise RuntimeError("OPENAI_API_KEY missing for openai mode")

        payload = {
            "model": self.cfg.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        req = request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.api_key}",
            },
            method="POST",
        )

        last_error: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            try:
                with request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    content = body["choices"][0]["message"]["content"]
                    return json.loads(content)
            except error.HTTPError as exc:
                last_error = exc
                if exc.code not in TRANSIENT_CODES or attempt >= self.cfg.retries:
                    raise
                time.sleep(0.5 * (attempt + 1))
            except Exception as exc:
                last_error = exc
                if attempt >= self.cfg.retries:
                    raise
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"LLM request failed key={redact(self.cfg.api_key)} error={last_error}")
