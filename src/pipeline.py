"""
Phase 3 — Full Pipeline
Orchestrates: ingest → deduplicate → filter → batch → analyze → store → Notion
Run this daily. Only new unseen emails are sent to Claude.
"""

import os
import json
import sqlite3
import requests
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

DB_PATH     = Path(__file__).parent.parent / "data" / "db" / "emails.db"
OUTPUT_PATH = Path(__file__).parent.parent / "data" / "output"
GRAPH_API   = "https://graph.microsoft.com/v1.0"

# Outlook category labels prefixed with "EA:" so they're grouped in Outlook
# Color presets: preset0=Red, preset3=Yellow, preset4=Green, preset7=Blue, preset12=Gray, preset8=Purple
OUTLOOK_CATEGORIES = {
    "EA: high":            "preset0",   # Red
    "EA: medium":          "preset3",   # Yellow
    "EA: low":             "preset7",   # Blue
    "EA: ignore":          "preset12",  # Gray
    "EA: action_required": "preset0",   # Red
    "EA: needs_reply":     "preset1",   # Orange
    "EA: fyi":             "preset7",   # Blue
    "EA: newsletter":      "preset8",   # Purple
    "EA: notification":    "preset5",   # Teal
    "EA: spam":            "preset12",  # Gray
    "EA: job_opportunity": "preset4",   # Green
}

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH.mkdir(parents=True, exist_ok=True)


# ── Database ─────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS seen_emails (
            email_id    TEXT PRIMARY KEY,
            graph_id    TEXT,
            subject     TEXT,
            sender      TEXT,
            received_at TEXT,
            seen_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS analyzed_emails (
            email_id        TEXT PRIMARY KEY,
            graph_id        TEXT,
            from_addr       TEXT,
            subject         TEXT,
            received_at     TEXT,
            priority        TEXT,
            category        TEXT,
            summary         TEXT,
            action_items    TEXT,   -- JSON array
            reply_needed    INTEGER,
            reply_urgency   TEXT,
            follow_up_date  TEXT,
            job_opportunity INTEGER,
            key_people      TEXT,   -- JSON array
            sentiment       TEXT,
            analyzed_at     TEXT NOT NULL
        );
    """)
    conn.commit()


def get_seen_ids(conn: sqlite3.Connection) -> set:
    """Return all email_ids already processed."""
    rows = conn.execute("SELECT email_id FROM seen_emails").fetchall()
    return {r[0] for r in rows}


def mark_seen(conn: sqlite3.Connection, emails: list[dict]) -> None:
    """Record emails as seen so they're never re-analyzed."""
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT OR IGNORE INTO seen_emails (email_id, graph_id, subject, sender, received_at, seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(e["email_id"], e.get("graph_id", ""), e.get("subject", ""),
          e.get("from", ""), e.get("received_at", ""), now)
         for e in emails]
    )
    conn.commit()


def store_results(conn: sqlite3.Connection, results: list[dict], emails_map: dict) -> None:
    """Persist Claude analysis results to the database."""
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        eid = r.get("email_id", "")
        raw = emails_map.get(eid, {})
        conn.execute("""
            INSERT OR REPLACE INTO analyzed_emails
            (email_id, graph_id, from_addr, subject, received_at, priority, category,
             summary, action_items, reply_needed, reply_urgency, follow_up_date,
             job_opportunity, key_people, sentiment, analyzed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            eid,
            raw.get("graph_id", ""),
            r.get("from", ""),
            r.get("subject", ""),
            r.get("received_at", ""),
            r.get("priority", ""),
            r.get("category", ""),
            r.get("summary", ""),
            json.dumps(r.get("action_items", [])),
            1 if r.get("reply_needed") else 0,
            r.get("reply_urgency", "none"),
            r.get("follow_up_date"),
            1 if r.get("job_opportunity") else 0,
            json.dumps(r.get("key_people", [])),
            r.get("sentiment", ""),
            now,
        ))
    conn.commit()


# ── Outlook Categories ────────────────────────────────────────────────────────

def _ensure_outlook_categories(token: str) -> None:
    """Create EA: categories in Outlook master list if they don't exist yet."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.get(f"{GRAPH_API}/me/outlook/masterCategories", headers=headers)
    if resp.status_code != 200:
        print(f"  ⚠ Could not fetch Outlook master categories: {resp.status_code}")
        return
    existing = {c["displayName"] for c in resp.json().get("value", [])}
    for name, color in OUTLOOK_CATEGORIES.items():
        if name not in existing:
            r = requests.post(
                f"{GRAPH_API}/me/outlook/masterCategories",
                headers=headers,
                json={"displayName": name, "color": color},
            )
            if r.status_code in (200, 201):
                print(f"  + Created Outlook category: {name}")
            # 409 = already exists, safe to ignore


def apply_outlook_categories(results: list[dict], emails_map: dict, token: str) -> None:
    """Patch each analyzed email in Outlook with its EA: priority + category labels."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    ok = skipped = 0
    for r in results:
        eid      = r.get("email_id", "")
        graph_id = emails_map.get(eid, {}).get("graph_id", "")
        if not graph_id:
            skipped += 1
            continue
        cats = []
        if r.get("priority"):
            cats.append(f"EA: {r['priority']}")
        if r.get("category"):
            cats.append(f"EA: {r['category']}")
        if not cats:
            continue
        resp = requests.patch(
            f"{GRAPH_API}/me/messages/{graph_id}",
            headers=headers,
            json={"categories": cats},
        )
        if resp.status_code in (200, 201):
            ok += 1
        elif resp.status_code != 404:   # 404 = already deleted, skip silently
            print(f"  ⚠ Could not label {graph_id[:24]}…: {resp.status_code}")
    print(f"  → {ok} emails labeled in Outlook" + (f" ({skipped} skipped, no graph_id)" if skipped else ""))


# ── Notion ───────────────────────────────────────────────────────────────────

NOTION_TOKEN     = os.environ.get("NOTION_TOKEN", "")
NOTION_DIGEST_DB = os.environ.get("NOTION_DIGEST_DB", "")
NOTION_API       = "https://api.notion.com/v1"
NOTION_VERSION   = "2022-06-28"


def build_digest_content(results: list[dict], junk: list[dict], stats: dict) -> str:
    """Build Notion markdown content for the digest page."""
    lines = []

    # Stats banner
    lines.append(f"## 📊 Run Summary")
    lines.append(
        f"**{stats['total']} fetched** · "
        f"**{stats['new']} new** · "
        f"{stats['junk_filtered']} pre-filtered · "
        f"{stats['already_seen']} already seen"
    )
    lines.append(
        f"🔴 {stats['high']} urgent · "
        f"🟡 {stats['medium']} medium · "
        f"💬 {stats['replies']} need reply · "
        f"💼 {stats['jobs']} job leads"
    )
    lines.append("")

    # Priority sections
    for priority, label, emoji in [
        ("high",   "Urgent — Action Today",    "🔴"),
        ("medium", "Medium — This Week",        "🟡"),
        ("low",    "Low — FYI",                 "🔵"),
    ]:
        section = [r for r in results if r.get("priority") == priority]
        if not section:
            continue
        lines.append(f"## {emoji} {label}")
        for r in section:
            job_tag  = " `JOB`"  if r.get("job_opportunity") else ""
            rep_tag  = " `REPLY`" if r.get("reply_needed")    else ""
            lines.append(f"### {r.get('subject', '(no subject)')[:80]}{job_tag}{rep_tag}")
            lines.append(f"**From:** {r.get('from', '')}  |  **Category:** {r.get('category', '')}")
            lines.append(f"> {r.get('summary', '')}")
            items = r.get("action_items", [])
            if items:
                lines.append("")
                for item in items:
                    lines.append(f"- [ ] {item}")
            lines.append("")

    # Ignore section (collapsed)
    ignored = [r for r in results if r.get("priority") == "ignore"]
    if ignored or junk:
        lines.append("## ⚫ Ignored / Pre-filtered")
        for r in ignored:
            lines.append(f"- {r.get('subject', '')[:70]} — *{r.get('from', '')}*")
        for j in (junk or []):
            lines.append(f"- *(pre-filtered)* {j.get('subject', '')[:70]}")

    return "\n".join(lines)


def post_to_notion(digest_name: str, run_dt: datetime,
                   results: list[dict], junk: list[dict], stats: dict) -> Optional[str]:
    """Create a new Daily Digest entry in Notion. Returns the page URL or None."""
    if not NOTION_TOKEN or not NOTION_DIGEST_DB:
        print("  ⚠ Notion not configured — skipping.")
        return None

    content = build_digest_content(results, junk, stats)
    db_id   = NOTION_DIGEST_DB.replace("-", "")

    # Convert content to Notion blocks (paragraph per line, headings handled)
    blocks = []
    for line in content.split("\n"):
        if line.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2",
                            "heading_2": {"rich_text": [{"type": "text", "text": {"content": line[3:]}}]}})
        elif line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                            "heading_3": {"rich_text": [{"type": "text", "text": {"content": line[4:]}}]}})
        elif line.startswith("- [ ] "):
            blocks.append({"object": "block", "type": "to_do",
                            "to_do": {"rich_text": [{"type": "text", "text": {"content": line[6:]}}], "checked": False}})
        elif line.startswith("- "):
            blocks.append({"object": "block", "type": "bulleted_list_item",
                            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": line[2:]}}]}})
        elif line.startswith("> "):
            blocks.append({"object": "block", "type": "quote",
                            "quote": {"rich_text": [{"type": "text", "text": {"content": line[2:]}}]}})
        elif line.strip():
            blocks.append({"object": "block", "type": "paragraph",
                            "paragraph": {"rich_text": [{"type": "text", "text": {"content": line}}]}})

    payload = {
        "parent": {"database_id": db_id},
        "icon": {"type": "emoji", "emoji": "📬"},
        "properties": {
            "Daily Digests": {
                "title": [{"type": "text", "text": {"content": digest_name}}]
            },
            "Date": {
                "date": {"start": run_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")}
            },
            "Processed": {
                "select": {"name": "Yes"}
            }
        },
        "children": blocks[:100],  # Notion API limit: 100 blocks per request
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


# ── Pipeline ─────────────────────────────────────────────────────────────────

def run():
    from ingest import fetch_emails
    from analyze import analyze_emails, print_summary

    print(f"{'='*60}")
    print(f"  Email Assistant Pipeline — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    # Step 1 — Fetch emails from Outlook
    print("Step 1 — Fetching emails from Outlook...")
    emails = fetch_emails()
    print(f"  → {len(emails)} emails fetched\n")

    if not emails:
        print("No emails fetched. Exiting.")
        return

    # Step 2 — Deduplicate against seen emails
    print("Step 2 — Deduplicating...")
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    seen_ids = get_seen_ids(conn)
    new_emails = [e for e in emails if e["email_id"] not in seen_ids]
    already_seen = len(emails) - len(new_emails)
    print(f"  → {len(new_emails)} new, {already_seen} already seen\n")

    if not new_emails:
        print("No new emails to analyze.")
        conn.close()
        return

    # Mark all fetched emails as seen immediately (even junk)
    mark_seen(conn, new_emails)

    # Step 3 — Analyze with Claude (includes local pre-filtering)
    print("Step 3 — Analyzing with Claude...")
    results, junk = analyze_emails(new_emails)

    # Step 4 — Store results
    print("\nStep 4 — Storing results...")
    emails_map = {e["email_id"]: e for e in new_emails}
    store_results(conn, results, emails_map)

    # Step 4b — Apply Outlook categories (EA: priority + category labels)
    print("Step 4b — Applying Outlook categories...")
    try:
        from ingest import get_access_token
        _ensure_outlook_categories(get_access_token())
        apply_outlook_categories(results, emails_map, get_access_token())
    except Exception as e:
        print(f"  ⚠ Outlook categories skipped (non-fatal): {e}")

    # Step 4c — Sync tasks, replies, follow-ups, contacts
    from tasks import sync_from_analysis, print_dashboard
    task_counts = sync_from_analysis(conn, results)
    conn.close()

    print(f"  → {len(results)} emails stored")
    print(f"  → +{task_counts['tasks']} tasks  "
          f"+{task_counts['replies']} pending replies  "
          f"+{task_counts['follow_ups']} follow-ups  "
          f"+{task_counts['contacts']} new contacts\n")

    # Save JSON snapshot
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_file = OUTPUT_PATH / f"analysis_{ts}.json"
    out_file.write_text(json.dumps(results, indent=2))
    print(f"  → Snapshot saved to {out_file}\n")

    # Step 5 — Print summary
    print(f"{'='*60}")
    print("  INBOX SUMMARY")
    print(f"{'='*60}\n")
    print_summary(results, junk)

    # Stats footer
    now     = datetime.now(timezone.utc)
    high    = sum(1 for r in results if r.get("priority") == "high")
    medium  = sum(1 for r in results if r.get("priority") == "medium")
    replies = sum(1 for r in results if r.get("reply_needed"))
    jobs    = sum(1 for r in results if r.get("job_opportunity"))
    print(f"\n{'─'*60}")
    print(f"  {len(new_emails)} new  |  {high} urgent  |  {medium} medium  |  {replies} need reply  |  {jobs} job leads")
    print(f"  {len(junk)} pre-filtered locally (no API cost)")
    print(f"{'─'*60}\n")

    # Step 5b — Print task dashboard
    conn2 = sqlite3.connect(DB_PATH)
    print_dashboard(conn2)
    conn2.close()

    # Step 6 — Post digest to Notion
    print("Step 6 — Posting digest to Notion...")
    digest_name = now.strftime("%m/%d, %H:%M, %A")   # e.g. "03/15, 08:05, Sunday"
    stats = {
        "total":          len(emails),
        "new":            len(new_emails),
        "already_seen":   already_seen,
        "junk_filtered":  len(junk),
        "high":           high,
        "medium":         medium,
        "replies":        replies,
        "jobs":           jobs,
    }
    notion_url = post_to_notion(digest_name, now, results, junk, stats)
    if notion_url:
        print(f"  → Digest posted: {notion_url}\n")
    else:
        print(f"  → Notion post failed or skipped.\n")


if __name__ == "__main__":
    run()
