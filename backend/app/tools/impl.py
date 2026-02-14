import hashlib
import json
from datetime import datetime, timedelta
from sqlalchemy import text
from app.integrations.google.calendar_client import CalendarClient
from app.integrations.google.gmail_client import GmailClient
from app.integrations.slack.slack_client import SlackClient
from app.integrations.jira.jira_client import JiraClient
from app.integrations.notion.notion_client import NotionClient
from app.search import SearchService
from app.integrations.microsoft.graph_client import MicrosoftGraphClient
from app.browser_automation import run_web_automation
from .registry import ToolSpec, ToolRegistry

WEB_FIXTURE = [
    {"title": "Tenant Isolation Guide", "snippet": "Best practices for RLS and app-level checks.", "url": "https://example.com/tenant-isolation"},
    {"title": "Audit Logging", "snippet": "Capture every action for compliance.", "url": "https://example.com/audit"},
    {"title": "Approval Gates", "snippet": "High-risk actions should require human approval.", "url": "https://example.com/approval"},
]


def _redact_payload(args: dict) -> dict:
    x = dict(args)
    for key in ["body", "body_text", "description"]:
        if key in x:
            x[key] = "<redacted>"
    if "changes" in x and isinstance(x["changes"], dict):
        y = dict(x["changes"])
        if "description" in y:
            y["description"] = "<redacted>"
        x["changes"] = y
    return x


def _recipient_domains(args: dict) -> set[str]:
    emails = []
    for k in ["to", "cc", "bcc", "attendees"]:
        values = args.get(k) or []
        if values and isinstance(values[0], dict):
            emails.extend([v.get("email", "") for v in values])
        else:
            emails.extend(values)
    out = set()
    for email in emails:
        if "@" in email:
            out.add(email.split("@", 1)[1].lower())
    return out


def _policy_allow(db, tenant_id: str) -> set[str]:
    row = db.execute(text("SELECT allowed_email_domains FROM tenant_policies WHERE tenant_id=:tenant_id"), {"tenant_id": tenant_id}).fetchone()
    return set((row.allowed_email_domains if row else []) or [])


def _external_risk(args: dict, ctx: dict) -> str:
    allow = _policy_allow(ctx["db"], ctx["tenant_id"])
    user_domain = (ctx.get("subject_email") or "").split("@")[-1].lower() if ctx.get("subject_email") and "@" in ctx.get("subject_email") else ""
    domains = _recipient_domains(args)
    if not domains:
        return "MEDIUM"
    return "HIGH" if any(d != user_domain and d not in allow for d in domains) else "MEDIUM"


def echo_tool(args: dict, ctx: dict) -> dict:
    return {"text": args["text"]}


def web_search_stub(args: dict, ctx: dict) -> dict:
    query = args["query"].lower()
    results = [item for item in WEB_FIXTURE if query in item["title"].lower() or query in item["snippet"].lower()]
    return {"results": results or WEB_FIXTURE[:2]}


def create_note(args: dict, ctx: dict) -> dict:
    row = ctx["db"].execute(
        text("INSERT INTO notes (tenant_id, title, body, created_by) VALUES (:tenant_id, :title, :body, :created_by) RETURNING id"),
        {"tenant_id": ctx["tenant_id"], "title": args["title"], "body": args["body"], "created_by": ctx["user_id"]},
    ).fetchone()
    return {"note_id": str(row.id)}


def gmail_search(args: dict, ctx: dict) -> dict:
    return GmailClient(ctx["db"]).search(args["query"], args.get("max_results", 10))


def gmail_read(args: dict, ctx: dict) -> dict:
    return GmailClient(ctx["db"]).read(args["message_id"])


def gmail_draft(args: dict, ctx: dict) -> dict:
    return GmailClient(ctx["db"]).create_draft(args["to"], args["subject"], args["body_text"], args.get("cc"), args.get("bcc"))


def gmail_send_idempotency(run_id: str, args: dict) -> str:
    recipients = sorted((args.get("to") or []) + (args.get("cc") or []) + (args.get("bcc") or []))
    payload = json.dumps({"draft_id": args.get("draft_id", ""), "recipients": recipients, "subject": args.get("subject", "")}, sort_keys=True)
    return hashlib.sha256(f"{run_id}:gmail_send:{payload}".encode()).hexdigest()


def gmail_send(args: dict, ctx: dict) -> dict:
    return GmailClient(ctx["db"]).send(draft_id=args.get("draft_id"), to=args.get("to"), subject=args.get("subject"), body_text=args.get("body_text"), cc=args.get("cc"), bcc=args.get("bcc"))


def _calendar_settings(db, tenant_id: str) -> dict:
    row = db.execute(text("SELECT timezone, work_start, work_end, work_days, slot_granularity_minutes, meeting_buffer_minutes, default_calendar_id FROM tenant_calendar_settings WHERE tenant_id=:tenant_id"), {"tenant_id": tenant_id}).fetchone()
    if not row:
        return {"timezone": "Asia/Kolkata", "work_start": "10:00", "work_end": "18:00", "work_days": [1,2,3,4,5], "slot_granularity_minutes": 15, "meeting_buffer_minutes": 10, "default_calendar_id": "primary"}
    return {"timezone": row.timezone, "work_start": str(row.work_start), "work_end": str(row.work_end), "work_days": row.work_days, "slot_granularity_minutes": row.slot_granularity_minutes, "meeting_buffer_minutes": row.meeting_buffer_minutes, "default_calendar_id": row.default_calendar_id}


def calendar_find_slots(args: dict, ctx: dict) -> dict:
    client = CalendarClient(ctx["db"])
    settings = _calendar_settings(ctx["db"], ctx["tenant_id"])
    timezone = args.get("timezone") or settings["timezone"]
    time_min = datetime.fromisoformat(args["time_min"].replace("Z", "+00:00"))
    time_max = datetime.fromisoformat(args["time_max"].replace("Z", "+00:00"))
    duration = int(args["duration_minutes"])
    max_slots = int(args.get("max_slots", 10))
    buffer_mins = int(settings["meeting_buffer_minutes"])
    granularity = int(settings["slot_granularity_minutes"])

    items = [{"id": settings["default_calendar_id"]}] + [{"id": e} for e in (args.get("attendees") or [])]
    fb = client.freebusy({"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(), "timeZone": timezone, "items": items})
    busy_ranges = []
    for cal in (fb.get("calendars") or {}).values():
        for b in cal.get("busy", []):
            bs = datetime.fromisoformat(b["start"].replace("Z", "+00:00")) - timedelta(minutes=buffer_mins)
            be = datetime.fromisoformat(b["end"].replace("Z", "+00:00")) + timedelta(minutes=buffer_mins)
            busy_ranges.append((bs, be))

    slots = []
    cursor = time_min
    while cursor + timedelta(minutes=duration) <= time_max and len(slots) < max_slots:
        end = cursor + timedelta(minutes=duration)
        if cursor.isoweekday() in settings["work_days"]:
            ws_h, ws_m = map(int, settings["work_start"].split(":"))
            we_h, we_m = map(int, settings["work_end"].split(":"))
            inside_hours = (cursor.hour, cursor.minute) >= (ws_h, ws_m) and (end.hour, end.minute) <= (we_h, we_m)
            overlaps = any(not (end <= bs or cursor >= be) for bs, be in busy_ranges)
            if inside_hours and not overlaps:
                slots.append({"start": cursor.isoformat(), "end": end.isoformat(), "confidence": "high"})
        cursor += timedelta(minutes=granularity)
    return {"slots": slots}


def calendar_create_event(args: dict, ctx: dict) -> dict:
    client = CalendarClient(ctx["db"])
    settings = _calendar_settings(ctx["db"], ctx["tenant_id"])
    timezone = args.get("timezone") or settings["timezone"]
    calendar_id = args.get("calendar_id") or settings["default_calendar_id"]
    payload = {
        "summary": args["title"],
        "start": {"dateTime": args["start"], "timeZone": timezone},
        "end": {"dateTime": args["end"], "timeZone": timezone},
        "attendees": [{"email": a} for a in (args.get("attendees") or [])],
        "location": args.get("location"),
        "description": args.get("description"),
    }
    if args.get("conference", True):
        payload["conferenceData"] = {"createRequest": {"requestId": hashlib.md5((args['title']+args['start']).encode()).hexdigest()}}
    event = client.create_event(calendar_id, payload)
    return {"event_id": event.get("id"), "calendar_id": calendar_id}


def calendar_create_risk(args: dict, ctx: dict) -> str:
    return _external_risk(args, ctx)


def calendar_create_idem(run_id: str, args: dict) -> str:
    payload = json.dumps({"start": args.get("start"), "end": args.get("end"), "title": args.get("title"), "attendees": sorted(args.get("attendees") or []), "calendar_id": args.get("calendar_id", "")}, sort_keys=True)
    return hashlib.sha256(f"{run_id}:calendar_create:{payload}".encode()).hexdigest()


def calendar_update_event(args: dict, ctx: dict) -> dict:
    client = CalendarClient(ctx["db"])
    settings = _calendar_settings(ctx["db"], ctx["tenant_id"])
    calendar_id = args.get("calendar_id") or settings["default_calendar_id"]
    changes = dict(args.get("changes") or {})
    if "attendees" in changes and changes["attendees"] and isinstance(changes["attendees"][0], str):
        changes["attendees"] = [{"email": a} for a in changes["attendees"]]
    event = client.update_event(calendar_id, args["event_id"], changes)
    return {"event_id": event.get("id", args["event_id"]), "calendar_id": calendar_id}


def calendar_update_risk(args: dict, ctx: dict) -> str:
    changes = args.get("changes") or {}
    if "attendees" not in changes:
        return "MEDIUM"
    normalized = {"attendees": [a if isinstance(a, str) else a.get("email", "") for a in changes.get("attendees", [])]}
    return _external_risk(normalized, ctx)


def calendar_update_idem(run_id: str, args: dict) -> str:
    return hashlib.sha256(f"{run_id}:calendar_update:{args.get('event_id')}:{json.dumps(args.get('changes', {}), sort_keys=True)}".encode()).hexdigest()


def calendar_cancel_event(args: dict, ctx: dict) -> dict:
    client = CalendarClient(ctx["db"])
    settings = _calendar_settings(ctx["db"], ctx["tenant_id"])
    calendar_id = args.get("calendar_id") or settings["default_calendar_id"]
    out = client.delete_event(calendar_id, args["event_id"])
    out["calendar_id"] = calendar_id
    return out


def calendar_cancel_idem(run_id: str, args: dict) -> str:
    return hashlib.sha256(f"{run_id}:calendar_cancel:{args.get('event_id')}".encode()).hexdigest()



def _slack_policy(db, tenant_id: str) -> dict:
    row = db.execute(text("SELECT allowed_channel_ids, allow_external_shared FROM tenant_slack_policies WHERE tenant_id=:tenant_id"), {"tenant_id": tenant_id}).fetchone()
    if not row:
        return {"allowed_channel_ids": [], "allow_external_shared": False}
    return {"allowed_channel_ids": row.allowed_channel_ids or [], "allow_external_shared": row.allow_external_shared}


def _truncate_text(v: str, n: int = 180) -> str:
    return (v[:n] + "...") if v and len(v) > n else (v or "")


def slack_list_conversations(args: dict, ctx: dict) -> dict:
    types = args.get("types") or ["public_channel", "private_channel", "im", "mpim"]
    out = SlackClient(ctx["db"]).conversations_list(types=",".join(types), limit=min(int(args.get("limit", 100)), 200), cursor=args.get("cursor"))
    channels = []
    for ch in out.get("channels", []):
        channels.append({"id": ch.get("id"), "name": ch.get("name"), "is_private": ch.get("is_private"), "is_im": ch.get("is_im"), "is_mpim": ch.get("is_mpim"), "is_ext_shared": ch.get("is_ext_shared")})
    return {"channels": channels, "next_cursor": ((out.get("response_metadata") or {}).get("next_cursor"))}


def slack_fetch_recent_messages(args: dict, ctx: dict) -> dict:
    out = SlackClient(ctx["db"]).conversations_history(channel_id=args["channel_id"], limit=min(int(args.get("limit", 20)), 20), cursor=args.get("cursor"))
    msgs = []
    for m in out.get("messages", []):
        msgs.append({"ts": m.get("ts"), "user": m.get("user"), "bot_id": m.get("bot_id"), "text": _truncate_text(m.get("text", "")), "thread_ts": m.get("thread_ts")})
    return {"messages": msgs, "next_cursor": ((out.get("response_metadata") or {}).get("next_cursor"))}


def slack_post_risk(args: dict, ctx: dict) -> str:
    c = SlackClient(ctx["db"])
    info = c.conversations_info(args["channel_id"])
    ch = (info.get("channel") or {})
    policy = _slack_policy(ctx["db"], ctx["tenant_id"])
    if ch.get("is_ext_shared"):
        return "HIGH"
    if args["channel_id"] not in set(policy["allowed_channel_ids"]):
        return "HIGH"
    return "MEDIUM"


def slack_post_idem(run_id: str, args: dict) -> str:
    text_hash = hashlib.sha256((args.get("text") or "").encode()).hexdigest()
    return hashlib.sha256(f"{run_id}:{args.get('channel_id')}:{args.get('thread_ts','')}:{text_hash}".encode()).hexdigest()


def slack_post_message(args: dict, ctx: dict) -> dict:
    text = args.get("text", "")
    out = SlackClient(ctx["db"]).chat_post_message(channel_id=args["channel_id"], text=text, thread_ts=args.get("thread_ts"))
    return {"channel_id": out.get("channel"), "ts": out.get("ts")}


def slack_lookup_user_by_email(args: dict, ctx: dict) -> dict:
    out = SlackClient(ctx["db"]).users_lookup_by_email(args["email"])
    user = out.get("user") or {}
    profile = user.get("profile") or {}
    return {"user_id": user.get("id"), "display_name": profile.get("display_name") or profile.get("real_name")}


def slack_open_dm_idem(run_id: str, args: dict) -> str:
    return hashlib.sha256(f"{run_id}:{args.get('user_id')}:open_dm".encode()).hexdigest()


def slack_open_dm(args: dict, ctx: dict) -> dict:
    out = SlackClient(ctx["db"]).conversations_open(args["user_id"])
    ch = out.get("channel") or {}
    return {"channel_id": ch.get("id")}


def _jira_policy(db, tenant_id: str) -> dict:
    row = db.execute(text("SELECT allowed_project_keys, allow_write FROM tenant_jira_policies WHERE tenant_id=:tenant_id"), {"tenant_id": tenant_id}).fetchone()
    if not row:
        return {"allowed_project_keys": [], "allow_write": True}
    return {"allowed_project_keys": row.allowed_project_keys or [], "allow_write": row.allow_write}


def jira_list_projects(args: dict, ctx: dict) -> dict:
    out = JiraClient(ctx["db"]).list_projects()
    projects = [{"id": p.get("id"), "key": p.get("key"), "name": p.get("name")} for p in out.get("values", [])]
    return {"projects": projects}


def jira_search_issues(args: dict, ctx: dict) -> dict:
    out = JiraClient(ctx["db"]).search_issues(args["jql"], min(int(args.get("max_results", 20)), 50), args.get("fields"))
    issues = []
    for it in out.get("issues", []):
        f = it.get("fields") or {}
        issues.append({"key": it.get("key"), "summary": _truncate_text(f.get("summary", ""), 160), "status": ((f.get("status") or {}).get("name")), "assignee": ((f.get("assignee") or {}).get("displayName")), "updated": f.get("updated")})
    return {"issues": issues, "total": out.get("total")}


def jira_get_issue(args: dict, ctx: dict) -> dict:
    out = JiraClient(ctx["db"]).get_issue(args["issue_key"], args.get("fields"))
    f = out.get("fields") or {}
    if "description" in f:
        f["description"] = "<redacted>"
    return {"key": out.get("key"), "fields": f}


def jira_create_risk(args: dict, ctx: dict) -> str:
    pol = _jira_policy(ctx["db"], ctx["tenant_id"])
    if not pol.get("allow_write", True):
        return "HIGH"
    allowed = set(pol.get("allowed_project_keys") or [])
    return "MEDIUM" if args.get("project_key", "").upper() in allowed else "HIGH"


def jira_create_idem(run_id: str, args: dict) -> str:
    return hashlib.sha256(f"{run_id}:{args.get('project_key')}:{args.get('issue_type')}:{hashlib.sha256((args.get('summary') or '').encode()).hexdigest()}:{hashlib.sha256((args.get('description') or '').encode()).hexdigest()}".encode()).hexdigest()


def jira_create_issue(args: dict, ctx: dict) -> dict:
    out = JiraClient(ctx["db"]).create_issue(args["project_key"], args["issue_type"], args["summary"], args.get("description"), args.get("priority"), None, args.get("labels"))
    return {"issue_key": out.get("key")}


def _issue_project_key(issue_key: str) -> str:
    return issue_key.split("-")[0].upper() if "-" in issue_key else issue_key.upper()


def jira_comment_risk(args: dict, ctx: dict) -> str:
    pol = _jira_policy(ctx["db"], ctx["tenant_id"])
    if not pol.get("allow_write", True):
        return "HIGH"
    key = _issue_project_key(args.get("issue_key", ""))
    return "MEDIUM" if key in set(pol.get("allowed_project_keys") or []) else "HIGH"


def jira_comment_idem(run_id: str, args: dict) -> str:
    return hashlib.sha256(f"{run_id}:{args.get('issue_key')}:{hashlib.sha256((args.get('body_text') or '').encode()).hexdigest()}".encode()).hexdigest()


def jira_add_comment(args: dict, ctx: dict) -> dict:
    out = JiraClient(ctx["db"]).add_comment(args["issue_key"], args["body_text"])
    return {"issue_key": args["issue_key"], "comment_id": out.get("id")}


def jira_transition_idem(run_id: str, args: dict) -> str:
    return hashlib.sha256(f"{run_id}:{args.get('issue_key')}:{args.get('transition_id')}".encode()).hexdigest()


def jira_transition_issue(args: dict, ctx: dict) -> dict:
    JiraClient(ctx["db"]).transition_issue(args["issue_key"], args["transition_id"])
    return {"issue_key": args["issue_key"], "transition_id": args["transition_id"]}



def _rich_text_plain(parts: list[dict]) -> str:
    return "".join([p.get("plain_text", "") for p in (parts or [])])


def _notion_title(page: dict) -> str:
    props = (page.get("properties") or {})
    for value in props.values():
        if isinstance(value, dict) and value.get("type") == "title":
            return _rich_text_plain(value.get("title") or []) or "Untitled"
    return "Untitled"


def notion_search_pages(args: dict, ctx: dict) -> dict:
    out = NotionClient(ctx["db"]).search_pages(args.get("query", ""), args.get("page_size", 20), args.get("start_cursor"))
    pages = []
    for p in out.get("results", []):
        pages.append({"page_id": p.get("id"), "title": _notion_title(p), "url": p.get("url"), "last_edited_time": p.get("last_edited_time")})
    return {"pages": pages, "next_cursor": out.get("next_cursor"), "has_more": bool(out.get("has_more"))}


def _extract_block_text(block: dict) -> str:
    block_type = block.get("type")
    value = (block.get(block_type) or {}) if block_type else {}
    for key in ["rich_text", "caption", "title"]:
        if isinstance(value.get(key), list):
            txt = _rich_text_plain(value.get(key))
            if txt:
                return txt
    return ""


def _collect_page_text(client: NotionClient, page_id: str) -> str:
    cursor = None
    lines = []
    while True:
        out = client.retrieve_block_children(page_id, cursor, 100)
        for b in out.get("results", []):
            txt = _extract_block_text(b)
            if txt:
                lines.append(txt)
        if not out.get("has_more"):
            break
        cursor = out.get("next_cursor")
        if not cursor:
            break
    return "\n".join(lines)


def notion_get_page(args: dict, ctx: dict) -> dict:
    client = NotionClient(ctx["db"])
    page = client.retrieve_page(args["page_id"])
    content = _collect_page_text(client, args["page_id"])
    return {"page_id": page.get("id"), "title": _notion_title(page), "url": page.get("url"), "content": _truncate_text(content, 5000)}


def notion_sync_page(args: dict, ctx: dict) -> dict:
    page_data = notion_get_page(args, ctx)
    ctx["db"].execute(text("""
        INSERT INTO notion_documents (tenant_id, notion_page_id, title, content, source_url, last_synced_at, updated_at)
        VALUES (:tenant_id, :page_id, :title, :content, :source_url, now(), now())
        ON CONFLICT (tenant_id, notion_page_id)
        DO UPDATE SET title=EXCLUDED.title, content=EXCLUDED.content, source_url=EXCLUDED.source_url, last_synced_at=now(), updated_at=now()
    """), {"tenant_id": ctx["tenant_id"], "page_id": page_data["page_id"], "title": page_data["title"], "content": page_data["content"], "source_url": page_data["url"]})
    return {"page_id": page_data["page_id"], "title": page_data["title"], "synced": True}


def notion_sync_idem(run_id: str, args: dict) -> str:
    return hashlib.sha256(f"{run_id}:notion_sync:{args.get('page_id','')}".encode()).hexdigest()



def _truncate_query_args(args: dict) -> dict:
    q = str(args.get("query", ""))
    out = dict(args)
    out["query"] = _truncate_text(q, 200)
    return out


def _redact_unified_result(result: dict) -> dict:
    items = []
    for r in (result.get("results") or [])[:10]:
        x = dict(r)
        x["snippet"] = _truncate_text(str(x.get("snippet", "")), 180)
        if isinstance(x.get("metadata"), dict):
            x["metadata"] = {k: _truncate_text(str(v), 80) for k, v in x["metadata"].items() if k in {"from", "channel_id", "project_key", "status", "assignee", "user"}}
        items.append(x)
    return {"results": items, "warnings": (result.get("warnings") or [])[:10], "took_ms": result.get("took_ms")}


def unified_search(args: dict, ctx: dict) -> dict:
    query = str(args.get("query", "")).strip()
    sources = args.get("sources")
    limit = int(args.get("limit", 20))
    service = SearchService(ctx["db"], ctx["tenant_id"])
    out = service.search(query=query, sources=sources, limit=limit)
    return {"results": out["results"], "warnings": out["warnings"]}



def ms_mail_search(args: dict, ctx: dict) -> dict:
    out = MicrosoftGraphClient(ctx["db"]).mail_search(args["query"], args.get("max_results", 10))
    msgs = []
    for m in out.get("value", []):
        sender = (((m.get("from") or {}).get("emailAddress") or {}).get("address"))
        msgs.append({"id": m.get("id"), "from": sender, "subject": m.get("subject"), "snippet": _truncate_text(m.get("bodyPreview", ""), 180), "received_at": m.get("receivedDateTime")})
    return {"messages": msgs}


def ms_mail_read(args: dict, ctx: dict) -> dict:
    m = MicrosoftGraphClient(ctx["db"]).mail_get_message(args["message_id"])
    return {"id": m.get("id"), "headers": {"from": (((m.get("from") or {}).get("emailAddress") or {}).get("address")), "subject": m.get("subject")}, "body_text": _truncate_text((m.get("bodyPreview") or ""), 600), "body_html": None}


def ms_mail_draft(args: dict, ctx: dict) -> dict:
    m = MicrosoftGraphClient(ctx["db"]).mail_create_draft(args.get("to") or [], args.get("cc"), args.get("bcc"), args["subject"], args["body_text"])
    return {"draft_id": m.get("id")}


def ms_mail_send_idem(run_id: str, args: dict) -> str:
    recipients = sorted((args.get("to") or []) + (args.get("cc") or []) + (args.get("bcc") or []))
    payload = json.dumps({"draft_id": args.get("draft_id", ""), "recipients": recipients, "subject": args.get("subject", "")}, sort_keys=True)
    return hashlib.sha256(f"{run_id}:ms_mail_send:{payload}".encode()).hexdigest()


def ms_mail_send_risk(args: dict, ctx: dict) -> str:
    return _external_risk(args, ctx)


def ms_mail_send(args: dict, ctx: dict) -> dict:
    out = MicrosoftGraphClient(ctx["db"]).mail_send(draft_id=args.get("draft_id"), payload=args)
    return {"message_id": out.get("message_id")}


def ms_calendar_find_meeting_times(args: dict, ctx: dict) -> dict:
    out = MicrosoftGraphClient(ctx["db"]).calendar_find_meeting_times(args.get("attendees") or [], args["time_min"], args["time_max"], int(args["duration_minutes"]), args.get("timezone", "UTC"), args.get("max_slots", 5))
    slots=[]
    for s in out.get("meetingTimeSuggestions", []):
        mt = s.get("meetingTimeSlot") or {}
        slots.append({"start": (mt.get("start") or {}).get("dateTime"), "end": (mt.get("end") or {}).get("dateTime"), "confidence": s.get("confidence")})
    return {"slots": slots}


def ms_calendar_create_risk(args: dict, ctx: dict) -> str:
    return _external_risk({"attendees": args.get("attendees") or []}, ctx)


def ms_calendar_create_idem(run_id: str, args: dict) -> str:
    payload = json.dumps({"start": args.get("start"), "end": args.get("end"), "title": args.get("title"), "attendees": sorted(args.get("attendees") or [])}, sort_keys=True)
    return hashlib.sha256(f"{run_id}:ms_calendar_create:{payload}".encode()).hexdigest()


def ms_calendar_create_event(args: dict, ctx: dict) -> dict:
    payload = {
        "subject": args["title"],
        "start": {"dateTime": args["start"], "timeZone": args.get("timezone", "UTC")},
        "end": {"dateTime": args["end"], "timeZone": args.get("timezone", "UTC")},
        "attendees": [{"emailAddress": {"address": x}, "type": "required"} for x in (args.get("attendees") or [])],
        "location": {"displayName": args.get("location") or ""},
        "body": {"contentType": "Text", "content": args.get("description") or ""},
    }
    out = MicrosoftGraphClient(ctx["db"]).calendar_create_event(payload)
    return {"event_id": out.get("id")}


def ms_calendar_update_risk(args: dict, ctx: dict) -> str:
    ch = args.get("changes") or {}
    atts = ch.get("attendees") or []
    if atts and isinstance(atts[0], dict):
        atts = [((a.get("emailAddress") or {}).get("address") or "") for a in atts]
    return _external_risk({"attendees": atts}, ctx) if atts else "MEDIUM"


def ms_calendar_update_idem(run_id: str, args: dict) -> str:
    return hashlib.sha256(f"{run_id}:ms_calendar_update:{args.get('event_id')}:{json.dumps(args.get('changes', {}), sort_keys=True)}".encode()).hexdigest()


def ms_calendar_update_event(args: dict, ctx: dict) -> dict:
    out = MicrosoftGraphClient(ctx["db"]).calendar_update_event(args["event_id"], args.get("changes") or {})
    return {"event_id": args["event_id"], "updated": True, "result": out}




def web_automation_idem(run_id: str, args: dict) -> str:
    payload = json.dumps({"session_id": args.get("session_id"), "steps": args.get("steps", []), "record_trace": bool(args.get("record_trace", False))}, sort_keys=True)
    return hashlib.sha256(f"{run_id}:web_automation_run:{payload}".encode()).hexdigest()


def web_automation_redact_result(result: dict) -> dict:
    x = dict(result or {})
    extracted = x.get("extracted") or {}
    if isinstance(extracted, dict):
        for k, v in list(extracted.items()):
            if isinstance(v, str) and len(v) > 500:
                extracted[k] = v[:500] + "..."
        x["extracted"] = extracted
    return x


def ms_calendar_cancel_idem(run_id: str, args: dict) -> str:
    return hashlib.sha256(f"{run_id}:ms_calendar_cancel:{args.get('event_id')}".encode()).hexdigest()


def ms_calendar_cancel_event(args: dict, ctx: dict) -> dict:
    MicrosoftGraphClient(ctx["db"]).calendar_cancel_event(args["event_id"])
    return {"event_id": args["event_id"], "cancelled": True}

def build_registry() -> ToolRegistry:
    return ToolRegistry([
        ToolSpec(name="echo_tool", description="Echoes back text for deterministic processing.", json_schema={"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=echo_tool),
        ToolSpec(name="web_search_stub", description="Returns deterministic static search results for a query.", json_schema={"type":"object","properties":{"query":{"type":"string"},"max_results":{"type":"integer"}},"required":["query"],"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=web_search_stub),
        ToolSpec(name="create_note", description="Creates a tenant-scoped note entry.", json_schema={"type":"object","properties":{"title":{"type":"string"},"body":{"type":"string"}},"required":["title","body"],"additionalProperties":False}, risk_level="HIGH", idempotent=False, min_role="owner", handler=create_note),
        ToolSpec(name="gmail_search", description="Search Gmail messages.", json_schema={"type":"object","properties":{"query":{"type":"string"},"max_results":{"type":"integer"}},"required":["query"],"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=gmail_search),
        ToolSpec(name="gmail_read", description="Read a Gmail message by id.", json_schema={"type":"object","properties":{"message_id":{"type":"string"}},"required":["message_id"],"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=gmail_read),
        ToolSpec(name="gmail_draft", description="Create a Gmail draft.", json_schema={"type":"object","properties":{"to":{"type":"array","items":{"type":"string"}},"cc":{"type":"array","items":{"type":"string"}},"bcc":{"type":"array","items":{"type":"string"}},"subject":{"type":"string"},"body_text":{"type":"string"}},"required":["to","subject","body_text"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, redact_args=_redact_payload, handler=gmail_draft),
        ToolSpec(name="gmail_send", description="Send Gmail draft or compose+send a message.", json_schema={"type":"object","properties":{"draft_id":{"type":"string"},"to":{"type":"array","items":{"type":"string"}},"subject":{"type":"string"},"body_text":{"type":"string"},"cc":{"type":"array","items":{"type":"string"}},"bcc":{"type":"array","items":{"type":"string"}},},"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, risk_evaluator=_external_risk, idempotency_builder=gmail_send_idempotency, redact_args=_redact_payload, handler=gmail_send),
        ToolSpec(name="calendar_find_slots", description="Find available calendar slots.", json_schema={"type":"object","properties":{"duration_minutes":{"type":"integer","minimum":15},"time_min":{"type":"string"},"time_max":{"type":"string"},"attendees":{"type":"array","items":{"type":"string"}},"timezone":{"type":"string"},"max_slots":{"type":"integer"}},"required":["duration_minutes","time_min","time_max","attendees"],"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=calendar_find_slots),
        ToolSpec(name="calendar_create_event", description="Create calendar event.", json_schema={"type":"object","properties":{"title":{"type":"string"},"start":{"type":"string"},"end":{"type":"string"},"timezone":{"type":"string"},"attendees":{"type":"array","items":{"type":"string"}},"location":{"type":"string"},"description":{"type":"string"},"conference":{"type":"boolean"},"calendar_id":{"type":"string"}},"required":["title","start","end"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, risk_evaluator=calendar_create_risk, idempotency_builder=calendar_create_idem, redact_args=_redact_payload, handler=calendar_create_event),
        ToolSpec(name="calendar_update_event", description="Update calendar event.", json_schema={"type":"object","properties":{"event_id":{"type":"string"},"calendar_id":{"type":"string"},"changes":{"type":"object"}},"required":["event_id","changes"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, risk_evaluator=calendar_update_risk, idempotency_builder=calendar_update_idem, redact_args=_redact_payload, handler=calendar_update_event),
        ToolSpec(name="calendar_cancel_event", description="Cancel calendar event.", json_schema={"type":"object","properties":{"event_id":{"type":"string"},"calendar_id":{"type":"string"},"reason":{"type":"string"}},"required":["event_id"],"additionalProperties":False}, risk_level="HIGH", idempotent=True, idempotency_builder=calendar_cancel_idem, redact_args=_redact_payload, handler=calendar_cancel_event),
        ToolSpec(name="slack_list_conversations", description="List Slack conversations.", json_schema={"type":"object","properties":{"types":{"type":"array","items":{"type":"string"}},"limit":{"type":"integer","maximum":200},"cursor":{"type":"string"}},"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=slack_list_conversations),
        ToolSpec(name="slack_fetch_recent_messages", description="Fetch recent Slack messages from channel.", json_schema={"type":"object","properties":{"channel_id":{"type":"string"},"limit":{"type":"integer","maximum":20},"cursor":{"type":"string"}},"required":["channel_id"],"additionalProperties":False}, risk_level="LOW", idempotent=True, redact_args=_redact_payload, handler=slack_fetch_recent_messages),
        ToolSpec(name="slack_post_message", description="Post message to Slack channel.", json_schema={"type":"object","properties":{"channel_id":{"type":"string"},"text":{"type":"string"},"thread_ts":{"type":"string"}},"required":["channel_id","text"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, risk_evaluator=slack_post_risk, idempotency_builder=slack_post_idem, redact_args=_redact_payload, handler=slack_post_message),
        ToolSpec(name="slack_lookup_user_by_email", description="Lookup Slack user by email.", json_schema={"type":"object","properties":{"email":{"type":"string"}},"required":["email"],"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=slack_lookup_user_by_email),
        ToolSpec(name="slack_open_dm", description="Open Slack DM by user id.", json_schema={"type":"object","properties":{"user_id":{"type":"string"}},"required":["user_id"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, idempotency_builder=slack_open_dm_idem, handler=slack_open_dm),
        ToolSpec(name="jira_list_projects", description="List Jira projects.", json_schema={"type":"object","properties":{},"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=jira_list_projects),
        ToolSpec(name="jira_search_issues", description="Search Jira issues.", json_schema={"type":"object","properties":{"jql":{"type":"string"},"max_results":{"type":"integer","maximum":50},"fields":{"type":"array","items":{"type":"string"}}},"required":["jql"],"additionalProperties":False}, risk_level="LOW", idempotent=True, redact_args=_redact_payload, handler=jira_search_issues),
        ToolSpec(name="jira_get_issue", description="Get Jira issue.", json_schema={"type":"object","properties":{"issue_key":{"type":"string"},"fields":{"type":"array","items":{"type":"string"}}},"required":["issue_key"],"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=jira_get_issue),
        ToolSpec(name="jira_create_issue", description="Create Jira issue.", json_schema={"type":"object","properties":{"project_key":{"type":"string"},"issue_type":{"type":"string"},"summary":{"type":"string"},"description":{"type":"string"},"labels":{"type":"array","items":{"type":"string"}},"priority":{"type":"string"}},"required":["project_key","issue_type","summary"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, risk_evaluator=jira_create_risk, idempotency_builder=jira_create_idem, redact_args=_redact_payload, handler=jira_create_issue),
        ToolSpec(name="jira_add_comment", description="Add Jira comment.", json_schema={"type":"object","properties":{"issue_key":{"type":"string"},"body_text":{"type":"string"}},"required":["issue_key","body_text"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, risk_evaluator=jira_comment_risk, idempotency_builder=jira_comment_idem, redact_args=_redact_payload, handler=jira_add_comment),
        ToolSpec(name="jira_transition_issue", description="Transition Jira issue.", json_schema={"type":"object","properties":{"issue_key":{"type":"string"},"transition_id":{"type":"string"}},"required":["issue_key","transition_id"],"additionalProperties":False}, risk_level="HIGH", idempotent=True, idempotency_builder=jira_transition_idem, redact_args=_redact_payload, handler=jira_transition_issue),
        ToolSpec(name="notion_search_pages", description="Search Notion pages visible to the integration.", json_schema={"type":"object","properties":{"query":{"type":"string"},"page_size":{"type":"integer","maximum":100},"start_cursor":{"type":"string"}},"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=notion_search_pages),
        ToolSpec(name="notion_get_page", description="Retrieve a Notion page and flattened text content.", json_schema={"type":"object","properties":{"page_id":{"type":"string"}},"required":["page_id"],"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=notion_get_page),
        ToolSpec(name="notion_sync_page", description="Sync a Notion page into tenant knowledge base storage.", json_schema={"type":"object","properties":{"page_id":{"type":"string"}},"required":["page_id"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, idempotency_builder=notion_sync_idem, handler=notion_sync_page),
        ToolSpec(name="unified_search", description="Search across connected Gmail, Slack, Jira, and Notion sources.", json_schema={"type":"object","properties":{"query":{"type":"string"},"sources":{"type":"array","items":{"type":"string","enum":["gmail","slack","jira","notion"]}},"limit":{"type":"integer","maximum":50}},"required":["query"],"additionalProperties":False}, risk_level="LOW", idempotent=True, redact_args=_truncate_query_args, redact_result=_redact_unified_result, handler=unified_search),
        ToolSpec(name="ms_mail_search", description="Search Outlook mailbox messages.", json_schema={"type":"object","properties":{"query":{"type":"string"},"max_results":{"type":"integer","maximum":25}},"required":["query"],"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=ms_mail_search),
        ToolSpec(name="ms_mail_read", description="Read Outlook message by id.", json_schema={"type":"object","properties":{"message_id":{"type":"string"}},"required":["message_id"],"additionalProperties":False}, risk_level="LOW", idempotent=True, redact_args=_redact_payload, handler=ms_mail_read),
        ToolSpec(name="ms_mail_draft", description="Create Outlook draft message.", json_schema={"type":"object","properties":{"to":{"type":"array","items":{"type":"string"}},"cc":{"type":"array","items":{"type":"string"}},"bcc":{"type":"array","items":{"type":"string"}},"subject":{"type":"string"},"body_text":{"type":"string"}},"required":["to","subject","body_text"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, redact_args=_redact_payload, handler=ms_mail_draft),
        ToolSpec(name="ms_mail_send", description="Send Outlook draft or compose+send mail.", json_schema={"type":"object","properties":{"draft_id":{"type":"string"},"to":{"type":"array","items":{"type":"string"}},"subject":{"type":"string"},"body_text":{"type":"string"},"cc":{"type":"array","items":{"type":"string"}},"bcc":{"type":"array","items":{"type":"string"}}},"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, risk_evaluator=ms_mail_send_risk, idempotency_builder=ms_mail_send_idem, redact_args=_redact_payload, handler=ms_mail_send),
        ToolSpec(name="ms_calendar_find_meeting_times", description="Find Outlook meeting times.", json_schema={"type":"object","properties":{"attendees":{"type":"array","items":{"type":"string"}},"time_min":{"type":"string"},"time_max":{"type":"string"},"duration_minutes":{"type":"integer","minimum":15},"timezone":{"type":"string"},"max_slots":{"type":"integer","maximum":10}},"required":["attendees","time_min","time_max","duration_minutes"],"additionalProperties":False}, risk_level="LOW", idempotent=True, handler=ms_calendar_find_meeting_times),
        ToolSpec(name="ms_calendar_create_event", description="Create Outlook calendar event.", json_schema={"type":"object","properties":{"title":{"type":"string"},"start":{"type":"string"},"end":{"type":"string"},"timezone":{"type":"string"},"attendees":{"type":"array","items":{"type":"string"}},"location":{"type":"string"},"description":{"type":"string"},"conference":{"type":"boolean"}},"required":["title","start","end"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, risk_evaluator=ms_calendar_create_risk, idempotency_builder=ms_calendar_create_idem, redact_args=_redact_payload, handler=ms_calendar_create_event),
        ToolSpec(name="ms_calendar_update_event", description="Update Outlook calendar event.", json_schema={"type":"object","properties":{"event_id":{"type":"string"},"changes":{"type":"object"}},"required":["event_id","changes"],"additionalProperties":False}, risk_level="MEDIUM", idempotent=True, risk_evaluator=ms_calendar_update_risk, idempotency_builder=ms_calendar_update_idem, redact_args=_redact_payload, handler=ms_calendar_update_event),
        ToolSpec(name="ms_calendar_cancel_event", description="Cancel Outlook calendar event.", json_schema={"type":"object","properties":{"event_id":{"type":"string"},"reason":{"type":"string"}},"required":["event_id"],"additionalProperties":False}, risk_level="HIGH", idempotent=True, idempotency_builder=ms_calendar_cancel_idem, redact_args=_redact_payload, handler=ms_calendar_cancel_event),
        ToolSpec(name="web_automation_run", description="Execute policy-constrained browser automation via Playwright runner.", json_schema={"type":"object","properties":{"session_id":{"type":"string"},"record_trace":{"type":"boolean"},"steps":{"type":"array","items":{"type":"object"}}},"required":["steps"],"additionalProperties":False}, risk_level="HIGH", idempotent=True, idempotency_builder=web_automation_idem, redact_args=_redact_payload, redact_result=web_automation_redact_result, handler=run_web_automation),
    ])
