import json


def compile_workflow_task(template_id: str, template_name: str, workflow_input: dict) -> dict:
    if template_id == "email_triage_v1":
        mode = workflow_input.get("mode", "draft_only")
        send_clause = (
            "Do NOT call gmail_send in this workflow. Produce drafts only." if mode == "draft_only" else
            "If mode is draft_and_send, call gmail_send only when policy/risk gates allow; otherwise stop with pending approval details."
        )
        description = (
            "WORKFLOW: Email Triage + Draft Replies\n"
            "1) Use unified_search first (sources=[gmail], include recent context).\n"
            "2) Use gmail_search and gmail_read for top threads in timeframe.\n"
            "3) Create gmail_draft replies with concise professional tone.\n"
            f"4) {send_clause}\n"
            "Output JSON only: {drafted:[], pending_approval:[], skipped:[], next_actions:[]}.\n"
            f"Input: {json.dumps(workflow_input, sort_keys=True)}"
        )
    elif template_id == "schedule_meeting_v1":
        description = (
            "WORKFLOW: Schedule a Meeting\n"
            "1) Use calendar_find_slots with duration/window/attendees from input.\n"
            "2) Return top 3 slot options.\n"
            "3) If auto_schedule=true, attempt calendar_create_event for best slot; let risk/approval gates decide if blocked.\n"
            "Output JSON only: {proposed_slots:[], created_event:null|{}, pending_approval:null|{}, next_actions:[]}.\n"
            f"Input: {json.dumps(workflow_input, sort_keys=True)}"
        )
    elif template_id == "jira_action_items_v1":
        description = (
            "WORKFLOW: Turn Action Items into Jira Issues\n"
            "1) Use unified_search first with sources=[slack,gmail,notion,jira].\n"
            "2) Extract concrete action items from query/context.\n"
            "3) Create jira_create_issue for each action item (issue_type=Task), include source refs in description.\n"
            "4) If project_key missing and cannot be inferred safely, return needs_input for project_key.\n"
            "Output JSON only: {created:[], pending_approval:[], needs_input:[], source_refs:[]}.\n"
            f"Input: {json.dumps(workflow_input, sort_keys=True)}"
        )
    else:
        raise ValueError("unknown_template")

    return {
        "title": f"Workflow: {template_name}",
        "description": description,
        "risk_level": "LOW",
    }
