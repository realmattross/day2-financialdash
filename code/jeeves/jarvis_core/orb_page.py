"""Jeeves orb — 2D HUD served at ``/orb``.

The orb is the desktop visual that shows what Jeeves is doing. Designed
to be opened as a Chrome popup (or just navigated to at
``localhost:8765`` — root redirects here).

Design — faithful to the reference Matt sent:

  Central layer: a bright white-cyan core with a soft cyan glow,
  surrounded by 4-5 concentric cyan rings in different dash patterns,
  each rotating at its own pace. Two-three small "satellite" dots
  orbit the rings. Four reticle tick marks at the cardinal points
  outside the outermost ring. State label below — "JEEVES · IDLE".

  Four corner telemetry widgets:
    Top-left      HEALTH    walking speed + steps (today)
    Top-right     CLOCK     time + date
    Bottom-left   FEED      current/last tool Jeeves used
    Bottom-right  SESSION   state · uptime · last wake

State drives:
  * Ring + satellite + reticle colour (cyan idle/listening, violet
    thinking, warm gold speaking)
  * Ring rotation speed (faster as the orb gets more active)
  * Core glow intensity
  * Label text

State contract is unchanged: poll ``/state.json`` every 150ms.
"""

from __future__ import annotations

ORB_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Jeeves</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    /* ---------- 1. PALETTE ------------------------------------------ */
    :root {
      --c-accent:        #4ad6ff;
      --c-accent-soft:   rgba(74, 214, 255, 0.55);
      --c-accent-faint:  rgba(74, 214, 255, 0.22);
      --c-accent-strong: rgba(74, 214, 255, 0.85);
      --c-core:          #ffffff;
      --c-core-halo:     rgba(74, 214, 255, 0.85);

      --spin-1: 28s;
      --spin-2: 44s;
      --spin-3: 60s;
      --spin-4: 80s;
      --spin-sat-a: 18s;
      --spin-sat-b: 26s;
      --spin-sat-c: 34s;

      --core-pulse: 1.0;
    }
    body.listening {
      --c-accent:       #6fe2ff;
      --c-accent-soft:  rgba(111, 226, 255, 0.65);
      --c-accent-faint: rgba(111, 226, 255, 0.28);
      --c-accent-strong:rgba(111, 226, 255, 0.95);
      --c-core-halo:    rgba(111, 226, 255, 0.95);
      --spin-1: 22s; --spin-2: 34s; --spin-3: 50s; --spin-4: 66s;
      --spin-sat-a: 14s; --spin-sat-b: 20s; --spin-sat-c: 28s;
      --core-pulse: 1.10;
    }
    body.thinking {
      --c-accent:       #a98bff;
      --c-accent-soft:  rgba(169, 139, 255, 0.65);
      --c-accent-faint: rgba(169, 139, 255, 0.28);
      --c-accent-strong:rgba(169, 139, 255, 0.95);
      --c-core-halo:    rgba(169, 139, 255, 0.95);
      --spin-1: 18s; --spin-2: 28s; --spin-3: 42s; --spin-4: 56s;
      --spin-sat-a: 12s; --spin-sat-b: 17s; --spin-sat-c: 24s;
      --core-pulse: 1.20;
    }
    body.speaking {
      --c-accent:       #ffc46d;
      --c-accent-soft:  rgba(255, 196, 109, 0.70);
      --c-accent-faint: rgba(255, 196, 109, 0.32);
      --c-accent-strong:rgba(255, 196, 109, 1.0);
      --c-core-halo:    rgba(255, 196, 109, 1.0);
      --spin-1: 14s; --spin-2: 22s; --spin-3: 34s; --spin-4: 48s;
      --spin-sat-a: 10s; --spin-sat-b: 14s; --spin-sat-c: 20s;
      --core-pulse: 1.40;
    }

    /* ---------- 2. GLOBAL ------------------------------------------- */
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      width: 100%; height: 100%;
      background: #04080d;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', sans-serif;
      color: var(--c-accent-soft);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-direction: column;
    }

    /* ---------- 3. ORB ---------------------------------------------- */
    .orb {
      position: relative;
      width: 560px;
      height: 560px;
    }
    .orb svg { width: 100%; height: 100%; display: block; overflow: visible; }

    /* Rotating groups. transform-origin sits at the SVG centre (280,280
       given our 560 viewBox). */
    .spin-1 { animation: spin var(--spin-1) linear infinite;         transform-origin: 280px 280px; }
    .spin-2 { animation: spin var(--spin-2) linear infinite reverse; transform-origin: 280px 280px; }
    .spin-3 { animation: spin var(--spin-3) linear infinite;         transform-origin: 280px 280px; }
    .spin-4 { animation: spin var(--spin-4) linear infinite reverse; transform-origin: 280px 280px; }
    .sat-a  { animation: spin var(--spin-sat-a) linear infinite;          transform-origin: 280px 280px; }
    .sat-b  { animation: spin var(--spin-sat-b) linear infinite reverse;  transform-origin: 280px 280px; }
    .sat-c  { animation: spin var(--spin-sat-c) linear infinite;          transform-origin: 280px 280px; }
    @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

    .ring, .tick { stroke: var(--c-accent); transition: stroke 500ms ease; }
    .sat-dot { fill: var(--c-accent); transition: fill 500ms ease; }

    .core-bright {
      fill: var(--c-core);
      filter:
        drop-shadow(0 0 calc(10px * var(--core-pulse)) var(--c-core-halo))
        drop-shadow(0 0 calc(22px * var(--core-pulse)) var(--c-core-halo))
        drop-shadow(0 0 calc(42px * var(--core-pulse)) var(--c-accent-soft));
      transition: filter 500ms ease;
    }
    .core-glow   { fill: var(--c-accent); opacity: 0.32; transition: fill 500ms ease; }
    .core-glow-2 { fill: var(--c-accent); opacity: 0.16; transition: fill 500ms ease; }

    .wake-pulse {
      stroke: var(--c-accent);
      fill: none;
      stroke-width: 1.5;
      opacity: 0;
      transform-origin: 280px 280px;
    }
    .wake-pulse.active { animation: wakeRipple 900ms ease-out forwards; }
    @keyframes wakeRipple {
      0%   { opacity: 0.8; transform: scale(0.4); }
      100% { opacity: 0;   transform: scale(1.7); }
    }

    /* ---------- 4. STATE LABEL -------------------------------------- */
    .label {
      position: absolute;
      bottom: -56px;
      left: 50%;
      transform: translateX(-50%);
      font-family: 'SF Mono', ui-monospace, 'JetBrains Mono', Menlo, monospace;
      font-size: 11px;
      letter-spacing: 0.36em;
      text-transform: uppercase;
      color: var(--c-accent-soft);
      text-shadow: 0 0 12px var(--c-accent-faint);
      white-space: nowrap;
      transition: color 500ms ease, text-shadow 500ms ease;
    }
    .label .name  { opacity: 0.7; }
    .label .sep   { opacity: 0.4; margin: 0 10px; }
    .label .state { opacity: 0.95; }

    /* ---------- 5. CORNER WIDGETS ----------------------------------- */
    /* Four small HUD panels in the corners — same anchoring scheme,
       just mirrored on which side the framing rule sits. Each shows
       a small accent LED dot next to a title row, then a compact
       data block underneath. */
    .corner {
      position: fixed;
      width: 220px;
      font-family: 'SF Mono', ui-monospace, 'JetBrains Mono', Menlo, monospace;
      color: var(--c-accent-soft);
      pointer-events: none;
      transition: color 500ms ease;
    }
    .corner.tl { top: 28px; left: 28px;  text-align: left; }
    .corner.tr { top: 28px; right: 28px; text-align: right; }
    .corner.bl { bottom: 28px; left: 28px;  text-align: left; }
    .corner.br { bottom: 28px; right: 28px; text-align: right; }

    .corner .head {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 9px;
      letter-spacing: 0.28em;
      text-transform: uppercase;
      color: var(--c-accent);
      transition: color 500ms ease;
    }
    .corner.tr .head, .corner.br .head { flex-direction: row-reverse; }
    .corner .head .led {
      width: 6px; height: 6px;
      border-radius: 50%;
      background: var(--c-accent);
      box-shadow: 0 0 8px var(--c-accent-strong);
      transition: background 500ms ease, box-shadow 500ms ease;
    }
    .corner .body {
      margin-top: 10px;
      font-size: 11px;
      color: var(--c-accent-soft);
      line-height: 1.55;
      letter-spacing: 0.05em;
    }
    .corner .big {
      font-size: 22px;
      color: #ffffff;
      letter-spacing: 0.03em;
      line-height: 1.1;
      margin-bottom: 4px;
      text-shadow: 0 0 12px var(--c-accent-faint);
    }
    .corner .sub {
      font-size: 10px;
      color: var(--c-accent-faint);
      letter-spacing: 0.18em;
      text-transform: uppercase;
    }
    .corner .row {
      display: flex; justify-content: space-between; gap: 12px;
      font-size: 11px;
    }
    .corner.tr .row, .corner.br .row { flex-direction: row-reverse; }
    .corner .row .k { color: var(--c-accent-faint); }
    .corner .row .v { color: #ffffff; font-variant-numeric: tabular-nums; }

    /* Side-rule — thin vertical line on the inside edge of each panel,
       gives the HUD frame feel without a heavy border. */
    .corner::before {
      content: "";
      position: absolute;
      top: 4px; bottom: 4px;
      width: 1px;
      background: var(--c-accent-faint);
      transition: background 500ms ease;
    }
    .corner.tl::before { left: -10px; }
    .corner.bl::before { left: -10px; }
    .corner.tr::before { right: -10px; }
    .corner.br::before { right: -10px; }

    /* Slight under-glow on the active body class for visual cohesion
       — each corner subtly brightens with the orb's state. */
    body.listening .corner .big,
    body.thinking  .corner .big,
    body.speaking  .corner .big { text-shadow: 0 0 14px var(--c-accent-soft); }
  </style>
</head>
<body class="idle">
  <!-- ============== CORNERS ============== -->
  <div class="corner tl">
    <div class="head"><span class="led"></span><span>Health</span></div>
    <div class="body">
      <div class="big" id="h-speed">—</div>
      <div class="sub" id="h-speed-label">Walking speed (m/s)</div>
      <div class="row" style="margin-top:10px;">
        <span class="k">Steps</span><span class="v" id="h-steps">—</span>
      </div>
    </div>
  </div>

  <div class="corner tr">
    <div class="head"><span class="led"></span><span>Clock</span></div>
    <div class="body">
      <div class="big" id="c-time">—</div>
      <div class="sub" id="c-date">—</div>
    </div>
  </div>

  <div class="corner bl">
    <div class="head"><span class="led"></span><span>Feed</span></div>
    <div class="body">
      <div class="big" id="f-current" style="font-size:13px;">Idle</div>
      <div class="sub" id="f-last">Last: —</div>
      <div class="row" style="margin-top:10px;">
        <span class="k">Tools today</span><span class="v" id="f-count">0</span>
      </div>
    </div>
  </div>

  <div class="corner br">
    <div class="head"><span class="led"></span><span>Session</span></div>
    <div class="body">
      <div class="big" id="s-state" style="font-size:14px;">IDLE</div>
      <div class="row"><span class="k">Uptime</span><span class="v" id="s-uptime">0m</span></div>
      <div class="row"><span class="k">Last wake</span><span class="v" id="s-wake">—</span></div>
    </div>
  </div>

  <!-- ============== CORE ORB ============== -->
  <div class="orb">
    <svg viewBox="0 0 560 560" xmlns="http://www.w3.org/2000/svg">
      <!-- Reticle ticks at N/S/E/W -->
      <g class="reticle">
        <line class="tick" x1="280" y1="16"  x2="280" y2="40"  stroke-width="1.4"/>
        <line class="tick" x1="268" y1="24"  x2="292" y2="24"  stroke-width="1.4"/>
        <line class="tick" x1="280" y1="520" x2="280" y2="544" stroke-width="1.4"/>
        <line class="tick" x1="268" y1="536" x2="292" y2="536" stroke-width="1.4"/>
        <line class="tick" x1="520" y1="280" x2="544" y2="280" stroke-width="1.4"/>
        <line class="tick" x1="536" y1="268" x2="536" y2="292" stroke-width="1.4"/>
        <line class="tick" x1="16"  y1="280" x2="40"  y2="280" stroke-width="1.4"/>
        <line class="tick" x1="24"  y1="268" x2="24"  y2="292" stroke-width="1.4"/>
      </g>

      <!-- Ring 4 — outermost, fine dots. -->
      <g class="spin-4">
        <circle class="ring" cx="280" cy="280" r="252" fill="none"
                stroke-width="1.2" stroke-dasharray="2 6" opacity="0.55"/>
      </g>
      <!-- Ring 3 — big dashes. -->
      <g class="spin-3">
        <circle class="ring" cx="280" cy="280" r="210" fill="none"
                stroke-width="1.6" stroke-dasharray="14 10" opacity="0.78"/>
      </g>
      <!-- Ring 2 — tight dots (middle-inner). -->
      <g class="spin-2">
        <circle class="ring" cx="280" cy="280" r="170" fill="none"
                stroke-width="1.3" stroke-dasharray="1 5" opacity="0.62"/>
      </g>
      <!-- Ring 1 — innermost, biggest dashes. -->
      <g class="spin-1">
        <circle class="ring" cx="280" cy="280" r="125" fill="none"
                stroke-width="2.0" stroke-dasharray="22 16" opacity="0.92"/>
      </g>
      <!-- Extra thin ring at r=90 close to the core — densifies the inner
           area, picks up the bright glow. -->
      <g class="spin-3">
        <circle class="ring" cx="280" cy="280" r="85" fill="none"
                stroke-width="0.8" stroke-dasharray="1 7" opacity="0.5"/>
      </g>

      <!-- Satellites. Each on its own spin group at its own pace; one
           reverses direction so they cross each other on screen. -->
      <g class="sat-a">
        <circle class="sat-dot" cx="280" cy="28" r="6"/>
      </g>
      <g class="sat-b">
        <circle class="sat-dot" cx="490" cy="280" r="5"/>
      </g>
      <g class="sat-c">
        <circle class="sat-dot" cx="280" cy="455" r="4"/>
      </g>

      <!-- Core: three concentric glows + bright centre. The drop-shadow
           on .core-bright does the heavy lifting via CSS filter. -->
      <circle class="core-glow-2" cx="280" cy="280" r="50"/>
      <circle class="core-glow"   cx="280" cy="280" r="30"/>
      <circle class="core-bright" cx="280" cy="280" r="15"/>

      <circle id="wake-pulse" class="wake-pulse"
              cx="280" cy="280" r="70" fill="none"/>
    </svg>

    <div class="label">
      <span class="name">JEEVES</span><span class="sep">·</span><span class="state" id="state-label">IDLE</span>
    </div>
  </div>

  <script>
  // =====================================================================
  // 1. STATE POLL — body class drives the whole palette + rotation
  //                 speed via CSS variables. /state.json contract.
  // =====================================================================
  const body = document.body;
  const stateLabel = document.getElementById('state-label');
  const wakePulse  = document.getElementById('wake-pulse');

  const STATE_LABEL = {
    idle: 'IDLE', listening: 'LISTENING',
    thinking: 'THINKING', speaking: 'REPLYING',
  };
  let currentState = 'idle';
  let lastWakeAt = null;

  function firePulse() {
    wakePulse.classList.remove('active');
    void wakePulse.getBoundingClientRect();
    wakePulse.classList.add('active');
  }

  async function pollState() {
    try {
      const resp = await fetch('/state.json', { cache: 'no-store' });
      if (!resp.ok) return;
      const data = await resp.json();
      const newState = data.state || 'idle';
      if (newState === currentState) return;

      body.classList.remove(currentState);
      body.classList.add(newState);
      const label = STATE_LABEL[newState] || newState.toUpperCase();
      stateLabel.textContent = label;
      document.getElementById('s-state').textContent = label;

      if (currentState === 'idle' && newState !== 'idle') {
        firePulse();
        lastWakeAt = Date.now();
      }
      currentState = newState;
    } catch (e) { /* keep last good state */ }
  }
  setInterval(pollState, 150);
  pollState();

  // =====================================================================
  // 2. CLOCK CORNER — pure JS, 1s tick.
  // =====================================================================
  const timeEl = document.getElementById('c-time');
  const dateEl = document.getElementById('c-date');
  function tickClock() {
    const d = new Date();
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    timeEl.textContent = `${hh}:${mm}:${ss}`;
    // Locale-aware date — "Sat 23 May" style.
    dateEl.textContent = d.toLocaleDateString('en-GB', {
      weekday: 'short', day: 'numeric', month: 'short',
    });
  }
  tickClock();
  setInterval(tickClock, 1000);

  // =====================================================================
  // 3. SESSION CORNER — uptime is JS-tracked from page-load; last-wake
  //                      is when the orb last saw idle → non-idle.
  // =====================================================================
  const sessionStart = Date.now();
  const uptimeEl = document.getElementById('s-uptime');
  const wakeEl   = document.getElementById('s-wake');
  function fmtAge(ms) {
    if (ms == null) return '—';
    const s = Math.floor(ms / 1000);
    if (s < 60)   return s + 's';
    const m = Math.floor(s / 60);
    if (m < 60)   return m + 'm';
    const h = Math.floor(m / 60);
    return h + 'h ' + (m % 60) + 'm';
  }
  function tickSession() {
    uptimeEl.textContent = fmtAge(Date.now() - sessionStart);
    wakeEl.textContent   = lastWakeAt ? (fmtAge(Date.now() - lastWakeAt) + ' ago') : '—';
  }
  tickSession();
  setInterval(tickSession, 1000);

  // =====================================================================
  // 4. FEED CORNER — /telemetry.json. Shows what tool is firing right
  //                   now ('current') and what fired last. Counter is
  //                   the per-session monotonic id from telemetry.py.
  // =====================================================================
  const fCurrentEl = document.getElementById('f-current');
  const fLastEl    = document.getElementById('f-last');
  const fCountEl   = document.getElementById('f-count');
  // Per-tool human label fallback — if the server ever emits a name
  // the orb hasn't seen prettified, we tidy it client-side.
  function pretty(name) {
    if (!name) return '—';
    return name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }
  let firstTelemetryId = null;
  async function pollTelemetry() {
    try {
      const resp = await fetch('/telemetry.json', { cache: 'no-store' });
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.current) {
        fCurrentEl.textContent = pretty(data.current.name);
      } else {
        fCurrentEl.textContent = 'Idle';
      }
      if (data.recent && data.recent.length) {
        const last = data.recent[data.recent.length - 1];
        fLastEl.textContent = 'Last: ' + pretty(last.name);
      } else {
        fLastEl.textContent = 'Last: —';
      }
      if (data.id != null) {
        if (firstTelemetryId == null) firstTelemetryId = data.id;
        fCountEl.textContent = String(data.id - firstTelemetryId);
      }
    } catch (e) { /* network blip */ }
  }
  pollTelemetry();
  setInterval(pollTelemetry, 1000);

  // =====================================================================
  // 5. HEALTH CORNER — /health/data.json returns nested categories.
  //                     We dig out walking_speed_m_s and step_count and
  //                     show their current values. Polled lazily; data
  //                     refreshes once per export cycle (~15-30 min).
  // =====================================================================
  const hSpeedEl = document.getElementById('h-speed');
  const hStepsEl = document.getElementById('h-steps');
  function findMetric(payload, id) {
    if (!payload || !payload.categories) return null;
    for (const cat of payload.categories) {
      for (const m of (cat.metrics || [])) {
        if (m.id === id) return m;
      }
    }
    return null;
  }
  function fmtVal(m) {
    if (m == null || m.value == null) return '—';
    const dp = (m.decimals != null) ? m.decimals : 0;
    return Number(m.value).toFixed(dp);
  }
  async function pollHealth() {
    try {
      const resp = await fetch('/health/data.json', { cache: 'no-store' });
      if (!resp.ok) return;
      const data = await resp.json();
      const speed = findMetric(data, 'walking_speed_m_s');
      const steps = findMetric(data, 'step_count');
      hSpeedEl.textContent = fmtVal(speed);
      // Steps come through as integers; bypass the metric's decimals.
      hStepsEl.textContent = (steps && steps.value != null)
        ? Number(steps.value).toLocaleString('en-GB')
        : '—';
    } catch (e) { /* keep last good values */ }
  }
  pollHealth();
  setInterval(pollHealth, 30000);  // every 30s — data doesn't change fast
  </script>
</body>
</html>
"""
