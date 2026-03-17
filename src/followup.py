"""
Phase 7 — Follow-Up Intelligence
Detects when you've sent an email and haven't received a reply after N days.
Surfaces overdue follow-ups and generates reminder drafts.

Usage:
    python src/followup.py            # Scan and show overdue follow-ups
    python src/followup.py --remind   # Also generate reminder drafts for overdue items
    python src/followup.py --scan     # Re-scan sent folder and update tracking DB
"""

import os
import sys
import re
import sqlite3
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional
from dotenv import load_dotenv
import anthropic

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

DB_PATH       = Path(__file__).parent.parent / "data" / "db" / "emails.db"
DRAFTS_PATH   = Path(__file__).parent.parent / "data" / "drafts"
PROMPT_PATH   = Path(__file__).parent.parent / "prompts" / "draft_reply.txt"

DRAFTS_PATH.mkdir(parents=True, exist_ok=True)

AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
OUTLOOK_EMAIL   = os.environ.get("OUTLOOK_EMAIL", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
GRAPH_API       = "https://graph.microsoft.com/v1.0"

# How many days before a sent email is considered overdue for a reply
DEFAULT_FOLLOWUP_DAYS = 3


# ── Schema ────────────────────────────────────────────────────────────────────

def init_followup_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sent_emails (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            graph_id        TEXT UNIQUE,
            subject         TEXT,
            to_addr         TEXT,
            sent_at         TEXT,
            thread_id       TEXT,
            reply_received  INTEGER DEFAULT 0,  -- 1 when a reply is detected
            reply_at        TEXT,
            followup_days   INTEGER DEFAULT 3,
            reminded        INTEGER DEFAULT 0,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS followup_reminders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_email_id   INTEGER,
            draft_body      TEXT,
            outlook_draft_id TEXT,
            status          TEXT DEFAULT 'drafted',  -- drafted | sent | dismissed
            created_at      TEXT NOT NULL
        );
    """)
    conn.commit()


# ── Graph API helpers ─────────────────────────────────────────────────────────

def get_graph_token() -> str:
    sys.path.insert(0, str(Path(__file__).parent))
    from ingest import get_access_token
    return get_access_token()


def fetch_sent_emails(token: str, limit: int = 50) -> list[dict]:
    """Fetch recent emails from the Sent Items folder."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"{GRAPH_API}/me/mailFolders/sentItems/messages",
        headers=headers,
        params={
            "$top": limit,
            "$select": "id,subject,toRecipients,sentDateTime,conversationId",
            "$orderby": "sentDateTime desc",
        }
    )
    if resp.status_code == 200:
        return resp.json().get("value", [])
    else:
        raise RuntimeError(f"Graph API error {resp.status_code}: {resp.text[:200]}")


def fetch_thread_messages(token: str, conversation_id: str) -> list[dict]:
    """Fetch all messages in a conversation thread."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"{GRAPH_API}/me/messages",
        headers=headers,
        params={
            "$filter": f"conversationId eq '{conversation_id}'",
            "$select": "id,from,sentDateTime,isDraft",
            "$orderby": "sentDateTime asc",
        }
    )
    if resp.status_code == 200:
        return resp.json().get("value", [])
    return []


def create_followup_draft(token: str, graph_id: str, draft_body: str) -> str:
    """Create a follow-up draft reply in Outlook."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{GRAPH_API}/me/messages/{graph_id}/createReply",
        headers=headers,
        json={}
    )
    if resp.status_code != 201:
        raise RuntimeError(f"createReply failed {resp.status_code}: {resp.text[:200]}")

    draft_id = resp.json()["id"]
    resp2 = requests.patch(
        f"{GRAPH_API}/me/messages/{draft_id}",
        headers=headers,
        json={"body": {"contentType": "Text", "content": draft_body}}
    )
    if resp2.status_code != 200:
        raise RuntimeError(f"PATCH draft failed {resp2.status_code}: {resp2.text[:200]}")
    return draft_id


# ── Sent email tracking ───────────────────────────────────────────────────────

def sync_sent_emails(conn: sqlite3.Connection, token: str,
                     limit: int = 50) -> dict:
    """Fetch sent emails and store new ones in the DB."""
    sent    = fetch_sent_emails(token, limit=limit)
    now     = datetime.now(timezone.utc).isoformat()
    added   = 0

    for msg in sent:
        graph_id = msg.get("id", "")
        if not graph_id:
            continue

        exists = conn.execute(
            "SELECT 1 FROM sent_emails WHERE graph_id=?", (graph_id,)
        ).fetchone()
        if exists:
            continue

        to_list = msg.get("toRecipients", [])
        to_addr = ", ".join(
            r.get("emailAddress", {}).get("address", "")
            for r in to_list
        )
        conn.execute("""
            INSERT INTO sent_emails
            (graph_id, subject, to_addr, sent_at, thread_id, created_at)
            VALUES (?,?,?,?,?,?)
        """, (
            graph_id,
            msg.get("subject", ""),
            to_addr,
            msg.get("sentDateTime", ""),
            msg.get("conversationId", ""),
            now,
        ))
        added += 1

    conn.commit()
    return {"added": added, "total_scanned": len(sent)}


def check_for_replies(conn: sqlite3.Connection, token: str) -> int:
    """
    For each unresolved sent email, check if a reply has arrived.
    Returns count of newly resolved threads.
    """
    pending = conn.execute("""
        SELECT id, graph_id, thread_id, to_addr
        FROM sent_emails
        WHERE reply_received = 0 AND thread_id IS NOT NULL AND thread_id != ''
    """).fetchall()

    resolved = 0
    now      = datetime.now(timezone.utc).isoformat()

    for row in pending:
        sent_id, graph_id, thread_id, to_addr = row
        try:
            messages = fetch_thread_messages(token, thread_id)
        except Exception:
            continue

        # A reply exists if any message in the thread is NOT from us
        # (i.e., from someone other than OUTLOOK_EMAIL)
        my_email = OUTLOOK_EMAIL.lower()
        for m in messages:
            sender = m.get("from", {}).get("emailAddress", {}).get("address", "").lower()
            if sender and sender != my_email and not m.get("isDraft", False):
                conn.execute("""
                    UPDATE sent_emails
                    SET reply_received=1, reply_at=?
                    WHERE id=?
                """, (m.get("sentDateTime", now), sent_id))
                resolved += 1
                break

    conn.commit()
    return resolved


def get_overdue(conn: sqlite3.Connection,
                days: int = DEFAULT_FOLLOWUP_DAYS) -> list[dict]:
    """Return sent emails with no reply after N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows   = conn.execute("""
        SELECT id, graph_id, subject, to_addr, sent_at, reminded
        FROM sent_emails
        WHERE reply_received = 0
          AND sent_at <= ?
          AND sent_at != ''
        ORDER BY sent_at ASC
    """, (cutoff,)).fetchall()
    return [{
        "id": r[0], "graph_id": r[1], "subject": r[2],
        "to_addr": r[3], "sent_at": r[4], "reminded": bool(r[5]),
    } for r in rows]


# ── Reminder draft generation ─────────────────────────────────────────────────

def generate_reminder(subject: str, to_addr: str, sent_at: str) -> str:
    """Ask Claude to draft a polite follow-up reminder."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    sent_date = sent_at[:10] if sent_at else "a few days ago"
    prompt = (
        f"Draft a short, polite follow-up reminder email.\n\n"
        f"Original email subject: {subject}\n"
        f"Sent to: {to_addr}\n"
        f"Sent on: {sent_date}\n\n"
        f"The recipient hasn't replied yet. Write a brief, friendly nudge — "
        f"2-3 sentences max. Sign off as Ken. Plain text only, no subject line."
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


# ── Terminal output ───────────────────────────────────────────────────────────

def print_overdue(overdue: list[dict], days: int) -> None:
    print(f"\n{'='*60}")
    print(f"  ⏰ FOLLOW-UP INTELLIGENCE — No reply after {days}+ days")
    print(f"{'='*60}\n")

    if not overdue:
        print(f"  ✓ No overdue follow-ups. All threads have replies.\n")
        return

    for item in overdue:
        age     = _days_ago(item["sent_at"])
        reminded = "  [reminder sent]" if item["reminded"] else ""
        print(f"  [{item['id']:>3}]  {item['subject'][:55]}{reminded}")
        print(f"         ↳ To: {item['to_addr'][:55]}")
        print(f"         ↳ Sent {age} days ago  ({item['sent_at'][:10]})")
        print()


def _days_ago(sent_at: str) -> int:
    try:
        sent = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - sent).days
    except Exception:
        return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def run(scan: bool = False, remind: bool = False,
        days: int = DEFAULT_FOLLOWUP_DAYS):

    if not DB_PATH.exists():
        print("No database found. Run the pipeline first.")
        return

    conn = sqlite3.connect(DB_PATH)
    init_followup_tables(conn)

    print(f"\n{'='*60}")
    print(f"  Follow-Up Intelligence — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    # Authenticate
    print("Authenticating with Outlook...")
    try:
        token = get_graph_token()
        print("  → Authenticated\n")
    except Exception as e:
        print(f"  ⚠ Auth failed: {e}")
        conn.close()
        return

    # Scan sent folder
    if scan or True:   # Always sync on run to keep DB fresh
        print("Syncing sent emails...")
        stats = sync_sent_emails(conn, token)
        print(f"  → {stats['added']} new sent emails tracked "
              f"({stats['total_scanned']} scanned)\n")

    # Check for replies
    print("Checking threads for replies...")
    resolved = check_for_replies(conn, token)
    print(f"  → {resolved} threads marked as resolved\n")

    # Show overdue
    overdue = get_overdue(conn, days=days)
    print_overdue(overdue, days)

    if not overdue:
        conn.close()
        return

    # Generate reminders
    if remind:
        print(f"Generating reminder drafts for {len(overdue)} overdue threads...\n")
        now = datetime.now(timezone.utc).isoformat()

        for item in overdue:
            if item["reminded"]:
                print(f"  → Skipping [{item['id']}] — reminder already sent.")
                continue

            print(f"  Drafting reminder for: {item['subject'][:55]}")
            try:
                draft = generate_reminder(
                    item["subject"], item["to_addr"], item["sent_at"]
                )
            except Exception as e:
                print(f"  ⚠ Claude error: {e}")
                continue

            print(f"\n  {'─'*50}")
            print(f"  {draft}")
            print(f"  {'─'*50}")
            print("  [s] Save to Outlook Drafts  [n] Skip  [d] Dismiss")

            choice = input("  Choice: ").strip().lower()
            if choice == "s":
                try:
                    draft_id = create_followup_draft(token, item["graph_id"], draft)
                    conn.execute("""
                        INSERT INTO followup_reminders
                        (sent_email_id, draft_body, outlook_draft_id, created_at)
                        VALUES (?,?,?,?)
                    """, (item["id"], draft, draft_id, now))
                    conn.execute(
                        "UPDATE sent_emails SET reminded=1 WHERE id=?", (item["id"],)
                    )
                    conn.commit()
                    print(f"  ✓ Reminder draft saved to Outlook Drafts.\n")
                except Exception as e:
                    print(f"  ⚠ Could not save to Outlook: {e}\n")
                    # Save locally as fallback
                    fname = DRAFTS_PATH / f"reminder_{item['id']}.txt"
                    fname.write_text(
                        f"To: {item['to_addr']}\nRe: {item['subject']}\n\n{draft}"
                    )
                    print(f"  → Saved locally to {fname}\n")
            elif choice == "d":
                conn.execute(
                    "UPDATE sent_emails SET reminded=1 WHERE id=?", (item["id"],)
                )
                conn.commit()
                print("  → Dismissed.\n")
            else:
                print("  → Skipped.\n")

    else:
        if overdue:
            print(f"Tip: run with --remind to generate follow-up drafts.\n")

    conn.close()


if __name__ == "__main__":
    args   = sys.argv[1:]
    run(
        scan   = "--scan"   in args,
        remind = "--remind" in args,
        days   = DEFAULT_FOLLOWUP_DAYS,
    )
