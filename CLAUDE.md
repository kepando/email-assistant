# CLAUDE.md — Email Assistant

Standing context, permissions, and preferences for this project.

## Project overview

Personal AI email triage tool. Fetches emails from Outlook (Microsoft Graph API), analyzes them with Claude, stores results in SQLite, and serves a local Flask dashboard at `http://localhost:5001`. Optionally posts daily digests to Notion.

Key files:
- `src/app.py` — Flask dashboard + REST API
- `src/pipeline.py` — main orchestrator (ingest → analyze → store → label → Notion)
- `src/ingest.py` — Microsoft Graph / MSAL OAuth2
- `src/analyze.py` — Claude batch analysis
- `.env` — secrets (never commit)
- `data/` — runtime data (gitignored)

## Permissions

These actions are pre-approved — no need to ask each time:

- Read any file in this project
- Edit source files (`src/`, `prompts/`, `templates/`, `static/`)
- Run `python src/pipeline.py` or `python src/app.py`
- Run `pip install` within `.venv`
- Run `git add`, `git commit`, `git push` for normal commits
- Create and push to new branches
- `Bash(gh repo:*)`
- `Bash(git --version)`
- `Bash(brew install:*)`
- `Bash(gh auth:*)`
- `Bash(git init:*)`
- `Bash(find /Users/ken/Projects/email-assistant -name *.env -o -name .env* -o -name *.key -o -name *.pem -o -name credentials* -o -name secrets*)`
- `Bash(git add:*)`
- `Bash(git commit:*)`
- `Bash(git push:*)`
- `Skill(update-config)`
- `Bash(python3 /Users/ken/Projects/email-assistant/.claude/sync_permissions.py)`
- `Bash(jq:*)`

## Preferences

- Keep responses short and direct — no trailing summaries of what was just done
- Don't add docstrings, comments, or type annotations to code that wasn't changed
- Don't introduce abstractions or helpers for one-off things
- Prefer editing existing files over creating new ones
- When writing Python, match the existing style (no f-string over-engineering, keep it readable)
- Sign git commits with: `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>`

## Environment

- Python 3.9, venv at `.venv/`
- Flask + SQLite (no ORM)
- Anthropic SDK (`anthropic` package)
- MSAL for Microsoft OAuth2
- `.env` holds: `ANTHROPIC_API_KEY`, `OUTLOOK_EMAIL`, `AZURE_CLIENT_ID`, `NOTION_TOKEN`, `NOTION_DIGEST_DB`
