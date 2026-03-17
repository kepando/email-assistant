"""
Feedback & Filter Improvement System
Review recent email analyses, flag miscategorizations, and improve pre-filter rules.

Usage:
    python src/feedback.py              # Review last 20 analyzed emails
    python src/feedback.py --suggest    # Ask Claude to suggest rule improvements
    python src/feedback.py --rules      # Show current pre-filter rules
"""

import os
import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
import anthropic

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

DB_PATH      = Path(__file__).parent.parent / "data" / "db" / "emails.db"
RULES_PATH   = Path(__file__).parent.parent / "data" / "filter_rules.json"
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Default pre-filter rules (loaded/saved as JSON) ──────────────────────────

DEFAULT_RULES = {
    "junk_senders": [
        "noreply@", "no-reply@", "notifications@", "mailer@",
        "newsletter@", "updates@", "alerts@", "billing@",
        "invoices@", "receipts@", "donotreply@",
    ],
    "junk_subjects": [
        "unsubscribe", "% off", "deal", "sale ends",
        "your receipt", "payment confirmation", "invoice #",
        "verify your email", "confirm your",
    ],
    "always_ignore_senders": [],    # e.g. ["instagram.com", "linkedin.com"]
    "always_ignore_subjects": [],   # e.g. ["Accepted:", "FW: Your Order"]
    "always_high_senders": [],      # e.g. ["boss@company.com"]
    "always_high_subjects": [],     # e.g. ["URGENT", "Action required"]
    "no_followup_subjects": [       # Sent emails that never expect a reply
        "Accepted:", "Declined:", "FW:", "RE:",
    ],
    "no_followup_to": [],           # e.g. your own email addresses
}


def load_rules() -> dict:
    if RULES_PATH.exists():
        return json.loads(RULES_PATH.read_text())
    return DEFAULT_RULES.copy()


def save_rules(rules: dict) -> None:
    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    RULES_PATH.write_text(json.dumps(rules, indent=2))


# ── Schema ────────────────────────────────────────────────────────────────────

def init_feedback_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id        TEXT,
            subject         TEXT,
            from_addr       TEXT,
            original_priority   TEXT,
            original_category   TEXT,
            corrected_priority  TEXT,
            corrected_category  TEXT,
            feedback_type   TEXT,   -- 'wrong_priority' | 'wrong_category' | 'should_filter' | 'should_not_filter'
            note            TEXT,
            rule_applied    INTEGER DEFAULT 0,
            created_at      TEXT NOT NULL
        )
    """)
    conn.commit()


# ── Review recent emails ──────────────────────────────────────────────────────

PRIORITY_MAP = {"h": "high", "m": "medium", "l": "low", "i": "ignore"}
CATEGORY_MAP = {
    "1": "action_required", "2": "needs_reply", "3": "fyi",
    "4": "newsletter",      "5": "notification", "6": "spam",
}

def review_recent(conn: sqlite3.Connection, limit: int = 20) -> None:
    rows = conn.execute("""
        SELECT email_id, from_addr, subject, priority, category, summary, action_items
        FROM analyzed_emails
        ORDER BY analyzed_at DESC
        LIMIT ?
    """, (limit,)).fetchall()

    if not rows:
        print("No analyzed emails found. Run the pipeline first.")
        return

    rules = load_rules()
    now   = datetime.now(timezone.utc).isoformat()

    print(f"\n{'='*65}")
    print(f"  FEEDBACK REVIEW — Last {len(rows)} analyzed emails")
    print(f"  Commands: [k] keep  [f] filter  [p] fix priority  [c] fix category  [s] skip all")
    print(f"{'='*65}\n")

    feedback_count = 0

    for row in rows:
        email_id, from_addr, subject, priority, category, summary, action_items_raw = row
        action_items = json.loads(action_items_raw or "[]")

        icon = {"high": "🔴", "medium": "🟡", "low": "🔵", "ignore": "⚪"}.get(priority, "⚫")
        print(f"{icon} [{priority.upper():<6}] [{category}]")
        print(f"   From:    {from_addr[:65]}")
        print(f"   Subject: {subject[:65]}")
        print(f"   Summary: {summary[:100]}")
        if action_items:
            print(f"   Actions: {', '.join(action_items[:2])}")
        print()
        print("   [k] keep  [f] should be filtered  [p] fix priority  [c] fix category  [s] stop reviewing")
        choice = input("   → ").strip().lower()
        print()

        if choice == "s":
            break
        elif choice == "k":
            continue
        elif choice == "f":
            # Add sender domain or subject keyword to always_ignore
            print(f"   Add to ignore list:")
            print(f"   [1] Sender: {from_addr}")
            print(f"   [2] Subject keyword (you'll enter it)")
            sub = input("   → ").strip()
            if sub == "1":
                domain = from_addr.split("@")[-1].rstrip(">").strip() if "@" in from_addr else from_addr
                rules["always_ignore_senders"].append(domain)
                save_rules(rules)
                print(f"   ✓ Added '{domain}' to always_ignore_senders\n")
            elif sub == "2":
                kw = input("   Keyword to filter: ").strip()
                if kw:
                    rules["always_ignore_subjects"].append(kw)
                    save_rules(rules)
                    print(f"   ✓ Added '{kw}' to always_ignore_subjects\n")
            _save_feedback(conn, email_id, subject, from_addr, priority, category,
                           None, None, "should_filter", "", now)
            feedback_count += 1

        elif choice == "p":
            print("   Correct priority: [h] high  [m] medium  [l] low  [i] ignore")
            p = input("   → ").strip().lower()
            correct = PRIORITY_MAP.get(p)
            if correct and correct != priority:
                _save_feedback(conn, email_id, subject, from_addr, priority, category,
                               correct, None, "wrong_priority", "", now)
                print(f"   ✓ Logged: {priority} → {correct}\n")
                feedback_count += 1
            else:
                print("   → No change.\n")

        elif choice == "c":
            print("   Correct category:")
            for k, v in CATEGORY_MAP.items():
                print(f"   [{k}] {v}")
            c = input("   → ").strip().lower()
            correct = CATEGORY_MAP.get(c)
            if correct and correct != category:
                _save_feedback(conn, email_id, subject, from_addr, priority, category,
                               None, correct, "wrong_category", "", now)
                print(f"   ✓ Logged: {category} → {correct}\n")
                feedback_count += 1
            else:
                print("   → No change.\n")

    print(f"{'─'*65}")
    print(f"  {feedback_count} feedback entries recorded.")
    print(f"  Run with --suggest to let Claude propose rule improvements.\n")


def _save_feedback(conn, email_id, subject, from_addr, orig_p, orig_c,
                   corr_p, corr_c, fb_type, note, now):
    conn.execute("""
        INSERT INTO feedback
        (email_id, subject, from_addr, original_priority, original_category,
         corrected_priority, corrected_category, feedback_type, note, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (email_id, subject, from_addr, orig_p, orig_c, corr_p, corr_c, fb_type, note, now))
    conn.commit()


# ── Claude rule suggestions ───────────────────────────────────────────────────

def suggest_improvements(conn: sqlite3.Connection) -> None:
    rows = conn.execute("""
        SELECT subject, from_addr, original_priority, original_category,
               corrected_priority, corrected_category, feedback_type, note
        FROM feedback
        WHERE rule_applied = 0
        ORDER BY created_at DESC
        LIMIT 50
    """).fetchall()

    if not rows:
        print("No unreviewed feedback found. Use the review tool first.")
        return

    rules = load_rules()
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    feedback_text = "\n".join([
        f"- Subject: '{r[0]}' | From: {r[1]} | "
        f"Was: {r[2]}/{r[3]} → Correct: {r[4] or '—'}/{r[5] or '—'} | Type: {r[6]}"
        for r in rows
    ])

    prompt = f"""You are helping improve email filtering rules for a personal AI email assistant.

Here is feedback the user has provided on recent email analyses:
{feedback_text}

Current filter rules:
{json.dumps(rules, indent=2)}

Based on this feedback, suggest concrete improvements to the filter rules.
Return a JSON object with only the keys that should be changed, and explain each change briefly.
Format:
{{
  "changes": {{
    "always_ignore_senders": ["new_domain.com"],
    "junk_subjects": ["new keyword"]
  }},
  "explanations": [
    "Added new_domain.com because user flagged 3 emails from this sender as junk"
  ]
}}
Return raw JSON only. No markdown."""

    print("\nAsking Claude to analyze feedback patterns...\n")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:-1])

    try:
        suggestion = json.loads(raw)
    except json.JSONDecodeError:
        print(f"Could not parse Claude's response:\n{raw}")
        return

    changes      = suggestion.get("changes", {})
    explanations = suggestion.get("explanations", [])

    print(f"{'='*65}")
    print("  SUGGESTED RULE IMPROVEMENTS")
    print(f"{'='*65}\n")
    for exp in explanations:
        print(f"  • {exp}")
    print(f"\n  Proposed changes:")
    print(json.dumps(changes, indent=4))
    print()
    apply = input("  Apply these changes? [y/n] → ").strip().lower()

    if apply == "y":
        for key, values in changes.items():
            if key in rules and isinstance(rules[key], list):
                existing = set(rules[key])
                for v in values:
                    if v not in existing:
                        rules[key].append(v)
        save_rules(rules)
        # Mark feedback as applied
        conn.execute("UPDATE feedback SET rule_applied=1 WHERE rule_applied=0")
        conn.commit()
        print(f"\n  ✓ Rules updated and saved to {RULES_PATH}\n")
    else:
        print("  → No changes made.\n")


# ── Show current rules ────────────────────────────────────────────────────────

def show_rules() -> None:
    rules = load_rules()
    print(f"\n{'='*65}")
    print("  CURRENT FILTER RULES")
    print(f"{'='*65}\n")
    for key, values in rules.items():
        if values:
            print(f"  {key}:")
            for v in values:
                print(f"    - {v}")
        else:
            print(f"  {key}: (empty)")
        print()


# ── Main ──────────────────────────────────────────────────────────────────────

def run(suggest: bool = False, show: bool = False, limit: int = 20):
    if not DB_PATH.exists():
        print("No database found. Run the pipeline first.")
        return

    conn = sqlite3.connect(DB_PATH)
    init_feedback_table(conn)

    if show:
        show_rules()
    elif suggest:
        suggest_improvements(conn)
    else:
        review_recent(conn, limit=limit)

    conn.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    run(
        suggest = "--suggest" in args,
        show    = "--rules"   in args,
        limit   = 20,
    )
