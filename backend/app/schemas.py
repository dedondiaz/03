from pydantic import BaseModel, Field
from typing import Literal, Any


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    token: str
    tenant_id: str


class TenantSwitchRequest(BaseModel):
    tenant_id: str


class TaskCreate(BaseModel):
    title: str
    description: str
    risk_level: Literal['LOW', 'MEDIUM', 'HIGH']


class TaskOut(BaseModel):
    id: str
    tenant_id: str
    title: str
    description: str
    risk_level: str


class RunTaskRequest(BaseModel):
    approve: bool = False


class RunDetail(BaseModel):
    run_id: str
    status: str
    plan: dict[str, Any] | None
    tool_invocations: list[dict[str, Any]]
    verifier: dict[str, Any]


class SearchResult(BaseModel):
    source: Literal['gmail', 'slack', 'jira', 'notion']
    item_type: Literal['email', 'message', 'issue', 'doc']
    title: str
    snippet: str
    url: str | None = None
    external_ref_type: str | None = None
    external_ref_id: str
    occurred_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float


class SearchResponse(BaseModel):
    results: list[SearchResult]
    warnings: list[dict[str, Any]]
    took_ms: int
