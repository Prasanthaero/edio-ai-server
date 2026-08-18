#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EDIO AI backend — UART diagnostic server
========================================
This is the SERVER that sits between the Smart Clone desktop app and Google
Gemini. It keeps your Gemini API key SAFE (the key lives here, on the server,
never in the app that customers download).

  Smart Clone app  ──HTTPS──▶  THIS SERVER  ──▶  Google Gemini
                                     │
              structured report ◀────┘

HOW TO RUN (locally, to test):
    pip install flask google-generativeai
    set GEMINI_API_KEY=your-key-here      (Windows)
    export GEMINI_API_KEY=your-key-here   (Mac/Linux)
    python server.py
  → it listens on http://localhost:8000

HOW TO DEPLOY FREE (Render.com):  see README_DEPLOY.txt

The desktop app should point at this server:
    config.py →  AI_API_URL = "https://<your-app>.onrender.com/ai"
                 AI_ENABLED = True
                 AI_DIRECT_GEMINI = False   (turn OFF direct mode)
"""

import os
import json
import time

from flask import Flask, request, jsonify

# ── Gemini setup ─────────────────────────────────────────────────────────────
# The key comes from an ENVIRONMENT VARIABLE, never hard-coded. On Render you
# set GEMINI_API_KEY in the dashboard; locally you export it in your shell.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash").strip()

# a simple shared token the app can send so random people can't use your server.
# Set EDIO_APP_TOKEN on the server AND put the same value in the app request.
# (Optional but recommended. Leave empty to skip this check while testing.)
APP_TOKEN = os.environ.get("EDIO_APP_TOKEN", "").strip()

try:
    import google.generativeai as genai
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    _GENAI_OK = True
except Exception:
    _GENAI_OK = False

app = Flask(__name__)

# the strict diagnostic instruction Gemini follows
SYSTEM_PROMPT = (
    "You are EDIO AI, a professional TV motherboard UART diagnostic engineer. "
    "You are given a UART/serial boot log from a TV mainboard. Analyze the boot "
    "sequence: SoC/boot ROM, bootloader (U-Boot etc.), DDR init, eMMC/NAND/NOR, "
    "PMIC, I2C, SPI, Ethernet/Wi-Fi, kernel load and init, Android/Linux startup, "
    "watchdog, reboot loops, kernel panic, device-tree, drivers, power.\n"
    "RULES: Base every finding ONLY on evidence in the log. Never invent a "
    "chipset, voltage, component, error or diagnosis not present in the log. "
    "Separate UART evidence from technician verification (e.g. do not say 'eMMC "
    "is damaged'; say 'repeated eMMC timeouts confirm an eMMC comms failure; the "
    "physical cause needs bench verification', then list checks like VCC, VCCQ, "
    "CLK, CMD, DAT, soldering, known-good compare). For every critical finding, "
    "include the exact UART line(s) as evidence.\n"
    "Respond with ONLY valid JSON in exactly this schema (no prose outside JSON):\n"
    '{"overall_status":"PASS|WARNING|FAILED|UNKNOWN",'
    '"platform":{"soc":"","bootloader":"","operating_system":""},'
    '"boot_stage":"","confirmed_findings":[],"critical_errors":[],"warnings":[],'
    '"probable_fault_area":"","probable_root_cause":"","possible_causes":[],'
    '"recommended_checks":[],"recommended_repair_actions":[],'
    '"useful_uart_commands":[],"confidence":0,'
    '"evidence":[{"finding":"","lines":[]}],"unknown_information":[]}'
)


# ── health check (open in a browser to confirm the server is up) ─────────────
@app.get("/")
def home():
    return jsonify(service="EDIO AI", status="ok",
                   gemini=bool(GEMINI_API_KEY and _GENAI_OK),
                   model=GEMINI_MODEL)


# ── the endpoint the app calls ───────────────────────────────────────────────
@app.post("/ai/analyze-uart")
def analyze_uart():
    # 0) basic app-token gate (optional)
    if APP_TOKEN:
        sent = request.headers.get("X-EDIO-Token", "")
        if sent != APP_TOKEN:
            return jsonify(ok=False, error="AUTHENTICATION_ERROR",
                           message="App token missing or invalid."), 401

    # 1) parse request
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify(ok=False, error="SERVER_ERROR",
                       message="Bad request body."), 400

    uart_log = (data.get("uart_log") or "").strip()
    if not uart_log:
        return jsonify(ok=False, error="EMPTY_LOG",
                       message="No UART log provided.")

    # 1a) simple size guard (the app already trims, but be safe)
    if len(uart_log) > 200_000:
        return jsonify(ok=False, error="LOG_TOO_LARGE",
                       message="UART log is too large.")

    # ── (Optional) verify license/usage here ──
    # device_id  = data.get("device_id")
    # license_key = data.get("license_key")
    # check them against your license DB, enforce monthly limits, etc.
    # If not allowed → return jsonify(ok=False, error="LICENSE_ERROR"), 403

    # 2) make sure Gemini is configured
    if not (GEMINI_API_KEY and _GENAI_OK):
        return jsonify(ok=False, error="SERVER_ERROR",
                       message="AI is not configured on the server."), 500

    # 3) call Gemini
    try:
        model = genai.GenerativeModel(GEMINI_MODEL,
                                      system_instruction=SYSTEM_PROMPT)
        resp = model.generate_content(
            "UART LOG:\n\n" + uart_log,
            generation_config={"response_mime_type": "application/json",
                               "temperature": 0.2})
        text = resp.text
    except Exception as e:
        # network / provider / quota problems
        msg = str(e).lower()
        if "quota" in msg or "rate" in msg or "429" in msg:
            return jsonify(ok=False, error="RATE_LIMIT_ERROR"), 429
        return jsonify(ok=False, error="AI_PROVIDER_ERROR",
                       message="AI service error."), 502

    # 4) parse + validate the AI's JSON
    report = _parse_report(text)
    if report is None:
        return jsonify(ok=False, error="INVALID_AI_RESPONSE",
                       message="AI returned an unexpected result.")

    # 5) (optional) record usage, then discard the raw log (privacy)
    return jsonify(ok=True,
                   session_id=data.get("session_id", str(int(time.time()))),
                   report=report)


def _parse_report(text):
    """Turn the model's text into a dict, tolerating ```json fences."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    cleaned = text.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return None


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
