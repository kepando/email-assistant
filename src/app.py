"""
Phase 8 — Flask Web Dashboard
Local GUI served at http://localhost:5001
"""

import os, sys, json, sqlite3, subprocess, threading, requests
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request as freq

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

SRC_DIR   = Path(__file__).parent
BASE_DIR  = SRC_DIR.parent
DB_PATH   = BASE_DIR / "data" / "db" / "emails.db"
GRAPH_API = "https://graph.microsoft.com/v1.0"

sys.path.insert(0, str(SRC_DIR))

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)

_pipeline_running = False
_pipeline_log     = []


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_token() -> str:
    from ingest import get_access_token
    return get_access_token()

def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return col in cols

def _ensure_schema():
    """Add any columns that may be missing from older DB versions."""
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    for col in ("archived_at", "web_link"):
        if not _col_exists(conn, "analyzed_emails", col):
            try:
                conn.execute(f"ALTER TABLE analyzed_emails ADD COLUMN {col} TEXT")
                conn.commit()
                print(f"[schema] Added missing column: analyzed_emails.{col}", flush=True)
            except Exception as e:
                print(f"[schema] Could not add {col}: {e}", flush=True)
    conn.close()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/emails")
def api_emails():
    if not DB_PATH.exists():
        return jsonify([])
    conn  = get_db()
    limit = int(freq.args.get("limit", 200))
    rows  = conn.execute("""
        SELECT email_id, graph_id, from_addr, subject, received_at,
               priority, category, summary, action_items, reply_needed,
               reply_urgency, follow_up_date, job_opportunity, key_people,
               sentiment, analyzed_at, web_link
        FROM analyzed_emails
        WHERE archived_at IS NULL
        ORDER BY
          CASE priority
            WHEN 'high'   THEN 1
            WHEN 'medium' THEN 2
            WHEN 'low'    THEN 3
            ELSE 4
          END,
          received_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["action_items"] = json.loads(d["action_items"] or "[]")
        d["key_people"]   = json.loads(d["key_people"]   or "[]")
        result.append(d)
    return jsonify(result)


@app.route("/api/tasks")
def api_tasks():
    if not DB_PATH.exists():
        return jsonify([])
    conn = get_db()
    rows = conn.execute("""
        SELECT id, email_id, source, task, status, due_date, created_at
        FROM tasks WHERE status = 'open'
        ORDER BY due_date ASC, created_at ASC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/pending-replies")
def api_pending_replies():
    if not DB_PATH.exists():
        return jsonify([])
    conn = get_db()
    rows = conn.execute("""
        SELECT id, email_id, from_addr, subject, received_at, urgency, status
        FROM pending_replies WHERE status = 'pending'
        ORDER BY
          CASE urgency
            WHEN 'today'     THEN 1
            WHEN 'this_week' THEN 2
            ELSE 3
          END,
          received_at DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/followups")
def api_followups():
    if not DB_PATH.exists():
        return jsonify([])
    conn = get_db()
    rows = conn.execute("""
        SELECT id, graph_id, subject, to_addr, sent_at, reminded
        FROM sent_emails
        WHERE reply_received = 0
          AND sent_at != ''
          AND datetime(sent_at) <= datetime('now', '-3 days')
        ORDER BY sent_at ASC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


def _get_archive_folder_id(token: str) -> str:
    """Resolve the Archive folder ID, falling back to display name search."""
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{GRAPH_API}/me/mailFolders/archive", headers=headers)
    if r.status_code == 200:
        return r.json()["id"]
    # Fall back: search by display name
    r2 = requests.get(f"{GRAPH_API}/me/mailFolders", headers=headers,
                      params={"$filter": "displayName eq 'Archive'"})
    if r2.status_code == 200:
        folders = r2.json().get("value", [])
        if folders:
            return folders[0]["id"]
    raise RuntimeError(f"Could not resolve Archive folder ({r.status_code}: {r.text[:100]})")


@app.route("/api/archive", methods=["POST"])
def api_archive():
    data      = freq.json or {}
    graph_ids = data.get("graph_ids", [])
    email_ids = data.get("email_ids", [])   # needed for undo (soft-delete)
    if not graph_ids:
        return jsonify({"error": "No graph_ids provided"}), 400
    try:
        token = get_token()
    except Exception as e:
        return jsonify({"error": f"Auth failed: {e}"}), 500

    try:
        archive_folder_id = _get_archive_folder_id(token)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    headers          = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    archived, errors = [], []

    for gid in graph_ids:
        resp = requests.post(
            f"{GRAPH_API}/me/messages/{gid}/move",
            headers=headers,
            json={"destinationId": archive_folder_id},
        )
        if resp.status_code in (200, 201):
            archived.append(gid)
        elif resp.status_code == 404:
            # Email no longer exists on Microsoft's servers (expired/already deleted).
            # Treat as archived — remove it from the local inbox view.
            print(f"[archive] 404 (already gone) for {gid} — soft-deleting locally", flush=True)
            archived.append(gid)
        else:
            err_body = resp.text[:300]
            print(f"[archive] FAILED {resp.status_code} for {gid}: {err_body}", flush=True)
            try:
                err_msg = resp.json().get("error", {}).get("message", err_body)
            except Exception:
                err_msg = err_body
            errors.append({"id": gid, "error": err_msg})

    # Soft-delete: stamp archived_at so undo can restore rows
    if archived:
        conn = get_db()
        conn.execute("ALTER TABLE analyzed_emails ADD COLUMN archived_at TEXT") if not _col_exists(conn, "analyzed_emails", "archived_at") else None
        now          = datetime.now(timezone.utc).isoformat()
        placeholders = ",".join("?" * len(archived))
        conn.execute(
            f"UPDATE analyzed_emails SET archived_at=? WHERE graph_id IN ({placeholders})",
            [now] + archived,
        )
        conn.commit()
        conn.close()

    return jsonify({"archived": len(archived), "errors": errors})


@app.route("/api/unarchive", methods=["POST"])
def api_unarchive():
    """Undo archive — move emails back to Inbox and clear archived_at."""
    data      = freq.json or {}
    graph_ids = data.get("graph_ids", [])
    if not graph_ids:
        return jsonify({"error": "No graph_ids provided"}), 400
    try:
        token = get_token()
    except Exception as e:
        return jsonify({"error": f"Auth failed: {e}"}), 500

    headers         = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    restored, errors = [], []

    for gid in graph_ids:
        resp = requests.post(
            f"{GRAPH_API}/me/messages/{gid}/move",
            headers=headers,
            json={"destinationId": "inbox"},
        )
        if resp.status_code in (200, 201):
            restored.append(gid)
        else:
            errors.append({"id": gid, "error": resp.json().get("error", {}).get("message", resp.text[:120])})

    if restored:
        conn         = get_db()
        placeholders = ",".join("?" * len(restored))
        conn.execute(
            f"UPDATE analyzed_emails SET archived_at=NULL WHERE graph_id IN ({placeholders})",
            restored,
        )
        conn.commit()
        conn.close()

    return jsonify({"restored": len(restored), "errors": errors})


@app.route("/api/tasks/uncomplete", methods=["POST"])
def api_task_uncomplete():
    """Undo task completion."""
    task_id = (freq.json or {}).get("task_id")
    if not task_id:
        return jsonify({"error": "No task_id"}), 400
    conn = get_db()
    conn.execute("UPDATE tasks SET status='open', updated_at=? WHERE id=?",
                 (datetime.now(timezone.utc).isoformat(), task_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/draft-replies", methods=["POST"])
def api_draft_replies():
    data      = freq.json or {}
    email_ids = data.get("email_ids", [])
    if not email_ids:
        return jsonify({"error": "No email_ids provided"}), 400

    conn         = get_db()
    placeholders = ",".join("?" * len(email_ids))
    rows         = conn.execute(
        f"SELECT email_id, graph_id, from_addr, subject, summary "
        f"FROM analyzed_emails WHERE email_id IN ({placeholders})",
        email_ids,
    ).fetchall()
    conn.close()

    if not rows:
        return jsonify({"error": "No emails found"}), 404

    try:
        token = get_token()
    except Exception as e:
        return jsonify({"error": f"Auth failed: {e}"}), 500

    import anthropic
    client  = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    drafts  = []

    for row in rows:
        r      = dict(row)
        prompt = (
            f"Draft a concise, professional reply to this email.\n\n"
            f"From: {r['from_addr']}\n"
            f"Subject: {r['subject']}\n"
            f"Summary: {r['summary']}\n\n"
            f"Keep it brief and professional. Sign off as Ken. Plain text only."
        )
        resp       = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        draft_body       = resp.content[0].text.strip()
        outlook_draft_id = None

        try:
            cr = requests.post(
                f"{GRAPH_API}/me/messages/{r['graph_id']}/createReply",
                headers=headers, json={}
            )
            if cr.status_code == 201:
                did = cr.json()["id"]
                requests.patch(
                    f"{GRAPH_API}/me/messages/{did}", headers=headers,
                    json={"body": {"contentType": "Text", "content": draft_body}}
                )
                outlook_draft_id = did
        except Exception:
            pass

        drafts.append({
            "email_id":        r["email_id"],
            "subject":         r["subject"],
            "draft_body":      draft_body,
            "outlook_draft_id": outlook_draft_id,
            "saved_to_outlook": outlook_draft_id is not None,
        })

    return jsonify({"drafts": drafts})


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    data = freq.json or {}
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT, subject TEXT, from_addr TEXT,
            original_priority TEXT, original_category TEXT,
            correct_priority TEXT, correct_category TEXT,
            note TEXT, created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT INTO feedback
        (email_id, subject, from_addr, original_priority, original_category,
         correct_priority, correct_category, note, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (
        data.get("email_id",""), data.get("subject",""), data.get("from_addr",""),
        data.get("original_priority",""), data.get("original_category",""),
        data.get("correct_priority",""), data.get("correct_category",""),
        data.get("note",""), datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/complete", methods=["POST"])
def api_task_complete():
    task_id = (freq.json or {}).get("task_id")
    if not task_id:
        return jsonify({"error": "No task_id"}), 400
    conn = get_db()
    conn.execute("UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                 (datetime.now(timezone.utc).isoformat(), task_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/pipeline/run", methods=["POST"])
def api_pipeline_run():
    global _pipeline_running, _pipeline_log
    if _pipeline_running:
        return jsonify({"error": "Pipeline already running"}), 409
    _pipeline_log     = []
    _pipeline_running = True

    def _run():
        global _pipeline_running, _pipeline_log
        try:
            proc = subprocess.Popen(
                [sys.executable, str(SRC_DIR / "pipeline.py")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in proc.stdout:
                _pipeline_log.append(line.rstrip())
            proc.wait()
        finally:
            _pipeline_running = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/pipeline/status")
def api_pipeline_status():
    return jsonify({"running": _pipeline_running, "log": _pipeline_log[-50:]})


if __name__ == "__main__":
    _ensure_schema()
    print("\n📬 Email Assistant  →  http://localhost:5001\n")
    app.run(host="127.0.0.1", port=5001, debug=False)
