"""
Phase 2 — Email Ingestion
Fetches recent emails from Outlook via Microsoft Graph API using OAuth2.
Token is cached locally — browser auth is only needed once.
"""

import os
import re
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv
import msal
import requests

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

CLIENT_ID     = os.environ.get("AZURE_CLIENT_ID")
EMAIL_ADDRESS = os.environ.get("OUTLOOK_EMAIL")
AUTHORITY     = "https://login.microsoftonline.com/consumers"
SCOPES        = ["Mail.ReadWrite"]

RAW_PATH       = Path(__file__).parent.parent / "data" / "raw"
TOKEN_CACHE    = Path(__file__).parent.parent / ".token_cache.json"
GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0/me/messages"
FETCH_LIMIT    = 50
BODY_MAX_CHARS = 2000

RAW_PATH.mkdir(parents=True, exist_ok=True)


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ── Auth ─────────────────────────────────────────────────────────────────────

def load_token_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE.exists():
        cache.deserialize(TOKEN_CACHE.read_text())
    return cache


def save_token_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        TOKEN_CACHE.write_text(cache.serialize())


def get_access_token() -> str:
    """Get a valid access token, prompting device-flow login if needed."""
    cache = load_token_cache()
    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache,
    )

    # Try silent refresh first (uses cached token)
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            save_token_cache(cache)
            return result["access_token"]

    # First run — device code flow (user approves in browser once)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Failed to create device flow: {flow}")

    print("\n" + "─" * 60)
    print("ONE-TIME SIGN-IN REQUIRED")
    print("─" * 60)
    print(f"1. Open this URL: {flow['verification_uri']}")
    print(f"2. Enter this code: {flow['user_code']}")
    print("─" * 60 + "\n")

    result = app.acquire_token_by_device_flow(flow)  # blocks until user signs in
    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description', result)}")

    save_token_cache(cache)
    print("✓ Signed in successfully. Token cached for future runs.\n")
    return result["access_token"]


# ── Graph API ─────────────────────────────────────────────────────────────────

def fetch_messages(token: str, limit: int = FETCH_LIMIT) -> list[dict]:
    """Fetch recent messages from Microsoft Graph API."""
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "$top": limit,
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,receivedDateTime,bodyPreview,body,hasAttachments,conversationId,isRead,webLink",
    }

    response = requests.get(GRAPH_ENDPOINT, headers=headers, params=params)
    response.raise_for_status()
    return response.json().get("value", [])


def parse_message(msg: dict) -> dict:
    """Normalize a Graph API message into our standard email dict."""
    sender = msg.get("from", {}).get("emailAddress", {})
    sender_str = f"{sender.get('name', '')} <{sender.get('address', '')}>".strip()

    body_content = msg.get("body", {}).get("content", "")
    body_type    = msg.get("body", {}).get("contentType", "text")

    if body_type == "html":
        body_content = strip_html(body_content)

    body_content = body_content[:BODY_MAX_CHARS]

    return {
        "email_id":        hashlib.sha1(msg["id"].encode()).hexdigest()[:12],
        "graph_id":        msg["id"],
        "from":            sender_str,
        "subject":         msg.get("subject", "(no subject)"),
        "received_at":     msg.get("receivedDateTime", ""),
        "thread_id":       msg.get("conversationId"),
        "body":            body_content,
        "body_preview":    msg.get("bodyPreview", ""),
        "has_attachments": msg.get("hasAttachments", False),
        "is_read":         msg.get("isRead", False),
        "web_link":        msg.get("webLink", ""),
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def save_emails(emails: list[dict]) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_file = RAW_PATH / f"inbox_{ts}.json"
    out_file.write_text(json.dumps(emails, indent=2))
    return out_file


def fetch_emails(limit: int = FETCH_LIMIT) -> list[dict]:
    """Public entry point used by pipeline.py: authenticate, fetch, parse."""
    if not CLIENT_ID:
        raise RuntimeError("Set AZURE_CLIENT_ID in your .env file")
    token = get_access_token()
    raw   = fetch_messages(token, limit=limit)
    return [parse_message(m) for m in raw]


def main():
    if not CLIENT_ID:
        print("ERROR: Set AZURE_CLIENT_ID in your .env file")
        return

    print(f"Fetching emails for {EMAIL_ADDRESS}...")
    emails = fetch_emails()

    if not emails:
        print("No emails found.")
        return

    out_file = save_emails(emails)
    print(f"  → Fetched {len(emails)} emails → {out_file}\n")

    # Preview newest 5
    print("Preview (newest first):")
    for e in emails[:5]:
        read    = " " if e["is_read"] else "●"
        attach  = " 📎" if e["has_attachments"] else ""
        date    = e["received_at"][:10]
        sender  = e["from"][:28]
        subject = e["subject"][:48]
        print(f"  {read} {date}  {sender:<28}  {subject}{attach}")


if __name__ == "__main__":
    main()
