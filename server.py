#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py — EDIO Smart Clone AI backend
=======================================
This runs on YOUR server (Render / any VPS). The desktop app POSTs each UART
log here; this server:

  1. Checks the app token (so random people can't use your endpoint).
  2. Calls Gemini with YOUR key (the key stays HERE, never in the app).
  3. SAVES every analysis report to a database (this is what you asked for —
     you get ALL the data: who, which device, the log, and the diagnosis).
  4. Returns the diagnosis to the customer's app.

You get an admin page to see every report: GET /admin/reports?token=ADMIN_TOKEN

--------------------------------------------------------------------------
ENVIRONMENT VARIABLES to set on Render (Settings → Environment):
    GEMINI_API_KEY   = AIzaSy...        (your Google Gemini key)
    EDIO_APP_TOKEN   = long-random-1    (must match config.AI_APP_TOKEN in app)
    EDIO_ADMIN_TOKEN = long-random-2    (your private key to view reports)
    GEMINI_MODEL     = gemini-flash-latest   (optional)
--------------------------------------------------------------------------
Run locally:   pip install flask requests
               python server.py
On Render:     Start command →  gunicorn server:app
"""

import json
import os
import sqlite3
import time
import uuid

import requests
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# ── config from environment (NEVER hardcode secrets) ──
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL     = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# Groq (free, fast) — if GROQ_API_KEY is set, the server uses Groq instead of
# Gemini. Get a free key at https://console.groq.com/keys
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL       = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
# which provider to use: "groq" if a Groq key is present, else "gemini"
AI_PROVIDER      = os.environ.get("AI_PROVIDER",
                                  "groq" if GROQ_API_KEY else "gemini")
EDIO_APP_TOKEN   = os.environ.get("EDIO_APP_TOKEN", "")
EDIO_ADMIN_TOKEN = os.environ.get("EDIO_ADMIN_TOKEN", "")

# ── database (SQLite file). On Render, mount a Disk so it persists. ──
DB_PATH = os.environ.get("EDIO_DB_PATH",
                         os.path.join(os.path.dirname(__file__), "reports.db"))


def _db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = _db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           INTEGER,
            created_at   TEXT,
            product      TEXT,
            app_version  TEXT,
            device_id    TEXT,
            license_key  TEXT,
            session_id   TEXT,
            uart_log     TEXT,
            meta         TEXT,
            ai_status    TEXT,
            ai_result    TEXT,
            client_ip    TEXT
        )
    """)
    con.commit()
    con.close()


init_db()


# ── the AI system prompt (same schema your app expects) ──
GEMINI_SYSTEM = (
    "You are a master TV/monitor motherboard repair engineer. A technician "
    "gives you the FULL UART/serial boot log of a TV mainboard. Read the WHOLE "
    "log carefully, then give a clear final verdict and a REAL, practical "
    "solution the technician can act on. Base every claim on the log; if the "
    "log does not show something, say UNKNOWN — never invent.\n\n"
    "YOUR JOB (in this order):\n"
    "1. Decide the board VERDICT: is the motherboard GOOD, or FAULTY/FAILED? "
    "Set 'verdict' to exactly 'BOARD GOOD', 'BOARD FAULTY', or 'NEEDS MORE "
    "DATA'. Put this in 'verdict'.\n"
    "2. In 'verdict_reason', explain the verdict in ONE plain sentence a "
    "technician understands (e.g. 'Board boots to U-Boot but the OS image in "
    "eMMC is corrupt, so it never starts Android').\n"
    "3. Set 'overall_status' to PASS (board fully OK), WARNING (boots but a "
    "problem), FAILED (does not work), or UNKNOWN.\n"
    "4. Give a REAL step-by-step solution in 'recommended_repair_actions', "
    "ordered from the cheapest/most-likely fix to the last resort. Be specific: "
    "which partition to reflash, which rail to measure, which chip to reball or "
    "replace. Board/eMMC replacement is ALWAYS the LAST step.\n"
    "5. Set 'confidence' 0-100 = how sure you are of the verdict, based on how "
    "much the log shows.\n\n"
    "WRITING RULES:\n"
    "- Write for a technician on the bench, not an engineer. Plain simple "
    "words, short sentences. Each list item under ~15 words.\n"
    "- 'useful_uart_commands' = exact console commands to run next, in order, "
    "with a 2-3 word note each.\n"
    "- Put the single most important next step FIRST in "
    "recommended_repair_actions.\n\n"
    "Respond with ONLY valid JSON in exactly this schema (no prose outside "
    "JSON):\n"
    '{"verdict":"BOARD GOOD|BOARD FAULTY|NEEDS MORE DATA",'
    '"verdict_reason":"",'
    '"overall_status":"PASS|WARNING|FAILED|UNKNOWN",'
    '"platform":{"soc":"","bootloader":"","operating_system":""},'
    '"boot_stage":"","confirmed_findings":[],"critical_errors":[],"warnings":[],'
    '"probable_fault_area":"","probable_root_cause":"","possible_causes":[],'
    '"recommended_checks":[],"recommended_repair_actions":[],'
    '"useful_uart_commands":[],"confidence":0,'
    '"evidence":[{"finding":"","lines":[]}],"unknown_information":[]}'
)


def _system_for(language):
    """Return the system prompt, adding a 'reply in this language' instruction
    when a non-English language is requested. The JSON keys stay English; only
    the human-readable values are translated."""
    lang = (language or "English").strip()
    if not lang or lang.lower() in ("english", "en"):
        return GEMINI_SYSTEM
    return (GEMINI_SYSTEM +
            f"\n\nIMPORTANT: Write ALL human-readable text values in the JSON "
            f"(findings, causes, checks, repair actions, command notes, fault "
            f"area, root cause, etc.) in {lang}. Keep the JSON keys and the "
            f"status words (PASS/WARNING/FAILED/UNKNOWN) in English, and keep "
            f"technical tokens (command names, register names, part numbers) "
            f"unchanged. Everything a technician reads must be in {lang}.")


def call_groq(uart_log, language="English"):
    """Call Groq (free, fast) with an OpenAI-compatible REST API. Returns
    (status, result_dict)."""
    if not GROQ_API_KEY:
        return "error", {"error": "SERVER_NO_KEY",
                         "message": "Groq key not configured on the server."}
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + GROQ_API_KEY}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": _system_for(language)},
            {"role": "user", "content": "UART LOG:\n\n" + uart_log},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        if r.status_code != 200:
            return "error", {"error": "AI_HTTP_%d" % r.status_code,
                             "message": r.text[:400]}
        data = r.json()
        text = data["choices"][0]["message"]["content"]
        try:
            result = json.loads(text)
        except Exception:
            result = {"overall_status": "UNKNOWN", "raw": text}
        return "ok", result
    except Exception as e:
        return "error", {"error": "AI_EXCEPTION", "message": str(e)[:400]}


def call_gemini(uart_log, language="English"):
    """Call Gemini via the lightweight REST API (no heavy SDK — the SDK uses
    too much memory for Render's free plan). Sends the key as a header so the
    new AQ. auth keys work. Returns (status, result_dict)."""
    if not GEMINI_API_KEY:
        return "error", {"error": "SERVER_NO_KEY",
                         "message": "AI key not configured on the server."}
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    headers = {"Content-Type": "application/json",
               "x-goog-api-key": GEMINI_API_KEY}
    payload = {
        "system_instruction": {"parts": [{"text": _system_for(language)}]},
        "contents": [{"role": "user",
                      "parts": [{"text": "UART LOG:\n\n" + uart_log}]}],
        "generationConfig": {"response_mime_type": "application/json",
                             "temperature": 0.2},
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=90)
        if r.status_code != 200:
            return "error", {"error": "AI_HTTP_%d" % r.status_code,
                             "message": r.text[:400]}
        data = r.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        try:
            result = json.loads(text)
        except Exception:
            result = {"overall_status": "UNKNOWN", "raw": text}
        return "ok", result
    except Exception as e:
        return "error", {"error": "AI_EXCEPTION", "message": str(e)[:400]}


def call_ai(uart_log, language="English"):
    """Route to the configured AI provider (Groq preferred if a key is set)."""
    if AI_PROVIDER == "groq" and GROQ_API_KEY:
        return call_groq(uart_log, language)
    return call_gemini(uart_log, language)


def save_report(body, ai_status, ai_result, client_ip):
    """Save EVERY analysis to the database — this is your data."""
    con = _db()
    con.execute(
        """INSERT INTO reports
           (ts, created_at, product, app_version, device_id, license_key,
            session_id, uart_log, meta, ai_status, ai_result, client_ip)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(time.time()),
         time.strftime("%Y-%m-%d %H:%M:%S"),
         body.get("product", ""),
         body.get("app_version", ""),
         body.get("device_id", ""),
         body.get("license_key", ""),
         body.get("session_id", ""),
         body.get("uart_log", ""),
         json.dumps(body.get("meta", {})),
         ai_status,
         json.dumps(ai_result),
         client_ip))
    con.commit()
    con.close()


# ── health check ──
@app.route("/", methods=["GET"])
@app.route("/ai", methods=["GET"])
def home():
    model = GROQ_MODEL if AI_PROVIDER == "groq" else GEMINI_MODEL
    return jsonify({"ok": True, "service": "EDIO Smart Clone AI",
                    "provider": AI_PROVIDER, "model": model})


# ── the main endpoint the app calls ──
@app.route("/ai/analyze-uart", methods=["POST"])
def analyze_uart():
    # 1) check the app token (stops outsiders using your AI)
    token = request.headers.get("X-EDIO-Token", "")
    if EDIO_APP_TOKEN and token != EDIO_APP_TOKEN:
        return jsonify({"ok": False, "error": "AUTHENTICATION_ERROR",
                        "message": "Invalid app token."}), 403

    body = request.get_json(silent=True) or {}
    uart_log = (body.get("uart_log") or "").strip()
    if not uart_log:
        return jsonify({"ok": False, "error": "EMPTY_LOG",
                        "message": "No UART log provided."}), 400

    # (Optional) per-license limit could go here — see LIMIT note below.

    # 2) call the AI (Groq if configured, else Gemini)
    language = (body.get("language") or "English").strip() or "English"
    ai_status, ai_result = call_ai(uart_log, language)

    # 3) SAVE the report (your data!)
    client_ip = request.headers.get("X-Forwarded-For",
                                    request.remote_addr or "")
    try:
        save_report(body, ai_status, ai_result, client_ip)
    except Exception as e:
        # never fail the customer just because saving failed; log it
        print("[WARN] save_report failed:", e)

    # 4) return the result to the app. The app expects the diagnosis under the
    # key "report" (and a session_id), so we send it in that exact shape.
    if ai_status == "ok":
        return jsonify({"ok": True, "report": ai_result,
                        "session_id": body.get("session_id", "")})
    else:
        return jsonify({"ok": False, "error": ai_result.get("error", "AI_ERROR"),
                        "message": ai_result.get("message", "")}), 502


# ── ADMIN: view all reports (only you, with the admin token) ──
@app.route("/admin/reports", methods=["GET"])
def admin_reports():
    token = request.args.get("token", "")
    if not EDIO_ADMIN_TOKEN or token != EDIO_ADMIN_TOKEN:
        return Response("Forbidden", status=403)
    limit = int(request.args.get("limit", "100"))
    con = _db()
    rows = con.execute(
        "SELECT * FROM reports ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    out = [dict(r) for r in rows]
    # pretty JSON so you can read it in a browser
    return Response(json.dumps(out, indent=2, ensure_ascii=False),
                    mimetype="application/json")


# ── ADMIN: download everything as CSV (open in Excel) ──
@app.route("/admin/export.csv", methods=["GET"])
def admin_export_csv():
    token = request.args.get("token", "")
    if not EDIO_ADMIN_TOKEN or token != EDIO_ADMIN_TOKEN:
        return Response("Forbidden", status=403)
    import csv
    import io
    con = _db()
    rows = con.execute("SELECT * FROM reports ORDER BY id DESC").fetchall()
    con.close()
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        for r in rows:
            w.writerow(dict(r))
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=edio_reports.csv"})


# ── ADMIN: simple stats ──
@app.route("/admin/stats", methods=["GET"])
def admin_stats():
    token = request.args.get("token", "")
    if not EDIO_ADMIN_TOKEN or token != EDIO_ADMIN_TOKEN:
        return Response("Forbidden", status=403)
    con = _db()
    total = con.execute("SELECT COUNT(*) c FROM reports").fetchone()["c"]
    by_dev = con.execute(
        "SELECT device_id, COUNT(*) c FROM reports GROUP BY device_id "
        "ORDER BY c DESC LIMIT 20").fetchall()
    con.close()
    return jsonify({"total_reports": total,
                    "top_devices": [dict(r) for r in by_dev]})


# ── DEBUG: test Gemini directly and show the REAL error (admin only) ──
# Open in a browser:  /admin/test-ai?token=YOUR_ADMIN_TOKEN
# It sends a tiny test log to Gemini and shows exactly what Gemini replies,
# so you can see the real error (bad key, wrong model, quota, etc.).
@app.route("/admin/test-ai", methods=["GET"])
def admin_test_ai():
    token = request.args.get("token", "")
    if not EDIO_ADMIN_TOKEN or token != EDIO_ADMIN_TOKEN:
        return Response("Forbidden", status=403)
    info = {
        "provider": AI_PROVIDER,
        "groq_key_present": bool(GROQ_API_KEY),
        "gemini_key_present": bool(GEMINI_API_KEY),
        "model": GROQ_MODEL if AI_PROVIDER == "groq" else GEMINI_MODEL,
    }
    # run a tiny real analysis through the active provider
    status, result = call_ai("U-Boot test log. Say OK.")
    info["status"] = status
    info["result"] = result
    return Response(json.dumps(info, indent=2), mimetype="application/json")


# ── ADMIN: a nice HTML dashboard (open in a browser) ──
# /admin/dashboard?token=YOUR_ADMIN_TOKEN
@app.route("/admin/dashboard", methods=["GET"])
def admin_dashboard():
    token = request.args.get("token", "")
    if not EDIO_ADMIN_TOKEN or token != EDIO_ADMIN_TOKEN:
        return Response("Forbidden", status=403)
    con = _db()
    total = con.execute("SELECT COUNT(*) c FROM reports").fetchone()["c"]
    rows = con.execute(
        "SELECT id, created_at, product, app_version, device_id, license_key, "
        "ai_status, client_ip FROM reports ORDER BY id DESC LIMIT 500"
    ).fetchall()
    con.close()

    trs = []
    for r in rows:
        trs.append(
            "<tr>"
            f"<td>{r['id']}</td>"
            f"<td>{r['created_at'] or ''}</td>"
            f"<td>{r['device_id'] or ''}</td>"
            f"<td>{r['license_key'] or ''}</td>"
            f"<td>{r['app_version'] or ''}</td>"
            f"<td><span class='st {r['ai_status']}'>{r['ai_status'] or ''}</span></td>"
            f"<td>{r['client_ip'] or ''}</td>"
            f"<td><a class='btn' href='/admin/report/{r['id']}?token={token}' "
            "target='_blank'>View</a></td>"
            "</tr>")
    body = "".join(trs) or "<tr><td colspan=8>No reports yet.</td></tr>"

    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>EDIO Smart Clone - Reports</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<style>"
        "body{font-family:Segoe UI,Arial,sans-serif;background:#0b0f17;"
        "color:#e9eef7;margin:0}"
        "header{background:#121824;padding:16px 24px;border-bottom:1px solid #26324a}"
        "h1{margin:0;font-size:20px;color:#4f7cff}"
        ".sub{color:#8b9bb4;font-size:13px;margin-top:4px}"
        ".wrap{padding:20px 24px}"
        ".bar{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}"
        "input,a.tool{background:#141b28;border:1px solid #26324a;color:#e9eef7;"
        "padding:8px 12px;border-radius:6px;text-decoration:none;font-size:13px}"
        "table{width:100%;border-collapse:collapse;font-size:13px}"
        "th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #1c2637}"
        "th{color:#8b9bb4;font-weight:600}"
        "tr:hover{background:#121824}"
        ".btn{background:#4f7cff;color:#fff;padding:5px 10px;border-radius:5px;"
        "text-decoration:none;font-size:12px}"
        ".st{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700}"
        ".st.ok{background:#123a2a;color:#2fe3c6}"
        ".st.error{background:#3a1520;color:#ff5d6c}"
        "</style></head><body>"
        "<header><h1>EDIO Smart Clone - Analysis Reports</h1>"
        f"<div class='sub'>{total} total reports - showing latest {len(rows)}</div>"
        "</header><div class='wrap'><div class='bar'>"
        "<input id='q' placeholder='Search device / license / status...' "
        "onkeyup='filt()' style='flex:1;min-width:200px'>"
        f"<a class='tool' href='/admin/export.csv?token={token}'>Download CSV</a>"
        f"<a class='tool' href='/admin/stats?token={token}' target='_blank'>Stats</a>"
        "</div><table id='t'><thead><tr><th>ID</th><th>Time</th><th>Device</th>"
        "<th>License</th><th>Ver</th><th>Status</th><th>IP</th><th></th></tr>"
        f"</thead><tbody>{body}</tbody></table></div>"
        "<script>function filt(){var q=document.getElementById('q').value"
        ".toLowerCase();var rows=document.querySelectorAll('#t tbody tr');"
        "rows.forEach(function(r){r.style.display=r.innerText.toLowerCase()"
        ".indexOf(q)>-1?'':'none';});}</script></body></html>")
    return Response(html, mimetype="text/html")


# ── ADMIN: view ONE full report (UART log + AI diagnosis) ──
@app.route("/admin/report/<int:rid>", methods=["GET"])
def admin_one_report(rid):
    token = request.args.get("token", "")
    if not EDIO_ADMIN_TOKEN or token != EDIO_ADMIN_TOKEN:
        return Response("Forbidden", status=403)
    con = _db()
    r = con.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    con.close()
    if not r:
        return Response("Not found", status=404)
    import html as _html
    try:
        ai = json.dumps(json.loads(r["ai_result"]), indent=2, ensure_ascii=False)
    except Exception:
        ai = r["ai_result"] or ""
    page = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Report #{r['id']}</title><style>"
        "body{font-family:Segoe UI,Arial;background:#0b0f17;color:#e9eef7;margin:0}"
        "header{background:#121824;padding:14px 24px;border-bottom:1px solid #26324a}"
        "a{color:#4f7cff}.wrap{padding:20px 24px;display:flex;gap:20px;"
        "flex-wrap:wrap}.col{flex:1;min-width:340px}h2{color:#4f7cff;font-size:15px}"
        "pre{background:#0a0f1a;border:1px solid #26324a;border-radius:8px;"
        "padding:14px;white-space:pre-wrap;word-break:break-word;"
        "font-family:Consolas,monospace;font-size:12px;max-height:70vh;overflow:auto}"
        ".meta td{padding:4px 10px}.meta td:first-child{color:#8b9bb4}"
        "</style></head><body>"
        f"<header><a href='/admin/dashboard?token={token}'>Back to all reports</a>"
        f" | Report #{r['id']} - {r['created_at']}</header><div class='wrap'>"
        "<div class='col'><h2>Details</h2><table class='meta'>"
        f"<tr><td>Device</td><td>{_html.escape(r['device_id'] or '')}</td></tr>"
        f"<tr><td>License</td><td>{_html.escape(r['license_key'] or '')}</td></tr>"
        f"<tr><td>App ver</td><td>{_html.escape(r['app_version'] or '')}</td></tr>"
        f"<tr><td>Status</td><td>{r['ai_status']}</td></tr>"
        f"<tr><td>IP</td><td>{r['client_ip'] or ''}</td></tr></table>"
        f"<h2>UART LOG</h2><pre>{_html.escape(r['uart_log'] or '')}</pre></div>"
        f"<div class='col'><h2>AI DIAGNOSIS</h2><pre>{_html.escape(ai)}</pre>"
        "</div></div></body></html>")
    return Response(page, mimetype="text/html")


# ═══════════════════════════════════════════════════════════════════════
#  AUTO-UPDATE — serve version.json + update files from THIS server
# ═══════════════════════════════════════════════════════════════════════
# You upload updated .py files to an "updates/" folder next to server.py and
# edit update_version.json. The app checks /update/version.json on launch, and
# if the version is newer it downloads the changed files from /update/files/…
# The customer NEVER installs a new EXE — the app updates its own .py files.
UPDATE_DIR = os.path.join(os.path.dirname(__file__), "updates")


@app.route("/update/version.json", methods=["GET"])
def update_version():
    """Return the current update manifest. Edit update_version.json in the
    updates/ folder to publish a new version."""
    manifest = os.path.join(UPDATE_DIR, "version.json")
    if os.path.exists(manifest):
        try:
            with open(manifest, "r", encoding="utf-8") as f:
                return Response(f.read(), mimetype="application/json")
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    # no update published yet
    return jsonify({"version": "0", "build": "0", "notes": "",
                    "files": []})


@app.route("/update/files/<path:fname>", methods=["GET"])
def update_file(fname):
    """Serve one update file (a .py the app will download). Only files inside
    the updates/ folder can be served (no path traversal)."""
    # security: no directory traversal, only simple filenames
    safe = os.path.basename(fname)
    if safe != fname or safe.startswith("."):
        return Response("Bad name", status=400)
    path = os.path.join(UPDATE_DIR, safe)
    if not os.path.exists(path):
        return Response("Not found", status=404)
    try:
        with open(path, "rb") as f:
            data = f.read()
        return Response(data, mimetype="application/octet-stream")
    except Exception as e:
        return Response(str(e), status=500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
