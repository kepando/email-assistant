# Email Assistant

A personal AI-powered email triage tool that connects to Outlook via Microsoft Graph API, analyzes emails with Claude, and surfaces a prioritized inbox through a local web dashboard.

## What it does

- **Ingests** emails from Outlook (Microsoft Graph API, OAuth2)
- **Analyzes** each email with Claude — assigns priority, category, summary, action items, reply urgency, job opportunity flag, and sentiment
- **Stores** results in a local SQLite database
- **Labels** emails back in Outlook with color-coded `EA:` categories
- **Posts** a daily digest to a Notion database
- **Serves** a local Flask dashboard at `http://localhost:5001`

## Dashboard features

- Inbox view sorted by priority (high → medium → low)
- Task tracker with open/complete actions
- Pending replies queue with urgency ordering
- Follow-up tracker for sent emails with no reply after 3 days
- One-click draft reply generation (saves draft to Outlook via Graph API)
- Archive / undo-archive emails directly from the UI
- Feedback system to correct Claude's priority/category labels (used as few-shot examples in future runs)
- Trigger the pipeline manually from the UI with live log streaming

## Setup

### 1. Prerequisites

- Python 3.9+
- An Azure AD app registration with `Mail.ReadWrite` scope (personal Microsoft account)
- Anthropic API key
- Notion integration token + database ID (optional)

### 2. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install flask anthropic msal requests python-dotenv
```

### 3. Configure environment

Create a `.env` file in the project root:

```env
ANTHROPIC_API_KEY=sk-ant-...
OUTLOOK_EMAIL=you@outlook.com
AZURE_CLIENT_ID=your-azure-app-client-id
NOTION_TOKEN=secret_...          # optional
NOTION_DIGEST_DB=your-db-id      # optional
```

### 4. First run (browser auth)

The first pipeline run will open a browser for Microsoft OAuth consent. The token is cached in `.token_cache.json` for subsequent runs.

```bash
python src/pipeline.py
```

### 5. Start the dashboard

```bash
python src/app.py
```

Open `http://localhost:5001`.

## Project structure

```
src/
  app.py        # Flask web dashboard + REST API
  pipeline.py   # Orchestrator: ingest → analyze → store → label → Notion
  ingest.py     # Microsoft Graph API email fetcher (OAuth2 via MSAL)
  analyze.py    # Claude batch analysis with local pre-filtering
  tasks.py      # Task / pending-reply / follow-up sync
  reply.py      # Reply draft generation
  followup.py   # Follow-up tracking
  briefing.py   # Morning briefing generator
  feedback.py   # User correction helpers
prompts/
  analyze_emails.txt   # System prompt for Claude email analysis
  draft_reply.txt      # Prompt for reply drafting
templates/
  index.html    # Single-page dashboard UI
data/           # Runtime data (gitignored)
  db/           # SQLite database
  output/       # JSON analysis snapshots
```

## Pipeline steps

1. Fetch latest 50 emails from Outlook
2. Deduplicate against `seen_emails` table
3. Pre-filter obvious junk locally (no API cost)
4. Batch-analyze new emails with Claude
5. Store results in SQLite
6. Apply `EA:` color labels to emails in Outlook
7. Sync tasks, pending replies, and follow-ups
8. Post digest page to Notion

## Outlook categories

The pipeline creates `EA:`-prefixed categories in Outlook's master category list:

| Category | Color |
|---|---|
| EA: high | Red |
| EA: medium | Yellow |
| EA: low | Blue |
| EA: action_required | Red |
| EA: needs_reply | Orange |
| EA: newsletter | Purple |
| EA: job_opportunity | Green |
| EA: ignore / spam | Gray |
