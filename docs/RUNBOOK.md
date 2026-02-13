# Pilot Runbook

## Queue backlog / DLQ
- Inspect Redis queues (`runs`, `runs_dlq`).
- `GET /ops/metrics/summary` shows queue counts.
- If `runs_dlq` grows, inspect failed run reasons via `/ops/tenants/health` and `/audit/list`.

## Integration revoked / scope missing
- Symptoms: `*_not_connected` or API failures.
- Action: disconnect + reconnect integration from Integrations page.
- Validate health in `/ops` integration cards.

## Token refresh failures
- Symptoms: repeated integration failures.
- Action: reconnect integration, verify OAuth app scopes and redirect URIs.

## Web automation runner down
- Symptoms: `automation_runner_unavailable`.
- Action: check `automation-runner` container health/logs and shared artifacts volume.

## Quota exceeded
- Symptoms: HTTP 429 / quota errors in runs.
- Action: review `/tenant/usage/summary?days=7`, adjust plan limits (admin DB op), retry after reset.

## Audit export for debugging
- Use `/audit/list` filters first.
- Use `/audit/export` for CSV (tenant scoped, redacted).
- Avoid sharing raw DB dumps; use redacted exports only.
