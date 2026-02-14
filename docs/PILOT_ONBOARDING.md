# Pilot Onboarding

## 1) Start stack
```bash
make dev
```
This brings up API, worker, scheduler, frontend, automation-runner, Postgres, and Redis via Docker Compose.

## 2) Create tenant users
- Login as owner (`owner@example.com`) in UI.
- Go to **Admin** and invite admins/members.
- Invitee logs in and uses invite token via `/tenant/members/accept` flow.

## 3) Connect integrations
From **Integrations** page, connect:
- Google
- Slack
- Jira
- Notion
- Microsoft 365

## 4) Configure policies
From **Admin → Policies Hub** configure:
- Email allowlist domains
- Slack allowed channels
- Jira allowed projects
- Notion allowed parent IDs
- Web automation domains/path prefixes and mutation caps
- Calendar settings

## 5) Run first tasks/workflows
- Create tasks from Home page.
- Trigger workflows from Workflows page.
- Verify tool outputs and audit events in run detail.

## 6) Automations + approvals
- Create automation rules.
- Check scheduler outcomes in Automations.
- HIGH-risk steps remain approval-gated.
