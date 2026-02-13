# Prioritized backlog

## P0
- [x] Implement FastAPI auth + sessions + membership roles.
- [x] Implement tenant switch endpoint.
- [x] Implement Postgres tables with tenant_id and RLS policies.
- [x] Implement task create/list APIs.
- [x] Implement run pipeline with Planner/Executor/Verifier and audit logs.
- [x] Implement high-risk approval gate and approvals table.
- [x] Implement RLS isolation and permission-gate tests.
- [x] Add docker-compose for Postgres + Redis.
- [x] Add one-command local start path.

## P1
- [x] Add Notion connector (docs sync + knowledge base ingestion).
- [ ] Add richer RBAC (custom roles/permissions per tool).
- [ ] Add Workflow packs (Email triage + Scheduling + Jira ticket creation) built on unified_search.
- [ ] Add retry/backoff and dead-letter queue for worker jobs.
- [x] Add Workflow Automations (scheduled/triggered runs + follow-up tracking).
- [x] Add Microsoft 365 connector (Outlook/Calendar).
- [ ] Add Playwright browser automation layer (HIGH-risk by default + allowlisted domains + evidence capture).
- [ ] Add structured logging + log shipping.

## P2
- [ ] Add per-tenant rate limits and quotas.
- [ ] Add encrypted object storage for artifacts.
- [ ] Add admin console for audit investigations.

- [ ] Browser automation follow-ups: observability dashboards, quota enforcement/billing, admin console controls.

- [ ] Next: observability dashboards + integration health page.

- [ ] Next: Admin essentials (member management, audit export, usage dashboard polish), then pilot hardening.

- [ ] Next: Pilot hardening sprint (no new features), onboarding docs, and performance/index pass.
