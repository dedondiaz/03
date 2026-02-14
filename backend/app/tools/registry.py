from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolSpec:
    name: str
    description: str
    json_schema: dict
    risk_level: str
    idempotent: bool
    tenant_scoped: bool = True
    min_role: str = "member"
    handler: Callable[[dict, dict], dict] | None = None
    risk_evaluator: Callable[[dict, dict], str] | None = None
    idempotency_builder: Callable[[str, dict], str] | None = None
    redact_args: Callable[[dict], dict] | None = None
    redact_result: Callable[[dict], dict] | None = None

    def llm_definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.json_schema,
        }


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec]):
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"Unknown tool {name}")
        return self._tools[name]

    def list_specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def llm_tools(self) -> list[dict[str, Any]]:
        return [t.llm_definition() for t in self._tools.values()]
