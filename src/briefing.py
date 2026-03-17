"""
Phase 5 — Daily Briefing System
Generates an executive-style morning briefing from the local database.
No API calls — everything comes from SQLite.

Run standalone:  python src/briefing.py
Or with flag:    python src/briefing.py --post   (also posts to Notion)
"""

import os
import sys
import json
import sqlite3
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

DB_PATH          = Path(__file__).parent.parent / "data" / "db" / "emails.db"
NOTION_TOKEN     = os.environ.get("NOTION_TOKEN", "")
NOTION_DIGEST_DB = os.environ.get("NOTION_DIGEST_DB", "")
NOTION_API       = "https://api.notion.com/v1"
NOTION_VERSION   = "2022-06-28"


# ── Data fetchers ─────────────────────────────────────────────────────────────

def get_priority_emails(conn: sqlite3.Connection, hours: int = 24) -> list[dict]:
    """Fetch high/medium emails analyzed in the last N hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute("""
        SELECT from_addr, subject, priority, category, summary,
               action_items, reply_needed, reply_urgency, job_opportunity, received_at
        FROM analyzed_emails
        WHERE priority IN ('high', 'medium')
          AND analyzed_at >= ?
        ORDER BY
            CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
            received_at DESC
    """, (since,)).fetchall()
    return [{
        "from":           r[0], "subject":      r[1], "priority":      r[2],
        "category":       r[3], "summary":       r[4],
        "action_items":   json.loads(r[5] or "[]"),
        "reply_needed":   bool(r[6]), "reply_urgency": r[7],
        "job_opportunity": bool(r[8]), "received_at":  r[9],
    } for r in rows]


def get_job_emails(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    """Fetch all job-flagged emails from the last N days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT from_addr, subject, summary, received_at
        FROM analyzed_emails
        WHERE job_opportunity = 1
          AND analyzed_at >= ?
        ORDER BY received_at DESC
    """, (since,)).fetchall()
    return [{"from": r[0], "subject": r[1], "summary": r[2], "received_at": r[3]}
            for r in rows]


def get_open_tasks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT id, source, task, due_date FROM tasks
        WHERE status = 'open'
        ORDER BY due_date ASC NULLS LAST, created_at ASC
    """).fetchall()
    return [{"id": r[0], "source": r[1], "task": r[2], "due_date": r[3]} for r in rows]


def get_pending_replies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT id, from_addr, subject, urgency, received_at
        FROM pending_replies WHERE status = 'pending'
        ORDER BY
            CASE urgency WHEN 'today' THEN 1 WHEN 'this_week' THEN 2 ELSE 3 END,
            received_at DESC
    """).fetchall()
    return [{"id": r[0], "from": r[1], "subject": r[2],
             "urgency": r[3], "received_at": r[4]} for r in rows]


def get_upcoming_follow_ups(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    until = (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT id, from_addr, subject, follow_up_date
        FROM follow_ups
        WHERE status = 'open'
          AND follow_up_date BETWEEN ? AND ?
        ORDER BY follow_up_date ASC
    """, (today, until)).fetchall()
    return [{"id": r[0], "from": r[1], "subject": r[2], "follow_up_date": r[3]}
            for r in rows]


def get_stats(conn: sqlite3.Connection) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    total_today = conn.execute(
        "SELECT COUNT(*) FROM analyzed_emails WHERE analyzed_at >= ?", (today,)
    ).fetchone()[0]
    open_tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='open'"
    ).fetchone()[0]
    pending_replies = conn.execute(
        "SELECT COUNT(*) FROM pending_replies WHERE status='pending'"
    ).fetchone()[0]
    total_contacts = conn.execute(
        "SELECT COUNT(*) FROM contacts"
    ).fetchone()[0]
    return {
        "emails_today": total_today,
        "open_tasks":   open_tasks,
        "pending_replies": pending_replies,
        "total_contacts": total_contacts,
    }


# ── Terminal briefing ─────────────────────────────────────────────────────────

def print_briefing(conn: sqlite3.Connection) -> dict:
    """Print the full daily briefing to terminal. Returns briefing data for Notion."""
    now        = datetime.now()
    priority   = get_priority_emails(conn)
    jobs       = get_job_emails(conn)
    tasks      = get_open_tasks(conn)
    replies    = get_pending_replies(conn)
    followups  = get_upcoming_follow_ups(conn)
    stats      = get_stats(conn)

    URGENCY_ICON = {"today": "🔴", "this_week": "🟡", "whenever": "🔵"}
    PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}

    # ── Header ────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  📬 DAILY BRIEFING — {now.strftime('%A, %B %-d, %Y  %-I:%M %p')}")
    print(f"{'='*60}")
    print(f"  {stats['emails_today']} emails today  ·  "
          f"{stats['open_tasks']} open tasks  ·  "
          f"{stats['pending_replies']} pending replies  ·  "
          f"{stats['total_contacts']} contacts")
    print(f"{'─'*60}\n")

    # ── Priority Emails ───────────────────────────────────────────────────
    print(f"📧 PRIORITY EMAILS (last 24h) — {len(priority)}")
    print(f"{'─'*60}")
    if priority:
        for e in priority:
            icon    = PRIORITY_ICON.get(e["priority"], "⚪")
            job_tag = "  💼 JOB"  if e["job_opportunity"] else ""
            rep_tag = "  💬 REPLY" if e["reply_needed"]   else ""
            print(f"  {icon} {e['subject'][:58]}{job_tag}{rep_tag}")
            print(f"     {e['summary'][:75]}")
            for item in e.get("action_items", []):
                print(f"     → {item[:70]}")
            print()
    else:
        print("  No priority emails in the last 24 hours.\n")

    # ── Pending Replies ───────────────────────────────────────────────────
    print(f"💬 PENDING REPLIES — {len(replies)}")
    print(f"{'─'*60}")
    if replies:
        for r in replies:
            icon = URGENCY_ICON.get(r["urgency"], "⚪")
            print(f"  {icon} [{r['urgency']:<10}]  {r['subject'][:48]}")
            print(f"              ↳ {r['from'][:55]}")
        print()
    else:
        print("  No pending replies.\n")

    # ── Open Tasks ────────────────────────────────────────────────────────
    print(f"📋 OPEN TASKS — {len(tasks)}")
    print(f"{'─'*60}")
    if tasks:
        for t in tasks:
            due = f"  [due {t['due_date'][:10]}]" if t.get("due_date") else ""
            print(f"  [ ] {t['task'][:62]}{due}")
        print()
    else:
        print("  No open tasks.\n")

    # ── Job Opportunities ─────────────────────────────────────────────────
    print(f"💼 JOB OPPORTUNITIES (last 7 days) — {len(jobs)}")
    print(f"{'─'*60}")
    if jobs:
        for j in jobs:
            date = j["received_at"][:10] if j.get("received_at") else ""
            print(f"  [{date}]  {j['subject'][:54]}")
            print(f"            {j['summary'][:70]}")
        print()
    else:
        print("  No job opportunities this week.\n")

    # ── Follow-ups ────────────────────────────────────────────────────────
    print(f"🗓  UPCOMING FOLLOW-UPS (next 7 days) — {len(followups)}")
    print(f"{'─'*60}")
    if followups:
        for f in followups:
            date = f.get("follow_up_date", "")[:10]
            print(f"  [{date}]  {f['subject'][:54]}")
            print(f"            ↳ {f['from'][:60]}")
        print()
    else:
        print("  No upcoming follow-ups.\n")

    print(f"{'='*60}\n")

    return {
        "priority": priority, "replies": replies, "tasks": tasks,
        "jobs": jobs, "followups": followups, "stats": stats,
    }


# ── Notion briefing post ──────────────────────────────────────────────────────

def build_briefing_blocks(data: dict) -> list[dict]:
    """Convert briefing data into Notion API blocks."""
    blocks  = []
    now     = datetime.now()
    stats   = data["stats"]
    URGENCY_ICON  = {"today": "🔴", "this_week": "🟡", "whenever": "🔵"}
    PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "low": "🔵"}

    def h2(text):
        return {"object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

    def h3(text):
        return {"object": "block", "type": "heading_3",
                "heading_3": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

    def para(text):
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

    def bullet(text):
        return {"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

    def todo(text):
        return {"object": "block", "type": "to_do",
                "to_do": {"rich_text": [{"type": "text", "text": {"content": text}}], "checked": False}}

    def quote(text):
        return {"object": "block", "type": "quote",
                "quote": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

    def divider():
        return {"object": "block", "type": "divider", "divider": {}}

    # Stats callout
    blocks.append(para(
        f"📊  {stats['emails_today']} emails today  ·  "
        f"{stats['open_tasks']} open tasks  ·  "
        f"{stats['pending_replies']} pending replies  ·  "
        f"{stats['total_contacts']} contacts"
    ))
    blocks.append(divider())

    # Priority emails
    blocks.append(h2(f"📧 Priority Emails — {len(data['priority'])}"))
    if data["priority"]:
        for e in data["priority"]:
            icon    = PRIORITY_ICON.get(e["priority"], "⚪")
            job_tag = "  💼" if e["job_opportunity"] else ""
            rep_tag = "  💬" if e["reply_needed"]    else ""
            blocks.append(h3(f"{icon} {e['subject'][:75]}{job_tag}{rep_tag}"))
            blocks.append(quote(e["summary"][:200]))
            for item in e.get("action_items", []):
                blocks.append(todo(item[:100]))
    else:
        blocks.append(para("No priority emails in the last 24 hours."))
    blocks.append(divider())

    # Pending replies
    blocks.append(h2(f"💬 Pending Replies — {len(data['replies'])}"))
    if data["replies"]:
        for r in data["replies"]:
            icon = URGENCY_ICON.get(r["urgency"], "⚪")
            blocks.append(bullet(f"{icon} [{r['urgency']}]  {r['subject'][:60]}  ↳ {r['from'][:50]}"))
    else:
        blocks.append(para("No pending replies."))
    blocks.append(divider())

    # Open tasks
    blocks.append(h2(f"📋 Open Tasks — {len(data['tasks'])}"))
    if data["tasks"]:
        for t in data["tasks"]:
            due = f"  [due {t['due_date'][:10]}]" if t.get("due_date") else ""
            blocks.append(todo(f"{t['task'][:80]}{due}"))
    else:
        blocks.append(para("No open tasks."))
    blocks.append(divider())

    # Job opportunities
    blocks.append(h2(f"💼 Job Opportunities — {len(data['jobs'])}"))
    if data["jobs"]:
        for j in data["jobs"]:
            blocks.append(h3(j["subject"][:80]))
            blocks.append(quote(j["summary"][:200]))
    else:
        blocks.append(para("No job opportunities this week."))
    blocks.append(divider())

    # Follow-ups
    blocks.append(h2(f"🗓 Upcoming Follow-ups — {len(data['followups'])}"))
    if data["followups"]:
        for f in data["followups"]:
            date = f.get("follow_up_date", "")[:10]
            blocks.append(bullet(f"[{date}]  {f['subject'][:60]}  ↳ {f['from'][:50]}"))
    else:
        blocks.append(para("No upcoming follow-ups."))

    return blocks[:100]  # Notion limit


def post_briefing_to_notion(data: dict) -> Optional[str]:
    """Post the daily briefing as a new Notion page entry."""
    if not NOTION_TOKEN or not NOTION_DIGEST_DB:
        print("  ⚠ Notion not configured — skipping.")
        return None

    now         = datetime.now(timezone.utc)
    title       = f"🌅 BRIEFING  {now.strftime('%m/%d, %H:%M, %A')}"
    db_id       = NOTION_DIGEST_DB.replace("-", "")
    blocks      = build_briefing_blocks(data)

    payload = {
        "parent": {"database_id": db_id},
        "icon": {"type": "emoji", "emoji": "🌅"},
        "properties": {
            "Daily Digests": {
                "title": [{"type": "text", "text": {"content": title}}]
            },
            "Date": {
                "date": {"start": now.strftime("%Y-%m-%dT%H:%M:%S+00:00")}
            },
            "Processed": {
                "select": {"name": "Yes"}
            }
        },
        "children": blocks,
    }

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    resp = requests.post(f"{NOTION_API}/pages", headers=headers, json=payload)
    if resp.status_code == 200:
        return resp.json().get("url", "")
    else:
        print(f"  ⚠ Notion API error {resp.status_code}: {resp.text[:200]}")
        return None


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    post_to_notion = "--post" in sys.argv

    if not DB_PATH.exists():
        print("No database found. Run the pipeline first: python src/pipeline.py")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    data = print_briefing(conn)
    conn.close()

    if post_to_notion:
        print("Posting briefing to Notion...")
        url = post_briefing_to_notion(data)
        if url:
            print(f"  → Briefing posted: {url}\n")
        else:
            print("  → Failed to post to Notion.\n")
    else:
        print("Tip: run with --post to also save this briefing to Notion.\n")
