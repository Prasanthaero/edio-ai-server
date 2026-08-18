================================================================================
 EDIO AI SERVER — DEPLOY FREE ON RENDER.COM   (step by step, beginner friendly)
================================================================================

This puts your AI backend online for free, with your Gemini key kept SAFE on the
server. Takes about 15 minutes the first time.

WHAT YOU HAVE (this folder):
    server.py            the backend code (already written for you)
    requirements.txt     the libraries it needs
    render.yaml          optional one-click config
    README_DEPLOY.txt    this file

--------------------------------------------------------------------------------
 STEP 1 — PUT THE CODE ON GITHUB  (Render deploys from GitHub)
--------------------------------------------------------------------------------
  1. Make a free account at https://github.com
  2. Click "New repository" → name it  edio-ai-server  → Create.
  3. Upload these 3 files: server.py, requirements.txt, render.yaml
     (Use the "uploading an existing file" link on the empty repo page — you can
      drag & drop them in the browser, no git commands needed.)

--------------------------------------------------------------------------------
 STEP 2 — CREATE THE SERVER ON RENDER
--------------------------------------------------------------------------------
  1. Make a free account at https://render.com  (sign in with GitHub).
  2. Click  New  →  Web Service.
  3. Connect your  edio-ai-server  GitHub repo.
  4. Render usually auto-fills these. If not, set:
        Runtime        : Python 3
        Build Command  : pip install -r requirements.txt
        Start Command  : gunicorn server:app --bind 0.0.0.0:$PORT --timeout 120
        Instance Type  : Free
  5. Click  "Advanced"  →  Add Environment Variable:
        Key   : GEMINI_API_KEY
        Value : <paste your NEW Gemini key here>     ← this is where the key lives
     (optional, recommended)
        Key   : EDIO_APP_TOKEN
        Value : <make up a long random password, e.g. edio-9f3k2p7q>
     (optional)
        Key   : GEMINI_MODEL
        Value : gemini-1.5-flash
  6. Click  Create Web Service.  Render builds & starts it (2-3 minutes).

--------------------------------------------------------------------------------
 STEP 3 — GET YOUR SERVER URL
--------------------------------------------------------------------------------
  When it finishes, Render shows a URL like:
        https://edio-ai.onrender.com
  Open it in a browser — you should see:
        {"service":"EDIO AI","status":"ok","gemini":true,...}
  If gemini:true → your key is working. 

--------------------------------------------------------------------------------
 STEP 4 — POINT THE APP AT YOUR SERVER
--------------------------------------------------------------------------------
  In the Smart Clone  config.py:
        AI_DIRECT_GEMINI = False                       # turn OFF direct mode
        GEMINI_API_KEY   = ""                          # remove any local key
        AI_API_URL = "https://edio-ai.onrender.com/ai" # ← your Render URL + /ai
        AI_ENABLED = True
  Rebuild the app. Press AI ANALYZE — it now goes through YOUR server.

  If you set EDIO_APP_TOKEN on the server, also tell me and I'll make the app
  send it in the request header (so only your app can use the server).

--------------------------------------------------------------------------------
 NOTES ON THE FREE PLAN
--------------------------------------------------------------------------------
  • The free Render service SLEEPS after ~15 minutes of no use. The first
    request after sleeping takes ~30-50 seconds to wake up (the app shows
    "Contacting EDIO AI…"). After that it's fast until it idles again.
  • Free is perfect for testing and light use. If you get many customers, a
    small paid plan ($7/mo) keeps it always-on.
  • Gemini itself has a free tier too (limited requests/day). Watch your usage
    in Google AI Studio.

--------------------------------------------------------------------------------
 TEST IT LOCALLY FIRST (optional)
--------------------------------------------------------------------------------
  pip install flask google-generativeai
  set GEMINI_API_KEY=your-key         (Windows)   /   export ... (Mac/Linux)
  python server.py
  → open http://localhost:8000  → should show status ok.
  Then temporarily set AI_API_URL = "http://localhost:8000/ai" in the app to try
  it before deploying.
================================================================================
