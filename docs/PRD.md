# PRD: Multi-tenant AI Assistant Platform

## Goal
Provide a secure SaaS assistant runtime where each tenant can create and run tasks while preserving strict tenant isolation and complete auditability.

## Users
- Tenant Owner: manages tenant, can switch tenant and approve high-risk runs
- Tenant Member: can create/list tasks and run low/medium risk tasks

## Functional requirements
1. Auth/session and tenant memberships with owner/member roles.
2. Tenant switch using verified active session and membership check.
3. Create/list tasks with risk level.
4. Run task through Planner -> Executor -> Verifier pipeline.
5. High-risk runs blocked until explicit approval flag is provided.
6. Persist audit events for every stage and error.

## Security requirements
- Every tenant-scoped table must include `tenant_id`.
- API must derive tenant from verified session; never trust request payload for tenant selection.
- Enforce RLS in Postgres for defense in depth.
- Log tenant_id in run events and queue/cache keys.

## Non-functional requirements
- Local development via docker-compose.
- Automated tests for RLS isolation and permission gates.
