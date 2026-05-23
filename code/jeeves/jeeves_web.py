"""Jeeves HTTP surface — same brain as the voice agent, exposed over HTTP.

Two consumers:

  /            → browser chat UI (the desktop popup)
  /chat        → JSON endpoint used by the browser AND by external surfaces
                 (Telegram via Cloudflare Tunnel, future mobile, etc.)

Conversations are isolated by chat_id. The browser uses chat_id='browser';
Telegram passes its own chat_id; anything else can pick its own. Each
gets its own conversation history kept in-process.

Auth: if JEEVES_AUTH_TOKEN is set in the environment, every /chat request
must carry a matching X-Auth header. Localhost loopback is exempt so the
browser doesn't need to know the token. External callers (the tunnel)
must include it.

Run:
    cd ~/Code/jeeves && source venv/bin/activate
    export SSL_CERT_FILE=$(python -c "import certifi; print(certifi.where())")
    python jeeves_web.py

Then open http://localhost:8765 (the script will also try to open it for you).
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser

from flask import Flask, jsonify, request

from jarvis_core.agent import chat


PORT = 8765
BROWSER_CHAT_ID = "browser"

# Optional shared secret. If set, every /chat call from a non-loopback IP
# must include matching X-Auth. Localhost requests bypass this so the
# browser surface doesn't need to know the token.
AUTH_TOKEN = os.getenv("JEEVES_AUTH_TOKEN", "").strip()

app = Flask(__name__)

# Per-chat conversation histories. Restart the server to clear them all.
_histories: dict[str, list[dict]] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Boot-time hygiene — wipe any stale orb state from a previous run.
# Without this, an orb loaded after the voice agent crashed mid-reply
# would render last week's transcript because ~/.jeeves-utterance.json
# still has the old content (and ~/.jeeves-state may be stuck at
# "speaking"). Resetting on web-server boot makes the orb safely show
# nothing until the voice agent next writes fresh state.
# ---------------------------------------------------------------------------
def _reset_orb_state_files() -> None:
    try:
        from jarvis_core.utterance import reset as _u_reset
        _u_reset()
    except Exception:
        pass
    try:
        from jarvis_core.state import write_state as _s_write
        _s_write("idle")
    except Exception:
        pass
    try:
        from jarvis_core.telemetry import reset as _t_reset
        _t_reset()
    except Exception:
        pass


_reset_orb_state_files()


# ---------------------------------------------------------------------------
# UI — same browser chat as before, just passes chat_id="browser" now.
# ---------------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Jeeves</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {
      --bg: #15171a;
      --bg-input: #22262b;
      --fg: #e3e3e3;
      --fg-dim: #7a7f88;
      --accent: #00bfa6;
      --user: #7fc8ff;
      --border: #2c3138;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body {
      background: var(--bg);
      color: var(--fg);
      font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", system-ui, sans-serif;
      font-size: 15px;
      line-height: 1.5;
      display: flex;
      flex-direction: column;
    }
    header {
      padding: 14px 22px 10px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: baseline;
      justify-content: space-between;
    }
    header h1 {
      margin: 0;
      font-size: 17px;
      font-weight: 700;
      color: var(--accent);
      letter-spacing: 0.02em;
    }
    header .status {
      font-size: 12px;
      color: var(--fg-dim);
    }
    main {
      flex: 1;
      overflow-y: auto;
      padding: 22px;
      max-width: 760px;
      width: 100%;
      margin: 0 auto;
    }
    .turn { margin-bottom: 18px; }
    .who {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      margin-bottom: 4px;
    }
    .turn.user .who { color: var(--user); }
    .turn.jeeves .who { color: var(--accent); }
    .body { white-space: pre-wrap; word-wrap: break-word; }
    .dim { color: var(--fg-dim); font-style: italic; font-size: 14px; }
    footer {
      border-top: 1px solid var(--border);
      padding: 12px 22px;
      max-width: 760px;
      width: 100%;
      margin: 0 auto;
      display: flex;
      gap: 10px;
    }
    #input {
      flex: 1;
      background: var(--bg-input);
      border: 1px solid var(--border);
      color: var(--fg);
      padding: 10px 12px;
      border-radius: 6px;
      font-family: inherit;
      font-size: 15px;
      outline: none;
    }
    #input:focus { border-color: var(--accent); }
    #send {
      background: var(--accent);
      color: #0a0a0a;
      border: none;
      padding: 10px 18px;
      border-radius: 6px;
      font-weight: 600;
      cursor: pointer;
      font-size: 14px;
    }
    #send:disabled { opacity: 0.5; cursor: default; }
    #send:hover:not(:disabled) { background: #00d4b8; }
    #attach {
      background: var(--bg-input);
      color: var(--fg-dim);
      border: 1px solid var(--border);
      padding: 10px 14px;
      border-radius: 6px;
      font-size: 18px;
      cursor: pointer;
      line-height: 1;
    }
    #attach:hover { color: var(--accent); border-color: var(--accent); }
    #attach.has-image { color: var(--accent); border-color: var(--accent); }
    #file { display: none; }
    .image-preview {
      max-width: 320px; max-height: 180px;
      border-radius: 6px; margin-top: 6px;
      border: 1px solid var(--border);
    }
    .pending-thumb {
      display: inline-block;
      margin-right: 8px;
      vertical-align: middle;
      max-height: 36px; max-width: 80px;
      border-radius: 4px; border: 1px solid var(--border);
    }
  </style>
</head>
<body>
  <header>
    <h1>Jeeves</h1>
    <div class="status" id="status">Ready.</div>
  </header>
  <main id="log">
    <div class="dim">Memory loaded. Tools wired. Paperclip to attach an image. Type below.</div>
  </main>
  <footer>
    <input id="file" type="file" accept="image/*">
    <button id="attach" title="Attach image">📎</button>
    <input id="input" type="text" placeholder="Ask Jeeves anything…" autofocus autocomplete="off">
    <button id="send">Send</button>
  </footer>
  <script>
    const CHAT_ID = 'browser';
    const logEl = document.getElementById('log');
    const inputEl = document.getElementById('input');
    const sendEl = document.getElementById('send');
    const statusEl = document.getElementById('status');
    const fileEl = document.getElementById('file');
    const attachEl = document.getElementById('attach');
    let busy = false;
    let pendingImage = null;  // {data: base64, mime: "image/...", dataUrl: full}

    function addTurn(who, text, opts) {
      opts = opts || {};
      const div = document.createElement('div');
      div.className = 'turn ' + who;
      const who_el = document.createElement('div');
      who_el.className = 'who';
      who_el.textContent = who === 'user' ? 'You' : 'Jeeves';
      const body = document.createElement('div');
      body.className = 'body';
      body.textContent = text;
      div.appendChild(who_el);
      div.appendChild(body);
      if (opts.imageUrl) {
        const img = document.createElement('img');
        img.src = opts.imageUrl;
        img.className = 'image-preview';
        div.appendChild(img);
      }
      logEl.appendChild(div);
      logEl.scrollTop = logEl.scrollHeight;
    }

    function readFileAsBase64(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const dataUrl = reader.result;
          // dataUrl: "data:image/jpeg;base64,XXXX..."
          const comma = dataUrl.indexOf(',');
          resolve({
            data: dataUrl.slice(comma + 1),
            mime: file.type || 'image/jpeg',
            dataUrl: dataUrl,
          });
        };
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
    }

    fileEl.addEventListener('change', async e => {
      const file = e.target.files[0];
      if (!file) return;
      try {
        pendingImage = await readFileAsBase64(file);
        attachEl.classList.add('has-image');
        attachEl.title = file.name + ' attached — click again to swap';
        statusEl.textContent = 'Image attached.';
      } catch (err) {
        addTurn('jeeves', 'Couldn\\'t read that file: ' + err.message);
      }
    });

    attachEl.addEventListener('click', () => fileEl.click());

    async function send() {
      if (busy) return;
      const text = inputEl.value.trim();
      if (!text && !pendingImage) return;

      const userImageUrl = pendingImage ? pendingImage.dataUrl : null;
      addTurn('user', text || '(image)', { imageUrl: userImageUrl });
      inputEl.value = '';
      busy = true;
      sendEl.disabled = true;
      statusEl.textContent = 'Thinking…';

      const body = { message: text, chat_id: CHAT_ID };
      if (pendingImage) {
        body.images = [{ data: pendingImage.data, mime: pendingImage.mime }];
      }

      try {
        const r = await fetch('/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await r.json();
        addTurn('jeeves', data.reply || '(no reply)');
      } catch (e) {
        addTurn('jeeves', 'Error: ' + e.message);
      } finally {
        // Clear the pending image whether success or fail — caller can re-attach
        pendingImage = null;
        fileEl.value = '';
        attachEl.classList.remove('has-image');
        attachEl.title = 'Attach image';
        busy = false;
        sendEl.disabled = false;
        statusEl.textContent = 'Ready.';
        inputEl.focus();
      }
    }

    sendEl.addEventListener('click', send);
    inputEl.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });
    inputEl.focus();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Auth — only enforced when the request is from a non-loopback origin AND
# JEEVES_AUTH_TOKEN is configured. The browser hitting localhost is exempt.
# ---------------------------------------------------------------------------
def _is_loopback() -> bool:
    addr = request.remote_addr or ""
    return addr in ("127.0.0.1", "::1", "localhost")


def _check_auth():
    if not AUTH_TOKEN:
        return None  # no auth configured
    if _is_loopback():
        return None  # localhost is trusted
    provided = request.headers.get("X-Auth", "")
    if provided != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    return None


# ---------------------------------------------------------------------------
# Orb overlay — fullscreen JARVIS-style activity indicator. Polls /state.json
# every 150ms and animates accordingly. Designed to be opened in Chrome's
# app mode (borderless window) via `jeeves orb`.
#
# The HTML/CSS/JS lives in jarvis_core/orb_page.py — moved out of this file
# in May 2026 when the orb grew a radial waveform, particle swarm, telemetry
# feed, and corner HUD widgets and ballooned past 900 lines. jeeves_web.py
# stays a thin Flask shell.
# ---------------------------------------------------------------------------
from jarvis_core.orb_page import ORB_PAGE  # noqa: E402


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    # Matt uses the terminal voice agent, not the browser chat UI.
    # Root used to land on the chat page; now it redirects to the orb
    # so localhost:8765 in any browser just shows the visual. The chat
    # UI is still reachable at /chat if it's ever useful.
    from flask import redirect
    return redirect("/orb", code=302)


@app.route("/chat")
def chat_page():
    return PAGE


@app.route("/health")
def health():
    """Cheap GET so the tunnel and external callers can probe liveness."""
    return jsonify({"ok": True, "chats": len(_histories)})


# ---------------------------------------------------------------------------
# Apple Health dashboard — /health/dashboard renders the editable UI,
# /health/data.json returns a fresh JSON payload, /health/save persists
# manual edits. All three are lazy-imported so jeeves_web.py boots fast
# even when the iCloud directory is empty.
# ---------------------------------------------------------------------------
@app.route("/health/dashboard")
def health_dashboard():
    from jarvis_core import health_dashboard as hd
    html = hd.render_html()
    from flask import Response
    return Response(html, mimetype="text/html")


@app.route("/health/voice")
def health_voice_view():
    """Focused single-screen variant that pops up while Jeeves narrates a
    spoken health summary — walking speed hero chart + 5 stat tiles, no
    scrolling, no editing. See jarvis_core/health_voice_view.py."""
    from jarvis_core import health_voice_view as hv
    from flask import Response
    return Response(hv.render_html(), mimetype="text/html")


@app.route("/health/data.json")
def health_data():
    from jarvis_core import health_dashboard as hd
    return jsonify(hd.build_payload())


@app.route("/health/save", methods=["POST"])
def health_save():
    from jarvis_core import health_dashboard as hd
    payload = request.get_json(silent=True) or {}
    date_str = (payload.get("date") or "").strip()
    metric = (payload.get("metric") or "").strip()
    value = payload.get("value")
    try:
        hd.save_override(date_str, metric, value)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "payload": hd.build_payload()})


@app.route("/state.json")
def state_endpoint():
    """Read the current voice-agent activity state for the orb overlay."""
    from jarvis_core.state import read_state
    return jsonify({"state": read_state()})


@app.route("/telemetry.json")
def telemetry_endpoint():
    """Tool-call activity feed for the orb HUD — "what Jeeves is doing right
    now" + the last few finished tool calls. See jarvis_core/telemetry.py."""
    from jarvis_core.telemetry import read_telemetry
    return jsonify(read_telemetry())


@app.route("/utterance.json")
def utterance_endpoint():
    """Read the live transcript — what Jeeves is currently saying or what
    Matt just said. Polled by the orb 4-5 times a second."""
    from jarvis_core.utterance import read_utterance
    return jsonify(read_utterance())


@app.route("/orb")
def orb_page():
    """JARVIS-style activity overlay. Polls /state.json and animates."""
    return ORB_PAGE


@app.route("/chat", methods=["POST"])
def chat_endpoint():
    auth_err = _check_auth()
    if auth_err is not None:
        return auth_err

    payload = request.get_json(silent=True) or {}
    text = (payload.get("message") or "").strip()
    chat_id = (payload.get("chat_id") or "default").strip()
    images = payload.get("images") or None  # list of {"data": <base64>, "mime": "image/..."}

    # Either text or at least one image is required
    if not text and not images:
        return jsonify({"error": "empty message"}), 400

    # Per-chat history under one global lock. Cheap; keeps things sane if
    # multiple surfaces are talking to the same server simultaneously.
    with _lock:
        history = _histories.get(chat_id, [])
        try:
            reply, new_history = chat(history, text, images=images)
            _histories[chat_id] = new_history
        except Exception as e:
            return jsonify({
                "error": str(e),
                "reply": f"⚠ {type(e).__name__}: {e}",
                "chat_id": chat_id,
            }), 200

    return jsonify({"reply": reply, "chat_id": chat_id})


@app.route("/reset", methods=["POST"])
def reset_endpoint():
    """Clear one chat's history (or all of them).

    POST /reset                  → clears the 'default' chat
    POST /reset {"chat_id": "X"} → clears that chat
    POST /reset {"chat_id": "*"} → clears every chat
    """
    auth_err = _check_auth()
    if auth_err is not None:
        return auth_err

    payload = request.get_json(silent=True) or {}
    chat_id = (payload.get("chat_id") or "default").strip()
    with _lock:
        if chat_id == "*":
            _histories.clear()
        else:
            _histories.pop(chat_id, None)
    return jsonify({"ok": True, "cleared": chat_id})


def _open_browser_when_ready() -> None:
    """Open the page in the default browser shortly after the server starts."""
    import time
    time.sleep(0.6)
    webbrowser.open(f"http://localhost:{PORT}")


def _reminders_loop() -> None:
    """Background scheduler for one-shot reminders. Lives inside the popup
    process because that's already managed by launchd and runs 24/7."""
    import time
    from jarvis_core import reminders as rem
    print(f"[reminders] scheduler started, checking every 30s", file=sys.stderr)
    while True:
        try:
            n = rem.check_and_fire()
            if n:
                print(f"[reminders] fired {n} reminder(s)", file=sys.stderr)
        except Exception as e:
            print(f"[reminders] loop error: {e}", file=sys.stderr)
        time.sleep(30)


if __name__ == "__main__":
    print(f"\nJeeves is listening at http://localhost:{PORT}", file=sys.stderr)
    if AUTH_TOKEN:
        print("  auth: enabled (X-Auth required for non-loopback)", file=sys.stderr)
    else:
        print("  auth: OFF — set JEEVES_AUTH_TOKEN before exposing to the internet", file=sys.stderr)
    print()
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    threading.Thread(target=_reminders_loop, daemon=True).start()
    # use_reloader=False so we don't double-import agent.py and reload memory twice
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
