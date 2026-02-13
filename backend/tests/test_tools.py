from app.tools import registry


def test_tool_registry_has_valid_schemas():
    tools = registry.list_specs()
    names = {t.name for t in tools}
    assert {"echo_tool", "web_search_stub", "create_note", "gmail_search", "gmail_read", "gmail_draft", "gmail_send", "calendar_find_slots", "calendar_create_event", "calendar_update_event", "calendar_cancel_event", "slack_list_conversations", "slack_fetch_recent_messages", "slack_post_message", "slack_lookup_user_by_email", "slack_open_dm", "jira_list_projects", "jira_search_issues", "jira_get_issue", "jira_create_issue", "jira_add_comment", "jira_transition_issue", "notion_search_pages", "notion_get_page", "notion_sync_page", "unified_search", "ms_mail_search", "ms_mail_read", "ms_mail_draft", "ms_mail_send", "ms_calendar_find_meeting_times", "ms_calendar_create_event", "ms_calendar_update_event", "ms_calendar_cancel_event", "web_automation_run"}.issubset(names)
    for tool in tools:
        assert tool.json_schema["type"] == "object"
        assert "properties" in tool.json_schema
        assert tool.risk_level in {"LOW", "MEDIUM", "HIGH"}
        assert isinstance(tool.idempotent, bool)
