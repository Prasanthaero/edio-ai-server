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
EDIO_APP_TOKEN   = os.environ.get("EDIO_APP_TOKEN", "")
EDIO_ADMIN_TOKEN = os.environ.get("EDIO_ADMIN_TOKEN", "")
GEMINI_MODEL     = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

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
    "You are an expert TV/embedded repair engineer. Analyse the UART/serial "
    "boot log and return a repair diagnosis. Base every claim on the log; if "
    "unsure, say UNKNOWN. Respond with ONLY valid JSON in exactly this schema "
    "(no prose outside JSON):\n"
    '{"overall_status":"PASS|WARNING|FAILED|UNKNOWN",'
    '"platform":{"soc":"","bootloader":"","operating_system":""},'
    '"boot_stage":"","confirmed_findings":[],"critical_errors":[],"warnings":[],'
    '"probable_fault_area":"","probable_root_cause":"","possible_causes":[],'
    '"recommended_checks":[],"recommended_repair_actions":[],'
    '"useful_uart_commands":[],"confidence":0,'
    '"evidence":[{"finding":"","lines":[]}],"unknown_information":[]}'
)


def call_gemini(uart_log):
    """Call Gemini using the OFFICIAL google-generativeai SDK. The SDK handles
    the new AQ. auth keys correctly (the raw REST endpoint can time out / reject
    them). Returns (status, result_dict)."""
    if not GEMINI_API_KEY:
        return "error", {"error": "SERVER_NO_KEY",
                         "message": "AI key not configured on the server."}
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(
            GEMINI_MODEL,
            system_instruction=GEMINI_SYSTEM)
        resp = model.generate_content(
            "UART LOG:\n\n" + uart_log,
            generation_config={"response_mime_type": "application/json",
                               "temperature": 0.2})
        text = resp.text
        try:
            result = json.loads(text)
        except Exception:
            result = {"overall_status": "UNKNOWN", "raw": text}
        return "ok", result
    except Exception as e:
        return "error", {"error": "AI_EXCEPTION", "message": str(e)[:400]}


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
    return jsonify({"ok": True, "service": "EDIO Smart Clone AI",
                    "model": GEMINI_MODEL})


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

    # 2) call the AI
    ai_status, ai_result = call_gemini(uart_log)

    # 3) SAVE the report (your data!)
    client_ip = request.headers.get("X-Forwarded-For",
                                    request.remote_addr or "")
    try:
        save_report(body, ai_status, ai_result, client_ip)
    except Exception as e:
        # never fail the customer just because saving failed; log it
        print("[WARN] save_report failed:", e)

    # 4) return the result to the app
    if ai_status == "ok":
        return jsonify({"ok": True, "result": ai_result})
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
        "model": GEMINI_MODEL,
        "key_present": bool(GEMINI_API_KEY),
        "key_prefix": (GEMINI_API_KEY[:6] + "...") if GEMINI_API_KEY else "",
    }
    # call Gemini via the official SDK and capture the result/error
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL)
        resp = model.generate_content("Say OK")
        info["ok"] = True
        info["gemini_reply"] = resp.text[:500]
    except Exception as e:
        info["ok"] = False
        info["error"] = str(e)[:800]
    return Response(json.dumps(info, indent=2), mimetype="application/json")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
