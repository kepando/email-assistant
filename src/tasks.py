"""
Phase 4 — Task and Memory System
Manages tasks, pending replies, follow-ups, and contacts.
All data is extracted from Claude analysis results — no extra API calls.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "db" / "emails.db"


# ── Schema ────────────────────────────────────────────────────────────────────

def init_task_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id    TEXT,
            source      TEXT,       -- "Subject | From" for context
            task        TEXT NOT NULL,
            status      TEXT DEFAULT 'open',  -- open | done | dismissed
            due_date    TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS pending_replies (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id    TEXT UNIQUE,
            from_addr   TEXT,
            subject     TEXT,
            received_at TEXT,
            urgency     TEXT,   -- today | this_week | whenever
            status      TEXT DEFAULT 'pending',  -- pending | replied | dismissed
            created_at  TEXT NOT NULL,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS follow_ups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id        TEXT UNIQUE,
            from_addr       TEXT,
            subject         TEXT,
            follow_up_date  TEXT,
            status          TEXT DEFAULT 'open',  -- open | done | dismissed
            created_at      TEXT NOT NULL,
            updated_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS contacts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT UNIQUE,
            name        TEXT,
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            email_count INTEGER DEFAULT 1,
            notes       TEXT
        );
    """)
    conn.commit()


# ── Sync from analysis results ────────────────────────────────────────────────

def sync_from_analysis(conn: sqlite3.Connection, results: list[dict]) -> dict:
    """
    Extract and store tasks, replies, follow-ups, and contacts
    from a batch of Claude analysis results.
    Returns counts of new records created.
    """
    init_task_tables(conn)
    now = datetime.now(timezone.utc).isoformat()
    counts = {"tasks": 0, "replies": 0, "follow_ups": 0, "contacts": 0}

    for r in results:
        email_id = r.get("email_id", "")
        subject  = r.get("subject", "")[:80]
        from_addr = r.get("from", "")
        source   = f"{subject} | {from_addr}"

        # ── Tasks ──────────────────────────────────────────────────────────
        for item in r.get("action_items", []):
            if not item.strip():
                continue
            # Avoid duplicates for same email+task
            exists = conn.execute(
                "SELECT 1 FROM tasks WHERE email_id=? AND task=?", (email_id, item)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO tasks (email_id, source, task, due_date, created_at) VALUES (?,?,?,?,?)",
                    (email_id, source, item, r.get("follow_up_date"), now)
                )
                counts["tasks"] += 1

        # ── Pending Replies ────────────────────────────────────────────────
        if r.get("reply_needed") and r.get("reply_urgency", "none") != "none":
            exists = conn.execute(
                "SELECT 1 FROM pending_replies WHERE email_id=?", (email_id,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO pending_replies "
                    "(email_id, from_addr, subject, received_at, urgency, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (email_id, from_addr, subject,
                     r.get("received_at", ""), r.get("reply_urgency", "whenever"), now)
                )
                counts["replies"] += 1

        # ── Follow-ups ─────────────────────────────────────────────────────
        if r.get("follow_up_date"):
            exists = conn.execute(
                "SELECT 1 FROM follow_ups WHERE email_id=?", (email_id,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO follow_ups "
                    "(email_id, from_addr, subject, follow_up_date, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (email_id, from_addr, subject, r.get("follow_up_date"), now)
                )
                counts["follow_ups"] += 1

        # ── Contacts ───────────────────────────────────────────────────────
        # Parse "Name <email@domain>" format
        raw_from = r.get("from", "")
        contact_email, contact_name = _parse_address(raw_from)

        if contact_email:
            existing = conn.execute(
                "SELECT id, email_count FROM contacts WHERE email=?", (contact_email,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE contacts SET last_seen=?, email_count=email_count+1 WHERE email=?",
                    (now, contact_email)
                )
            else:
                conn.execute(
                    "INSERT INTO contacts (email, name, first_seen, last_seen) VALUES (?,?,?,?)",
                    (contact_email, contact_name, now, now)
                )
                counts["contacts"] += 1

        # Also add key_people mentioned in the email
        for person in r.get("key_people", []):
            p_email, p_name = _parse_address(person)
            if p_email and "@" in p_email:
                exists = conn.execute(
                    "SELECT 1 FROM contacts WHERE email=?", (p_email,)
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO contacts (email, name, first_seen, last_seen) VALUES (?,?,?,?)",
                        (p_email, p_name, now, now)
                    )
                    counts["contacts"] += 1

    conn.commit()
    return counts


# ── Query helpers ─────────────────────────────────────────────────────────────

def get_open_tasks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT id, source, task, due_date, created_at
        FROM tasks WHERE status='open'
        ORDER BY due_date ASC NULLS LAST, created_at ASC
    """).fetchall()
    return [{"id": r[0], "source": r[1], "task": r[2],
             "due_date": r[3], "created_at": r[4]} for r in rows]


def get_pending_replies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT id, from_addr, subject, received_at, urgency
        FROM pending_replies WHERE status='pending'
        ORDER BY
            CASE urgency WHEN 'today' THEN 1 WHEN 'this_week' THEN 2 ELSE 3 END,
            received_at DESC
    """).fetchall()
    return [{"id": r[0], "from": r[1], "subject": r[2],
             "received_at": r[3], "urgency": r[4]} for r in rows]


def get_open_follow_ups(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT id, from_addr, subject, follow_up_date
        FROM follow_ups WHERE status='open'
        ORDER BY follow_up_date ASC
    """).fetchall()
    return [{"id": r[0], "from": r[1], "subject": r[2],
             "follow_up_date": r[3]} for r in rows]


def get_contacts(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute("""
        SELECT id, name, email, email_count, last_seen
        FROM contacts
        ORDER BY email_count DESC, last_seen DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [{"id": r[0], "name": r[1], "email": r[2],
             "email_count": r[3], "last_seen": r[4]} for r in rows]


# ── Status updates ────────────────────────────────────────────────────────────

def mark_task_done(conn: sqlite3.Connection, task_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE tasks SET status='done', updated_at=? WHERE id=?", (now, task_id))
    conn.commit()


def mark_reply_sent(conn: sqlite3.Connection, reply_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE pending_replies SET status='replied', updated_at=? WHERE id=?", (now, reply_id))
    conn.commit()


def mark_follow_up_done(conn: sqlite3.Connection, fu_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE follow_ups SET status='done', updated_at=? WHERE id=?", (now, fu_id))
    conn.commit()


# ── CLI dashboard ─────────────────────────────────────────────────────────────

def print_dashboard(conn: sqlite3.Connection) -> None:
    tasks    = get_open_tasks(conn)
    replies  = get_pending_replies(conn)
    followups = get_open_follow_ups(conn)
    contacts = get_contacts(conn, limit=10)

    print(f"\n{'='*60}")
    print(f"  TASK & MEMORY DASHBOARD")
    print(f"{'='*60}\n")

    # Open Tasks
    print(f"📋 OPEN TASKS ({len(tasks)})")
    print(f"{'─'*60}")
    if tasks:
        for t in tasks:
            due = f"  [due {t['due_date'][:10]}]" if t.get("due_date") else ""
            print(f"  [{t['id']:>3}] {t['task'][:65]}{due}")
            print(f"         ↳ {t['source'][:65]}")
    else:
        print("  No open tasks.")
    print()

    # Pending Replies
    urgency_icon = {"today": "🔴", "this_week": "🟡", "whenever": "🔵"}
    print(f"💬 PENDING REPLIES ({len(replies)})")
    print(f"{'─'*60}")
    if replies:
        for r in replies:
            icon = urgency_icon.get(r["urgency"], "⚪")
            print(f"  {icon} [{r['id']:>3}] {r['subject'][:55]}")
            print(f"         ↳ {r['from'][:60]}  [{r['urgency']}]")
    else:
        print("  No pending replies.")
    print()

    # Follow-ups
    print(f"🗓  FOLLOW-UPS ({len(followups)})")
    print(f"{'─'*60}")
    if followups:
        for f in followups:
            date = f.get("follow_up_date", "")[:10] if f.get("follow_up_date") else "no date"
            print(f"  [{f['id']:>3}] {f['subject'][:58]}  [{date}]")
            print(f"         ↳ {f['from'][:60]}")
    else:
        print("  No open follow-ups.")
    print()

    # Top Contacts
    print(f"👥 TOP CONTACTS (by frequency)")
    print(f"{'─'*60}")
    if contacts:
        for c in contacts:
            name = c["name"] or ""
            email = c["email"] or ""
            label = f"{name} <{email}>" if name else email
            print(f"  {c['email_count']:>3}x  {label[:58]}")
    else:
        print("  No contacts yet.")
    print()


# ── Utilities ─────────────────────────────────────────────────────────────────

def _parse_address(raw: str) -> tuple:
    """Parse 'Name <email>' or plain 'email' into (email, name)."""
    raw = raw.strip()
    if "<" in raw and ">" in raw:
        name  = raw[:raw.index("<")].strip().strip('"')
        email = raw[raw.index("<")+1:raw.index(">")].strip().lower()
        return email, name
    elif "@" in raw:
        return raw.lower(), ""
    return "", raw


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    init_task_tables(conn)
    print_dashboard(conn)
    conn.close()
