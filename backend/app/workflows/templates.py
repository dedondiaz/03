TEMPLATES = [
    {
        "id": "email_triage_v1",
        "name": "Email Triage + Draft Replies",
        "description": "Summarize recent emails and draft replies; optional gated sending.",
        "version": 1,
        "enabled": True,
        "input_schema_json": {
            "type": "object",
            "properties": {
                "timeframe_days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 30},
                "max_threads": {"type": "integer", "default": 10, "minimum": 1, "maximum": 25},
                "mode": {"type": "string", "enum": ["draft_only", "draft_and_send"], "default": "draft_only"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "id": "schedule_meeting_v1",
        "name": "Schedule a Meeting",
        "description": "Find slots and optionally auto-schedule a meeting with policy-aware risk gates.",
        "version": 1,
        "enabled": True,
        "input_schema_json": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "duration_minutes": {"type": "integer", "minimum": 15, "maximum": 240},
                "attendees_emails": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "time_window_start": {"type": "string"},
                "time_window_end": {"type": "string"},
                "auto_schedule": {"type": "boolean", "default": False},
                "notes": {"type": "string"},
            },
            "required": ["title", "duration_minutes", "attendees_emails", "time_window_start", "time_window_end"],
            "additionalProperties": False,
        },
    },
    {
        "id": "jira_action_items_v1",
        "name": "Turn Action Items into Jira Issues",
        "description": "Use unified search context to extract action items and create Jira tasks safely.",
        "version": 1,
        "enabled": True,
        "input_schema_json": {
            "type": "object",
            "properties": {
                "query_or_text": {"type": "string", "minLength": 1},
                "project_key": {"type": "string"},
                "max_issues": {"type": "integer", "default": 5, "minimum": 1, "maximum": 15},
            },
            "required": ["query_or_text"],
            "additionalProperties": False,
        },
    },
]
