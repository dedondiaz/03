# Multi-tenant AI assistant platform

A full-stack local dev implementation with strict tenant isolation, approval gates, and auditable LLM-driven tool execution.

## Stack
- Frontend: Next.js (TypeScript)
- Backend: FastAPI (Python)
- DB: Postgres with RLS
- Queue/worker: Redis + RQ
- LLM: Fake mode (default) or OpenAI
- Integrations: Google OAuth + Gmail + Calendar, Slack OAuth + tools, Jira OAuth + tools, Notion OAuth + docs ingestion, Microsoft 365 OAuth + Outlook Mail/Calendar tools

## Required environment variables
- `LLM_MODE=fake|openai` (default: `fake`)
- `OPENAI_API_KEY` (required when `LLM_MODE=openai`)
- `OPENAI_MODEL` (default: `gpt-4o-mini`)
- `LLM_TIMEOUT_S` (default: `20`)
- `LLM_RETRIES` (default: `2`)
- `TOOL_CALL_TIMEOUT_S` (default: `8`)
- `RUN_MAX_STEPS` (default: `10`)
- `APP_ENCRYPTION_KEY` (required; URL-safe base64 key for 32 raw bytes)
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REDIRECT_URI` (e.g., `http://localhost:8000/integrations/google/callback`)
- `SLACK_CLIENT_ID`
- `SLACK_CLIENT_SECRET`
- `SLACK_REDIRECT_URI` (e.g., `http://localhost:8000/integrations/slack/callback`)
- `SLACK_SCOPES` (default: `channels:read,groups:read,chat:write,users:read.email`)
- `JIRA_CLIENT_ID`
- `JIRA_CLIENT_SECRET`
- `JIRA_REDIRECT_URI` (e.g., `http://localhost:8000/integrations/jira/callback`)
- `JIRA_SCOPES` (default: `read:jira-work,write:jira-work,read:jira-user,offline_access`)
- `NOTION_CLIENT_ID`
- `NOTION_CLIENT_SECRET`
- `NOTION_REDIRECT_URI` (e.g., `http://localhost:8000/integrations/notion/callback`)
- `MICROSOFT_CLIENT_ID`
- `MICROSOFT_CLIENT_SECRET`
- `MICROSOFT_REDIRECT_URI` (e.g., `http://localhost:8000/integrations/microsoft/callback`)
- `MICROSOFT_SCOPES` (default: `offline_access User.Read Mail.Read Mail.Send Calendars.ReadWrite`)

Google scopes used:
- `openid`
- `email`
- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/gmail.compose`
- `https://www.googleapis.com/auth/gmail.send`
- `https://www.googleapis.com/auth/calendar`
- `https://www.googleapis.com/auth/calendar.events`

## Quick start
1. Start everything with one command:
   ```bash
   make dev
   ```
2. Open frontend at `http://localhost:3000`
3. Backend API at `http://localhost:8000`

## Running tests
### Local python path (optional)
```bash
PYTHONPATH=backend pytest backend/tests -q
```

### Containerized test runner (no local pip required)
```bash
make test-docker
```

This builds `backend/Dockerfile` and runs tests in the `backend-test` service via docker compose.

## Core API additions
- `GET /tenant/calendar-settings`
- `PUT /tenant/calendar-settings`
- `POST /integrations/google/connect`
- `GET /integrations/google/callback`
- `GET /integrations/google/status`
- `POST /integrations/google/disconnect`
- `POST /integrations/slack/connect`
- `GET /integrations/slack/callback`
- `GET /integrations/slack/status`
- `POST /integrations/slack/disconnect`
- `GET /tenant/slack-policy`
- `PUT /tenant/slack-policy`
- `POST /integrations/jira/connect`
- `GET /integrations/jira/callback`
- `GET /integrations/jira/status`
- `POST /integrations/jira/disconnect`
- `GET /tenant/jira-policy`
- `PUT /tenant/jira-policy`
- `POST /integrations/notion/connect`
- `GET /integrations/notion/callback`
- `GET /integrations/notion/status`
- `POST /integrations/notion/disconnect`
- `GET /knowledge/notion-docs`
- `POST /integrations/microsoft/connect`
- `GET /integrations/microsoft/callback`
- `GET /integrations/microsoft/status`
- `POST /integrations/microsoft/disconnect`
- `GET /search`
- `GET /search/sources`
- `GET /workflows/templates`
- `POST /workflows/runs`
- `GET /workflows/runs`
- `GET /workflows/runs/{id}`
- `GET /automations/rules`
- `POST /automations/rules`
- `GET /automations/rules/{id}`
- `PUT /automations/rules/{id}`
- `POST /automations/rules/{id}/toggle`
- `GET /automations/executions`

## Security model
- All tenant-owned tables include `tenant_id`
- FastAPI derives tenant context from server-side session token (`sessions` table)
- Postgres RLS policies enforce `tenant_id = current_setting('app.tenant_id')`
- OAuth credentials and tenant settings are tenant-scoped and RLS-protected
- OAuth state is stored server-side and validated on callback
- Access/refresh tokens are encrypted at rest (application-level)
- High-risk sends/cancels require explicit approval


Slack scopes used (minimum):
- `channels:read`
- `groups:read`
- `chat:write`
- `users:read.email`


Jira scopes used (minimum):
- `read:jira-work`
- `write:jira-work`
- `read:jira-user`
- `offline_access`

Jira API base uses cloudId discovered from `accessible-resources`:
- `https://api.atlassian.com/ex/jira/{cloudId}/rest/api/3/...`


## Unified Search
- Use `GET /search?q=<query>&sources=gmail,slack,jira,notion&limit=20` to run tenant-scoped merged search across connected sources.
- Use `GET /search/sources` to inspect connection state and blockers (for example Slack channel allowlist not configured).
- Slack v1 behavior: searches recent messages from `tenant_slack_policies.allowed_channel_ids` (up to 10 channels, recent window), no `search.messages` scope required.
- Search is read-only and returns warnings for partial failures or disconnected sources instead of failing the full request.


## Workflow Packs
- Use the Workflows page or API to launch high-value workflow templates that compile into structured task runs.
- Templates in v1:
  - `email_triage_v1`: summarize recent emails and create reply drafts (draft-only by default).
  - `schedule_meeting_v1`: find slots and optionally auto-create calendar events.
  - `jira_action_items_v1`: extract action items and create Jira tasks from unified search context.
- Approval model is preserved: if any compiled plan hits HIGH-risk tools, the linked run enters `PENDING_APPROVAL` and workflow status maps to `waiting_approval`.
- Workflow runs are tenant-scoped and linked to existing run details for full auditability.


## Workflow Automations
- Create tenant-scoped automation rules over workflow templates with cron schedules, timezone-aware next-run computation, quiet hours, daily quota, and concurrency limits.
- Cron examples:
  - Weekdays 9am: `0 9 * * 1-5`
  - Daily 8am: `0 8 * * *`
- Quiet hours: if a run becomes due during quiet hours, scheduler records `skipped_quiet_hours` and advances `next_run_at`.
- Guardrails: `max_runs_per_day` and `max_concurrent_runs` prevent runaway execution.
- Safety: scheduler enqueues workflow runs only; existing approval gates still block HIGH-risk tool calls (no auto-approve).
- Audit trail: each scheduler decision writes `automation_executions` and audit log events with linked workflow/run references.


Microsoft 365 (Graph) scopes used (minimum):
- `offline_access`
- `User.Read`
- `Mail.Read`
- `Mail.Send`
- `Calendars.ReadWrite`

## Browser automation (Playwright runner)
- Configure tenant allowlists via `GET/PUT /tenant/automation-policy`.
- Upload encrypted Playwright `storage_state` JSON via `POST /automation/sessions` (owner only).
- `web_automation_run` is always HIGH risk and requires approval.
- Evidence is persisted as screenshots (and optional trace) and can be fetched from automation run artifact endpoints.


## Reliability guardrails
- Per-tenant plans and daily usage metering are enforced for API requests, tool calls, workflow/automation runs, LLM token estimates, and web automation runtime.
- API rate limits are tenant-scoped and return HTTP 429 with Retry-After.
- Run cancellation APIs: `POST /runs/{id}/cancel`, `POST /workflows/runs/{id}/cancel`.
- Update pilot plan limits in `tenant_plans.limits_json` (seeded in DB init).


## Ops observability
- Ops endpoints (owner): `GET /ops/metrics/summary`, `GET /ops/tenants/health`, `GET /ops/runs/recent`.
- Integration health tracks: `connected`, `last_success_at`, `last_error_at`, `last_error_code`, redacted message, and `consecutive_failures`.
- Health data is tenant-scoped and protected by RLS.


## Admin essentials
- Member management: `GET /tenant/members`, `POST /tenant/members/invite`, `POST /tenant/members/accept`, `PUT/DELETE /tenant/members/{user_id}`.
- Unified Policy Hub: `GET/PUT /tenant/policies` for consolidated guardrails.
- Audit exports: `GET /audit/list` and `GET /audit/export` (CSV, tenant-scoped, redacted).
- Usage dashboard API: `GET /tenant/usage/summary?days=7`.


## Pilot hardening operations
- Start full stack (Docker): `make dev`
- Run containerized backend tests: `make test-docker`
- Ops endpoints: `/ops/metrics/summary`, `/ops/tenants/health`, `/ops/runs/recent`
- Admin endpoints: `/tenant/members*`, `/tenant/policies`, `/audit/list`, `/audit/export`, `/tenant/usage/summary`
- Runbooks: `docs/PILOT_ONBOARDING.md`, `docs/RUNBOOK.md`, `docs/Known_Limitations.md`
