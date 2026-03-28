import json
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
import anthropic

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "analyze_emails.txt"
OUTPUT_PATH = Path(__file__).parent.parent / "data" / "output"

# Tune these to control token usage vs batch size
BODY_TRUNCATE_CHARS = 400   # max body chars sent to Claude per email
BATCH_TOKEN_BUDGET = 3000   # estimated input tokens per batch before splitting

# Senders/subjects that are never worth Claude's time
JUNK_SENDER_PATTERNS = [
    "noreply@", "no-reply@", "donotreply@",
    "notifications@", "updates@", "mailer@",
    "billing@", "invoices@", "receipts@",
]
JUNK_SUBJECT_PATTERNS = [
    "unsubscribe", "newsletter", "digest", "weekly update",
    "your receipt", "order confirmation", "shipping update",
]


def load_feedback_examples(limit: int = 8) -> str:
    """Load recent user corrections from SQLite and format as few-shot examples."""
    db_path = Path(__file__).parent.parent / "data" / "db" / "emails.db"
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("""
            SELECT subject, from_addr, original_priority, original_category,
                   correct_priority, correct_category, note
            FROM feedback
            WHERE correct_priority != '' OR correct_category != ''
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        conn.close()
    except Exception:
        return ""
    if not rows:
        return ""
    lines = ["\n\nUser corrections from previous runs — adjust your classifications to match these:"]
    for r in rows:
        subj, frm, op, oc, cp, cc, note = r
        orig = f"{op}/{oc}" if op and oc else (op or oc)
        corr = f"{cp}/{cc}" if cp and cc else (cp or cc)
        line = f'- "{subj}" from "{frm}": was {orig}, should be {corr}'
        if note:
            line += f" ({note})"
        lines.append(line)
    return "\n".join(lines)


def load_system_prompt() -> str:
    base = PROMPT_PATH.read_text()
    feedback = load_feedback_examples()
    return base + feedback


def is_junk(email: dict) -> bool:
    """Return True for emails not worth sending to Claude."""
    sender = email.get("from", "").lower()
    subject = email.get("subject", "").lower()
    if any(p in sender for p in JUNK_SENDER_PATTERNS):
        return True
    if any(p in subject for p in JUNK_SUBJECT_PATTERNS):
        return True
    return False


def slim(email: dict) -> dict:
    """Strip to only the fields Claude needs, with truncated body."""
    body = email.get("body", "")
    if len(body) > BODY_TRUNCATE_CHARS:
        body = body[:BODY_TRUNCATE_CHARS] + "…"
    return {
        "email_id": email["email_id"],
        "from": email.get("from", ""),
        "subject": email.get("subject", ""),
        "received_at": email.get("received_at", ""),
        "body": body,
    }


def chunk_by_tokens(emails: list[dict], budget: int) -> list[list[dict]]:
    """Split email list into batches estimated to stay under token budget.
    Rough heuristic: 1 token ≈ 4 chars of JSON."""
    batches, current, current_size = [], [], 0
    for email in emails:
        size = len(json.dumps(email)) // 4
        if current and current_size + size > budget:
            batches.append(current)
            current, current_size = [], 0
        current.append(email)
        current_size += size
    if current:
        batches.append(current)
    return batches


def call_claude(batch: list[dict]) -> list[dict]:
    """Single API call for one batch of slimmed emails."""
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    system_prompt = load_system_prompt()
    user_message = f"Analyze these emails and return structured JSON:\n\n{json.dumps(batch, indent=2)}"
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )
    raw = response.content[0].text.strip()
    # Strip markdown code fences if Claude wraps the response
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]       # drop opening fence line
        if raw.startswith("json"):
            raw = raw[4:]                  # drop "json" language tag
        raw = raw.rsplit("```", 1)[0]      # drop closing fence
    return json.loads(raw.strip())


def analyze_emails(emails: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Filter, slim, batch, and analyze emails.
    Returns (analyzed_results, skipped_junk).
    """
    junk = [e for e in emails if is_junk(e)]
    to_analyze = [slim(e) for e in emails if not is_junk(e)]

    results = []
    if to_analyze:
        batches = chunk_by_tokens(to_analyze, BATCH_TOKEN_BUDGET)
        print(f"  → {len(to_analyze)} emails to Claude in {len(batches)} batch(es), {len(junk)} pre-filtered")
        for i, batch in enumerate(batches, 1):
            print(f"  → Batch {i}/{len(batches)}: {len(batch)} emails")
            try:
                results.extend(call_claude(batch))
            except Exception as e:
                ids = [e["email_id"] for e in batch]
                print(f"  ⚠ Batch {i} failed ({e}) — {len(batch)} emails skipped: {ids}")
    else:
        print(f"  → All {len(junk)} emails pre-filtered, no API call needed")

    return results, junk


def print_summary(results: list[dict], junk: list[dict]) -> None:
    priority_icon = {"high": "🔴", "medium": "🟡", "low": "🔵", "ignore": "⚪"}
    for r in results:
        icon = priority_icon.get(r["priority"], "⚪")
        job = " [JOB]" if r.get("job_opportunity") else ""
        reply = " [REPLY]" if r.get("reply_needed") else ""
        print(f"{icon} [{r['category']}]{job}{reply} {r['subject'][:60]}")
        print(f"   {r['summary']}")
        for item in r.get("action_items", []):
            print(f"   → {item}")
        print()
    if junk:
        print(f"⚫ {len(junk)} junk emails skipped (no API call)")


def main():
    sample_path = Path(__file__).parent.parent / "data" / "samples" / "sample_emails.json"
    emails = json.loads(sample_path.read_text())

    print(f"Analyzing {len(emails)} emails...\n")
    results, junk = analyze_emails(emails)

    OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_PATH / "analysis.json"
    out_file.write_text(json.dumps(results, indent=2))
    print(f"\nResults saved to {out_file}\n")

    print_summary(results, junk)


if __name__ == "__main__":
    main()
