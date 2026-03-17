"""
Phase 6 — Reply Assistant
Generates draft replies for pending emails and saves them to Outlook Drafts.

Usage:
    python src/reply.py            # Interactive — review each draft before saving
    python src/reply.py --list     # Just list pending replies, no drafts
    python src/reply.py --id 3     # Draft a reply for a specific pending_reply id
"""

import os
import sys
import json
import sqlite3
import requests
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
import anthropic

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

DB_PATH     = Path(__file__).parent.parent / "data" / "db" / "emails.db"
DRAFTS_PATH = Path(__file__).parent.parent / "data" / "drafts"
PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "draft_reply.txt"

DRAFTS_PATH.mkdir(parents=True, exist_ok=True)

AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
OUTLOOK_EMAIL   = os.environ.get("OUTLOOK_EMAIL", "")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
GRAPH_API       = "https://graph.microsoft.com/v1.0"


# ── Outlook helpers ───────────────────────────────────────────────────────────

def get_graph_token() -> str:
    """Reuse the cached MSAL token from ingest.py."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from ingest import get_access_token
    return get_access_token()


def fetch_full_email(token: str, graph_id: str) -> dict:
    """Fetch the full email body from Graph API using the message's graph_id."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"{GRAPH_API}/me/messages/{graph_id}",
        headers=headers,
        params={"$select": "id,subject,from,body,receivedDateTime,toRecipients"}
    )
    if resp.status_code == 200:
        return resp.json()
    else:
        raise RuntimeError(f"Graph API error {resp.status_code}: {resp.text[:200]}")


def create_outlook_draft(token: str, graph_id: str, draft_body: str) -> str:
    """Create a reply draft in Outlook Drafts folder. Returns the draft message URL."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Step 1: Create a reply draft anchored to the original message thread
    resp = requests.post(
        f"{GRAPH_API}/me/messages/{graph_id}/createReply",
        headers=headers,
        json={}
    )
    if resp.status_code != 201:
        raise RuntimeError(f"createReply failed {resp.status_code}: {resp.text[:200]}")

    draft_id = resp.json()["id"]

    # Step 2: Set the draft body
    resp2 = requests.patch(
        f"{GRAPH_API}/me/messages/{draft_id}",
        headers=headers,
        json={"body": {"contentType": "Text", "content": draft_body}}
    )
    if resp2.status_code != 200:
        raise RuntimeError(f"PATCH draft failed {resp2.status_code}: {resp2.text[:200]}")

    return draft_id


# ── Claude draft generation ───────────────────────────────────────────────────

def generate_draft(email_subject: str, email_from: str,
                   email_body: str, context: str = "") -> str:
    """Send email to Claude and get a draft reply back."""
    client        = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    system_prompt = PROMPT_PATH.read_text()

    user_message = f"""Draft a reply to this email:

FROM: {email_from}
SUBJECT: {email_subject}
BODY:
{email_body[:3000]}
"""
    if context:
        user_message += f"\nAdditional context: {context}"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text.strip()


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_pending_replies(conn: sqlite3.Connection, reply_id: int = None) -> list[dict]:
    query = """
        SELECT pr.id, pr.email_id, pr.from_addr, pr.subject,
               pr.received_at, pr.urgency,
               ae.graph_id, ae.summary
        FROM pending_replies pr
        LEFT JOIN analyzed_emails ae ON pr.email_id = ae.email_id
        WHERE pr.status = 'pending'
    """
    params = ()
    if reply_id:
        query += " AND pr.id = ?"
        params = (reply_id,)
    query += " ORDER BY CASE pr.urgency WHEN 'today' THEN 1 WHEN 'this_week' THEN 2 ELSE 3 END"
    rows = conn.execute(query, params).fetchall()
    return [{
        "id": r[0], "email_id": r[1], "from": r[2], "subject": r[3],
        "received_at": r[4], "urgency": r[5], "graph_id": r[6], "summary": r[7],
    } for r in rows]


def save_draft_record(conn: sqlite3.Connection, reply_id: int,
                      draft_body: str, outlook_draft_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reply_drafts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            pending_reply_id INTEGER,
            draft_body      TEXT,
            outlook_draft_id TEXT,
            status          TEXT DEFAULT 'drafted',  -- drafted | sent | discarded
            created_at      TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO reply_drafts (pending_reply_id, draft_body, outlook_draft_id, created_at) "
        "VALUES (?,?,?,?)",
        (reply_id, draft_body, outlook_draft_id, now)
    )
    conn.execute(
        "UPDATE pending_replies SET status='drafted', updated_at=? WHERE id=?",
        (now, reply_id)
    )
    conn.commit()


# ── Interactive review ────────────────────────────────────────────────────────

def review_draft(pending: dict, draft_body: str) -> str:
    """
    Show the draft and prompt the user.
    Returns: 'save' | 'skip' | 'discard' | edited body string
    """
    URGENCY = {"today": "🔴", "this_week": "🟡", "whenever": "🔵"}
    icon    = URGENCY.get(pending["urgency"], "⚪")

    print(f"\n{'─'*60}")
    print(f"  {icon} REPLY DRAFT — [{pending['urgency']}]")
    print(f"  To:       {pending['from'][:60]}")
    print(f"  Re:       {pending['subject'][:60]}")
    print(f"{'─'*60}")
    print(f"\n{draft_body}\n")
    print(f"{'─'*60}")
    print("  [s] Save to Outlook Drafts")
    print("  [n] Skip this email")
    print("  [d] Discard (mark as dismissed)")
    print("  [e] Edit draft body manually")
    print(f"{'─'*60}")

    while True:
        choice = input("  Choice: ").strip().lower()
        if choice == "s":
            return "save"
        elif choice == "n":
            return "skip"
        elif choice == "d":
            return "discard"
        elif choice == "e":
            print("\n  Paste your edited reply below. Enter a blank line when done:")
            lines = []
            while True:
                line = input()
                if line == "" and lines and lines[-1] == "":
                    break
                lines.append(line)
            return "\n".join(lines).strip()
        else:
            print("  Please enter s, n, d, or e.")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(reply_id: int = None, list_only: bool = False):
    if not DB_PATH.exists():
        print("No database found. Run the pipeline first: python src/pipeline.py")
        return

    conn     = sqlite3.connect(DB_PATH)
    pending  = get_pending_replies(conn, reply_id=reply_id)

    if not pending:
        print("\nNo pending replies found.\n")
        conn.close()
        return

    print(f"\n{'='*60}")
    print(f"  ✉️  REPLY ASSISTANT — {len(pending)} pending")
    print(f"{'='*60}\n")

    URGENCY = {"today": "🔴", "this_week": "🟡", "whenever": "🔵"}
    for p in pending:
        icon = URGENCY.get(p["urgency"], "⚪")
        print(f"  [{p['id']:>3}] {icon} {p['subject'][:55]}")
        print(f"         ↳ {p['from'][:60]}  [{p['urgency']}]")
    print()

    if list_only:
        conn.close()
        return

    # Get Graph token once for all drafts
    print("Authenticating with Outlook...")
    try:
        token = get_graph_token()
    except Exception as e:
        print(f"  ⚠ Auth failed: {e}")
        conn.close()
        return
    print("  → Authenticated\n")

    saved = skipped = discarded = 0

    for p in pending:
        print(f"\nProcessing: {p['subject'][:60]}")

        # Fetch full email body
        email_body = ""
        if p.get("graph_id"):
            try:
                msg       = fetch_full_email(token, p["graph_id"])
                email_body = msg.get("body", {}).get("content", "")
                # Strip HTML if needed
                if msg.get("body", {}).get("contentType") == "html":
                    import re
                    email_body = re.sub(r"<[^>]+>", " ", email_body)
                    email_body = re.sub(r"\s+", " ", email_body).strip()
            except Exception as e:
                print(f"  ⚠ Could not fetch full body: {e}")
                email_body = p.get("summary", "")
        else:
            email_body = p.get("summary", "No body available.")

        # Generate draft
        print("  Generating draft with Claude...")
        try:
            draft = generate_draft(p["subject"], p["from"], email_body)
        except Exception as e:
            print(f"  ⚠ Claude error: {e}")
            continue

        # Review
        result = review_draft(p, draft)

        if result == "skip":
            skipped += 1
            continue
        elif result == "discard":
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE pending_replies SET status='dismissed', updated_at=? WHERE id=?",
                (now, p["id"])
            )
            conn.commit()
            discarded += 1
            print("  → Dismissed.")
            continue
        else:
            # result is either "save" or an edited body string
            final_body = draft if result == "save" else result

            # Save to Outlook Drafts
            if p.get("graph_id"):
                try:
                    outlook_id = create_outlook_draft(token, p["graph_id"], final_body)
                    save_draft_record(conn, p["id"], final_body, outlook_id)
                    print(f"  ✓ Draft saved to Outlook Drafts.")
                    saved += 1

                    # Also save locally
                    ts    = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    fname = DRAFTS_PATH / f"draft_{p['id']}_{ts}.txt"
                    fname.write_text(
                        f"To: {p['from']}\nRe: {p['subject']}\n\n{final_body}"
                    )
                except Exception as e:
                    print(f"  ⚠ Could not save to Outlook: {e}")
                    print("  → Draft saved locally only.")
                    fname = DRAFTS_PATH / f"draft_{p['id']}_error.txt"
                    fname.write_text(
                        f"To: {p['from']}\nRe: {p['subject']}\n\n{final_body}"
                    )
            else:
                print("  ⚠ No graph_id — saving locally only.")
                fname = DRAFTS_PATH / f"draft_{p['id']}.txt"
                fname.write_text(
                    f"To: {p['from']}\nRe: {p['subject']}\n\n{final_body}"
                )
                saved += 1

    conn.close()
    print(f"\n{'─'*60}")
    print(f"  Done — {saved} saved · {skipped} skipped · {discarded} discarded")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    args     = sys.argv[1:]
    list_only = "--list" in args
    reply_id  = None

    if "--id" in args:
        idx = args.index("--id")
        try:
            reply_id = int(args[idx + 1])
        except (IndexError, ValueError):
            print("Usage: python src/reply.py --id <number>")
            sys.exit(1)

    run(reply_id=reply_id, list_only=list_only)
