"""Apple Health dashboard — data layer + HTML renderer.

Reads everything Apple Health knows about Matt (via the existing
jarvis_core.health module which mirrors iPhone exports out of iCloud),
walks the historical JSON files in the same iCloud directory to build
trend lines, lets Matt manually edit any value (saved to a sidecar
JSON), and renders a single self-contained dashboard HTML.

The dashboard is served live by jeeves_web.py at /health/dashboard.
Each page load re-reads fresh data from disk, so the "refresh daily"
requirement is met just by the iPhone exporter pushing a new file —
Jeeves picks it up the next time the page is opened.

Public surface:
    build_payload()  → dict ready for JSON serialisation / template
    render_html()    → full standalone HTML string
    save_override()  → persist a single user edit
    OVERRIDES_PATH   → where edits live (~/.jeeves-health-overrides.json)

Override format on disk:
    {
      "2026-05-10": {
        "steps": 8421,
        "resting_hr": 58
      },
      ...
    }
Edits are keyed by ISO date + metric id. Deleting the entry restores
the original Apple Health value.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvis_core import health as _health


# ---------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------
OVERRIDES_PATH = Path.home() / ".jeeves-health-overrides.json"
DATE_PAT = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


# ---------------------------------------------------------------------
# Metric catalogue — declarative description of every card the
# dashboard renders. Keep this as the single source of truth so adding
# a new Apple Health field is one entry here, no template edits.
# ---------------------------------------------------------------------
def _metric_catalogue() -> list[dict]:
    """Categories + metrics in display order. Each metric describes how
    to extract the value from a HealthSnapshot, format it for the eye,
    and (where relevant) which direction is "good"."""
    return [
        {
            "id": "gait",
            "label": "Gait",
            "blurb": "Movement quality — the Parkinson's-relevant signals.",
            "accent": "#d4a045",
            "metrics": [
                {
                    "id": "walking_speed_m_s",
                    "label": "Walking speed",
                    "unit": "m/s",
                    "decimals": 2,
                    "good": "higher",
                    "icon": "walk",
                },
                {
                    "id": "walking_asymmetry_pct",
                    "label": "Walking asymmetry",
                    "unit": "%",
                    "decimals": 1,
                    "good": "lower",
                    "icon": "asymmetry",
                },
                {
                    "id": "double_support_pct",
                    "label": "Double support",
                    "unit": "%",
                    "decimals": 1,
                    "good": "lower",
                    "icon": "feet",
                },
                {
                    "id": "walking_step_length_m",
                    "label": "Step length",
                    "unit": "m",
                    "decimals": 2,
                    "good": "higher",
                    "icon": "ruler",
                },
            ],
        },
        {
            "id": "activity",
            "label": "Activity",
            "blurb": "What today's body has actually done.",
            "accent": "#4ec88a",
            "metrics": [
                {
                    "id": "steps",
                    "label": "Steps",
                    "unit": "",
                    "decimals": 0,
                    "good": "higher",
                    "target": 8000,
                    "icon": "footprint",
                },
                {
                    "id": "active_kcal",
                    "label": "Active energy",
                    "unit": "kcal",
                    "decimals": 0,
                    "good": "higher",
                    "icon": "flame",
                },
                {
                    "id": "exercise_min",
                    "label": "Exercise",
                    "unit": "min",
                    "decimals": 0,
                    "good": "higher",
                    "target": 30,
                    "icon": "stopwatch",
                },
                {
                    "id": "stand_hours",
                    "label": "Stand hours",
                    "unit": "hr",
                    "decimals": 0,
                    "good": "higher",
                    "target": 12,
                    "icon": "stand",
                },
            ],
        },
        {
            "id": "cardio",
            "label": "Cardio",
            "blurb": "Heart, autonomic nervous system, recovery.",
            "accent": "#e36d6d",
            "metrics": [
                {
                    "id": "resting_hr",
                    "label": "Resting HR",
                    "unit": "bpm",
                    "decimals": 0,
                    "good": "lower",
                    "icon": "heart",
                },
                {
                    "id": "hrv_ms",
                    "label": "HRV",
                    "unit": "ms",
                    "decimals": 0,
                    "good": "higher",
                    "icon": "wave",
                },
            ],
        },
        {
            "id": "sleep",
            "label": "Sleep",
            "blurb": "Last night, by stage. PD often fragments this.",
            "accent": "#7e8eff",
            "metrics": [
                {
                    "id": "sleep_total_min",
                    "label": "Total sleep",
                    "unit": "min",
                    "decimals": 0,
                    "good": "higher",
                    "target": 420,
                    "format": "duration",
                    "icon": "moon",
                },
                {
                    "id": "sleep_deep_min",
                    "label": "Deep sleep",
                    "unit": "min",
                    "decimals": 0,
                    "good": "higher",
                    "format": "duration",
                    "icon": "moon-deep",
                },
                {
                    "id": "sleep_rem_min",
                    "label": "REM sleep",
                    "unit": "min",
                    "decimals": 0,
                    "good": "higher",
                    "format": "duration",
                    "icon": "moon-rem",
                },
                {
                    "id": "sleep_awake_min",
                    "label": "Awake",
                    "unit": "min",
                    "decimals": 0,
                    "good": "lower",
                    "format": "duration",
                    "icon": "eye",
                },
            ],
        },
        {
            "id": "hydration",
            "label": "Hydration",
            "blurb": "Water taken in today.",
            "accent": "#5fc1d4",
            "metrics": [
                {
                    "id": "water_ml",
                    "label": "Water",
                    "unit": "ml",
                    "decimals": 0,
                    "good": "higher",
                    "target": 2000,
                    "icon": "drop",
                },
            ],
        },
    ]


# ---------------------------------------------------------------------
# History — read every JSON in the iCloud dir, return per-day snapshots
# ---------------------------------------------------------------------
def _all_history_files() -> list[Path]:
    """All export files we know about across configured iCloud dirs,
    sorted oldest → newest by the date in the filename (mtime fallback)."""
    files: list[Path] = []
    for d in _health.HEALTH_DIRS:
        if d.exists():
            files.extend(d.glob("*.json"))
    files = [
        p for p in files
        if not p.name.endswith("_new_automation.json")
        and not p.name.startswith("hae_export_")
    ]

    def sort_key(p: Path):
        m = DATE_PAT.search(p.name)
        if m:
            return (1, m.group(0), p.stat().st_mtime)
        return (0, "", p.stat().st_mtime)

    return sorted(files, key=sort_key)


def _date_from_filename_or_mtime(p: Path) -> date:
    m = DATE_PAT.search(p.name)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass
    return datetime.fromtimestamp(p.stat().st_mtime).date()


def _snap_for_file(p: Path) -> _health.HealthSnapshot | None:
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    try:
        return _health._parse(data)
    except Exception:
        return None


def load_history(days: int = 30) -> list[tuple[date, _health.HealthSnapshot]]:
    """Return [(date, snapshot)] for up to N days, newest last.

    De-duplicates by date — if multiple files cover the same day we keep
    the freshest by mtime (most recent export wins)."""
    files = _all_history_files()
    by_date: dict[date, tuple[float, _health.HealthSnapshot]] = {}
    for p in files:
        d = _date_from_filename_or_mtime(p)
        snap = _snap_for_file(p)
        if not snap:
            continue
        mtime = p.stat().st_mtime
        prev = by_date.get(d)
        if prev is None or mtime > prev[0]:
            by_date[d] = (mtime, snap)

    # Trim to last N days ending on the latest known date
    if not by_date:
        return []
    latest = max(by_date.keys())
    cutoff = latest - timedelta(days=days - 1)
    out = sorted(
        ((d, snap) for d, (_, snap) in by_date.items() if d >= cutoff),
        key=lambda t: t[0],
    )
    return out


# ---------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------
def _load_overrides() -> dict[str, dict[str, Any]]:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        return json.loads(OVERRIDES_PATH.read_text()) or {}
    except Exception:
        return {}


def save_override(date_str: str, metric_id: str, value: Any) -> dict:
    """Persist (or clear) a manual edit. Pass value=None to clear.

    Returns the entire overrides map after the change so the caller can
    return it to the client for an instant rerender."""
    if not date_str or not metric_id:
        raise ValueError("date_str and metric_id required")
    data = _load_overrides()
    day = data.setdefault(date_str, {})
    if value is None or value == "":
        day.pop(metric_id, None)
        if not day:
            data.pop(date_str, None)
    else:
        # Coerce numerics where possible — text input on a number field
        # can give us strings. Leave anything else verbatim.
        try:
            num = float(value)
            day[metric_id] = int(num) if num.is_integer() else num
        except (TypeError, ValueError):
            day[metric_id] = value
    OVERRIDES_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))
    return data


def _apply_overrides(
    snap_or_dict: dict, day: date, overrides: dict
) -> tuple[dict, set[str]]:
    """Layer per-day overrides on top of a snapshot dict. Returns the
    merged dict + the set of metric_ids that were edited."""
    edited: set[str] = set()
    day_str = day.isoformat()
    for metric_id, val in (overrides.get(day_str, {}) or {}).items():
        snap_or_dict[metric_id] = val
        edited.add(metric_id)
    return snap_or_dict, edited


# ---------------------------------------------------------------------
# Payload builder — what the HTML consumes
# ---------------------------------------------------------------------
def _snap_to_dict(snap: _health.HealthSnapshot) -> dict:
    """asdict but stripping the giant raw blob and serialising datetimes."""
    d = asdict(snap)
    d.pop("raw", None)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


def _delta(today_val, avg_val) -> float | None:
    if today_val is None or avg_val is None or avg_val == 0:
        return None
    try:
        return ((float(today_val) - float(avg_val)) / float(avg_val)) * 100.0
    except (TypeError, ValueError):
        return None


def build_payload() -> dict:
    """Assemble the full structured payload for the dashboard.

    Shape:
        {
          "today": "2026-05-10",
          "exported_at": "2026-05-10T07:21:00",
          "stale_hours": 0.5,
          "has_data": true,
          "concerns": [...],
          "categories": [
            {
              "id": "gait", "label": "Gait", "accent": "#d4a045",
              "metrics": [
                { id, label, unit, decimals, good, value, edited,
                  avg_7d, delta_7d_pct, target, history: [[date, val], ...] },
                ...
              ]
            },
            ...
          ]
        }
    """
    history = load_history(days=30)
    overrides = _load_overrides()

    if not history:
        # No data at all — return a minimal payload so the UI can
        # render an empty-state nicely.
        return {
            "today": date.today().isoformat(),
            "exported_at": None,
            "stale_hours": None,
            "has_data": False,
            "concerns": [
                "No Apple Health exports found yet. Open Health Auto "
                "Export on the iPhone and run a manual export, or wait "
                "for the next scheduled push to iCloud Drive."
            ],
            "categories": _metric_catalogue(),
            "overrides": overrides,
        }

    today_date, today_snap = history[-1]
    today_dict = _snap_to_dict(today_snap)

    # Sleep backfill — Health Auto Export writes a fresh file each day
    # for live metrics (steps / HR / SpO2), but the sleep_analysis row
    # for last night often doesn't land until later in the day. If
    # today's snap has no sleep, lift the most recent prior day's sleep
    # fields in so the Sleep card shows "what you slept last night"
    # rather than blank. We only copy IF today is empty — don't
    # clobber today with stale data when today actually has values.
    SLEEP_KEYS = (
        "sleep_total_min", "sleep_deep_min",
        "sleep_rem_min",   "sleep_awake_min",
        "bedtime",         "wake_time",
    )
    if today_dict.get("sleep_total_min") is None:
        for prev_date, prev_snap in reversed(history[:-1]):
            prev_dict = _snap_to_dict(prev_snap)
            if prev_dict.get("sleep_total_min") is not None:
                for k in SLEEP_KEYS:
                    today_dict[k] = prev_dict.get(k)
                break

    today_dict, edited_today = _apply_overrides(today_dict, today_date, overrides)

    # Per-day map of metric → value, with overrides layered in
    daily: dict[date, dict] = {}
    for d, snap in history:
        sd = _snap_to_dict(snap)
        sd, _ = _apply_overrides(sd, d, overrides)
        daily[d] = sd

    # Build category/metric output
    catalogue = _metric_catalogue()
    out_categories: list[dict] = []
    for cat in catalogue:
        cat_out = {
            "id": cat["id"],
            "label": cat["label"],
            "blurb": cat.get("blurb", ""),
            "accent": cat["accent"],
            "metrics": [],
        }
        for m in cat["metrics"]:
            mid = m["id"]
            value = today_dict.get(mid)
            # 7-day window ending today
            window = [
                (d, daily[d].get(mid))
                for d in sorted(daily.keys())
                if (today_date - d).days < 7 and daily[d].get(mid) is not None
            ]
            avg_7d = (
                sum(v for _, v in window) / len(window) if window else None
            )
            # Up to 30-day history for sparkline (sparse-tolerant: only
            # plot dates with values, frontend handles gaps)
            history_pts = [
                [d.isoformat(), daily[d].get(mid)]
                for d in sorted(daily.keys())
                if daily[d].get(mid) is not None
            ]

            cat_out["metrics"].append({
                "id": mid,
                "label": m["label"],
                "unit": m.get("unit", ""),
                "decimals": m.get("decimals", 0),
                "good": m.get("good"),
                "target": m.get("target"),
                "format": m.get("format"),
                "icon": m.get("icon"),
                "value": value,
                "edited": mid in edited_today,
                "avg_7d": avg_7d,
                "delta_7d_pct": _delta(value, avg_7d),
                "history": history_pts,
            })
        out_categories.append(cat_out)

    exported_at = today_snap.exported_at
    stale_hours = (
        (datetime.now() - exported_at).total_seconds() / 3600
        if exported_at else None
    )

    return {
        "today": today_date.isoformat(),
        "exported_at": exported_at.isoformat() if exported_at else None,
        "stale_hours": round(stale_hours, 1) if stale_hours is not None else None,
        "has_data": True,
        "concerns": _health.concerns(today_snap),
        "categories": out_categories,
        "overrides": overrides,
    }


# ---------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------
def render_html(payload: dict | None = None) -> str:
    """Return a complete self-contained HTML document for the dashboard.

    If payload is None, builds a fresh one. Pass an explicit payload
    when you want to render to a static file (the live route always
    passes None so each page hit gets fresh data)."""
    if payload is None:
        payload = build_payload()
    data_json = json.dumps(payload, default=str)
    return _TEMPLATE.replace("__DATA_JSON__", data_json)


# ---------------------------------------------------------------------
# Static export — useful for previewing without the server running
# ---------------------------------------------------------------------
def export_static(path: Path | str) -> Path:
    """Write a one-shot snapshot of the dashboard to disk. Edits made
    in the static HTML won't persist (no /health/save backend), but the
    visuals are identical — useful as a preview / share / backup."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html())
    return out


# ---------------------------------------------------------------------
# HTML / CSS / JS template — self-contained, single file. Chart.js
# pulled from CDN. Uses __DATA_JSON__ token replaced at render time
# so we don't have to escape every brace in the file.
# ---------------------------------------------------------------------
_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Health · Jeeves</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,400;9..144,500;9..144,600&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@300;400;500&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {
      /* Light theme. Page is plain white; cards stand out via subtle
         tonal contrast + soft shadow rather than darker fills. */
      --bg-0: #ffffff;
      --bg-1: #f7f8fa;
      --bg-2: #ffffff;
      --bg-3: #eceff3;
      --line: rgba(0,0,0,0.07);
      --line-strong: rgba(0,0,0,0.16);
      --fg: #1a1d23;
      --fg-dim: #5a6068;
      --fg-faint: #9aa0aa;
      /* Slightly more saturated good/bad than the dark theme — paler
         pastels disappear against white. */
      --good: #2d9968;
      --bad: #c8444a;
      --neutral: #5a6068;
      --shadow-soft: 0 1px 2px rgba(15,20,30,0.04), 0 8px 32px rgba(15,20,30,0.07);
      --serif: "Fraunces", ui-serif, Georgia, serif;
      --sans: "Inter", -apple-system, BlinkMacSystemFont, "Helvetica Neue", system-ui, sans-serif;
      --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
      /* Bumped opacity ~3x — the dark-theme tints were tuned against
         a near-black backdrop and washed out completely on white. */
      background:
        radial-gradient(1200px 600px at 80% -10%, rgba(212,160,69,0.16), transparent 60%),
        radial-gradient(900px 700px at -10% 30%, rgba(126,142,255,0.14), transparent 60%),
        radial-gradient(1000px 800px at 50% 110%, rgba(78,200,138,0.12), transparent 60%),
        var(--bg-0);
      color: var(--fg);
      font-family: var(--sans);
      font-size: 15px;
      line-height: 1.55;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }
    .wrap {
      max-width: 1320px;
      margin: 0 auto;
      padding: 48px 36px 96px;
    }
    /* ---------- Header ---------- */
    header.top {
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 36px;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--line);
    }
    header.top .titles h1 {
      font-family: var(--serif);
      font-weight: 400;
      font-style: italic;
      font-size: 44px;
      letter-spacing: -0.015em;
      margin: 0 0 6px;
      line-height: 1;
      color: var(--fg);
    }
    header.top .titles .sub {
      color: var(--fg-dim);
      font-size: 14px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      font-weight: 500;
    }
    header.top .meta {
      text-align: right;
      font-size: 12px;
      color: var(--fg-dim);
      display: flex;
      flex-direction: column;
      gap: 6px;
      align-items: flex-end;
    }
    header.top .meta .stamp {
      font-family: var(--mono);
      color: var(--fg);
      font-size: 13px;
    }
    header.top .meta .stale {
      color: var(--bad);
    }
    .actions {
      display: flex;
      gap: 8px;
    }
    button.btn {
      background: var(--bg-2);
      color: var(--fg);
      border: 1px solid var(--line-strong);
      padding: 8px 14px;
      border-radius: 999px;
      font-family: var(--sans);
      font-size: 12px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      font-weight: 500;
      cursor: pointer;
      transition: all 200ms ease;
    }
    button.btn:hover { background: var(--bg-3); border-color: var(--fg-dim); }
    button.btn.primary {
      background: linear-gradient(180deg, #1a1d23, #0a0d12);
      color: #fff;
      border-color: transparent;
    }
    button.btn.primary:hover { background: #000; }

    /* ---------- Concerns banner ---------- */
    .concerns {
      /* Tinted to the light-theme --bad (#c8444a). Lower-alpha than the
         dark theme since white amplifies overlay intensity. */
      background: linear-gradient(180deg, rgba(200,68,74,0.09), rgba(200,68,74,0.02));
      border: 1px solid rgba(200,68,74,0.28);
      border-radius: 12px;
      padding: 16px 20px;
      margin-bottom: 32px;
      display: flex;
      gap: 14px;
      align-items: flex-start;
    }
    .concerns .icon {
      font-family: var(--serif);
      font-style: italic;
      color: var(--bad);
      font-size: 22px;
      line-height: 1;
      margin-top: 2px;
    }
    .concerns ul { margin: 0; padding: 0; list-style: none; }
    .concerns li {
      padding: 4px 0;
      color: var(--fg);
      font-size: 14px;
    }
    .concerns li + li { border-top: 1px dashed rgba(200,68,74,0.22); }

    /* ---------- Categories ---------- */
    section.cat {
      margin-bottom: 56px;
    }
    section.cat header.cat-head {
      display: flex;
      align-items: baseline;
      gap: 18px;
      margin-bottom: 18px;
    }
    section.cat header.cat-head h2 {
      font-family: var(--serif);
      font-weight: 400;
      font-size: 26px;
      letter-spacing: -0.01em;
      margin: 0;
      color: var(--fg);
    }
    section.cat header.cat-head .pip {
      width: 8px; height: 8px; border-radius: 50%;
      box-shadow: 0 0 16px currentColor;
    }
    section.cat header.cat-head .blurb {
      color: var(--fg-dim);
      font-size: 13px;
      flex: 1;
    }
    section.cat header.cat-head .count {
      color: var(--fg-faint);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 16px;
    }
    .card {
      position: relative;
      background:
        linear-gradient(180deg, var(--bg-2), var(--bg-1));
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 20px 22px 18px;
      box-shadow: var(--shadow-soft);
      transition: border-color 200ms ease, transform 200ms ease;
      overflow: hidden;
    }
    .card::before {
      content: "";
      position: absolute;
      inset: 0;
      border-radius: 16px;
      pointer-events: none;
      /* Subtle inner-top highlight. On light theme this is a faint
         dark tint at the top, mirroring the dark theme's white tint. */
      background: linear-gradient(180deg, rgba(15,20,30,0.025), transparent 28%);
    }
    .card:hover {
      border-color: var(--line-strong);
      transform: translateY(-1px);
    }
    .card .accent-bar {
      position: absolute;
      top: 0; left: 0; bottom: 0;
      width: 3px;
      background: currentColor;
      opacity: 0.7;
    }
    .card .label {
      font-size: 11px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      font-weight: 600;
      color: var(--fg-dim);
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 8px;
    }
    .card .label .edit-btn {
      margin-left: auto;
      background: transparent;
      border: 1px solid transparent;
      color: var(--fg-faint);
      padding: 2px 8px;
      border-radius: 999px;
      font-family: var(--mono);
      font-size: 10px;
      letter-spacing: 0.08em;
      cursor: pointer;
      opacity: 0;
      transition: all 200ms ease;
    }
    .card:hover .edit-btn { opacity: 1; }
    .card .edit-btn:hover {
      color: var(--fg);
      border-color: var(--line-strong);
      background: var(--bg-3);
    }
    .card .edit-btn.editing { opacity: 1; color: currentColor; border-color: currentColor; }
    .card .edited-pip {
      width: 6px; height: 6px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 8px currentColor;
      display: none;
    }
    .card.edited .edited-pip { display: inline-block; }
    .card .value-row {
      display: flex;
      align-items: baseline;
      gap: 8px;
      margin-bottom: 4px;
    }
    .card .value {
      font-family: var(--mono);
      font-size: 36px;
      font-weight: 400;
      letter-spacing: -0.02em;
      color: var(--fg);
      line-height: 1.1;
    }
    .card .value.dim { color: var(--fg-faint); font-style: italic; }
    .card .unit {
      font-family: var(--mono);
      font-size: 13px;
      color: var(--fg-dim);
      letter-spacing: 0.02em;
    }
    .card .target {
      font-family: var(--mono);
      font-size: 11px;
      color: var(--fg-faint);
      margin-left: auto;
      letter-spacing: 0.05em;
    }
    .card .delta {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-family: var(--mono);
      font-size: 12px;
      padding: 3px 8px;
      border-radius: 999px;
      background: rgba(15,20,30,0.05);
      color: var(--neutral);
      letter-spacing: 0.02em;
    }
    .card .delta.up   { color: var(--good); background: rgba(45,153,104,0.12); }
    .card .delta.down { color: var(--bad);  background: rgba(200,68,74,0.10); }
    .card .delta.flat { color: var(--fg-dim); }
    .card .avg7 {
      font-family: var(--mono);
      font-size: 11px;
      color: var(--fg-faint);
      margin-left: 6px;
    }
    .card .delta-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 4px;
      margin-bottom: 14px;
    }
    .card .spark {
      height: 56px;
      width: 100%;
      position: relative;
    }
    .card .spark canvas { display: block; }
    .card .progress {
      height: 3px;
      background: rgba(15,20,30,0.08);
      border-radius: 2px;
      overflow: hidden;
      margin-top: 12px;
      display: none;
    }
    .card.has-target .progress { display: block; }
    .card .progress .fill {
      height: 100%;
      background: currentColor;
      border-radius: 2px;
      box-shadow: 0 0 8px currentColor;
      transition: width 600ms cubic-bezier(0.2, 0.8, 0.25, 1);
    }
    /* Editing state */
    .card .edit-form {
      display: none;
      margin: 8px 0 12px;
      gap: 6px;
      align-items: center;
    }
    .card.editing .edit-form { display: flex; }
    .card.editing .value-row,
    .card.editing .delta-row { display: none; }
    .card .edit-form input {
      flex: 1;
      background: var(--bg-3);
      border: 1px solid var(--line-strong);
      color: var(--fg);
      padding: 8px 12px;
      border-radius: 8px;
      font-family: var(--mono);
      font-size: 18px;
      outline: none;
    }
    .card .edit-form input:focus { border-color: currentColor; }
    .card .edit-form button {
      background: currentColor;
      /* currentColor inherits from the card's accent (good/bad/neutral),
         all of which are mid-saturation on the light theme — white text
         on top reads cleanly without needing a hardcoded fill. */
      color: #fff;
      border: none;
      padding: 8px 12px;
      border-radius: 8px;
      font-weight: 600;
      font-size: 12px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      cursor: pointer;
    }
    .card .edit-form .clear {
      background: transparent;
      color: var(--fg-dim);
      padding: 6px 8px;
    }

    /* ---------- Toast ---------- */
    .toast {
      position: fixed;
      bottom: 24px; left: 50%;
      transform: translateX(-50%) translateY(20px);
      background: var(--bg-3);
      color: var(--fg);
      padding: 10px 18px;
      border-radius: 999px;
      font-size: 13px;
      border: 1px solid var(--line-strong);
      box-shadow: var(--shadow-soft);
      opacity: 0;
      transition: all 300ms ease;
      pointer-events: none;
      z-index: 100;
    }
    .toast.show {
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }
    .toast.bad { border-color: var(--bad); color: var(--bad); }

    /* ---------- Empty state ---------- */
    .empty {
      text-align: center;
      padding: 96px 24px;
      color: var(--fg-dim);
    }
    .empty h2 {
      font-family: var(--serif);
      font-style: italic;
      font-size: 32px;
      color: var(--fg);
      margin: 0 0 12px;
    }

    @media (max-width: 760px) {
      .wrap { padding: 28px 18px 64px; }
      header.top { flex-direction: column; align-items: flex-start; }
      header.top .meta { text-align: left; align-items: flex-start; }
      header.top .titles h1 { font-size: 34px; }
      .cards { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header class="top">
      <div class="titles">
        <h1>Health</h1>
        <div class="sub" id="today-label">—</div>
      </div>
      <div class="meta">
        <div class="stamp" id="stamp">—</div>
        <div class="actions">
          <button class="btn" id="refresh">Refresh</button>
          <button class="btn primary" id="reload">Reload data</button>
        </div>
      </div>
    </header>

    <div id="concerns-host"></div>
    <div id="cats"></div>
  </div>

  <div class="toast" id="toast"></div>

  <script id="bootstrap" type="application/json">__DATA_JSON__</script>
  <script>
    // ============================================================
    // Apple Health dashboard — client logic
    //   - Renders the payload Python handed us into a card grid
    //   - Inline edit on each card → POST /health/save
    //   - Reload pulls fresh JSON from /health/data.json
    // ============================================================
    const PAYLOAD = JSON.parse(document.getElementById('bootstrap').textContent);
    let state = PAYLOAD;

    const fmtDate = (iso) => {
      try {
        const d = new Date(iso + 'T00:00:00');
        return d.toLocaleDateString('en-GB', {
          weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'
        });
      } catch (e) { return iso; }
    };

    const fmtTimestamp = (iso) => {
      if (!iso) return 'no data';
      try {
        const d = new Date(iso);
        return d.toLocaleString('en-GB', {
          day: 'numeric', month: 'short',
          hour: '2-digit', minute: '2-digit'
        });
      } catch (e) { return iso; }
    };

    const fmtNumber = (val, decimals, format) => {
      if (val === null || val === undefined) return '—';
      const n = Number(val);
      if (!isFinite(n)) return '—';
      if (format === 'duration') {
        const hours = Math.floor(n / 60);
        const mins = Math.round(n % 60);
        if (hours === 0) return mins + 'm';
        return hours + 'h ' + (mins ? mins + 'm' : '');
      }
      if (decimals === 0) {
        return n.toLocaleString('en-GB', { maximumFractionDigits: 0 });
      }
      return n.toLocaleString('en-GB', {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      });
    };

    function deltaPill(metric) {
      const d = metric.delta_7d_pct;
      if (d === null || d === undefined || metric.value === null || metric.avg_7d === null) {
        return '<span class="delta flat">—</span>';
      }
      const abs = Math.abs(d);
      let cls = 'flat';
      if (abs < 1.5) cls = 'flat';
      else if (metric.good === 'higher') cls = d > 0 ? 'up' : 'down';
      else if (metric.good === 'lower') cls = d < 0 ? 'up' : 'down';
      else cls = 'flat';
      const arrow = d > 0 ? '▲' : d < 0 ? '▼' : '·';
      return '<span class="delta ' + cls + '">' + arrow + ' ' + abs.toFixed(1) + '%</span>';
    }

    function avgLabel(metric) {
      if (metric.avg_7d === null || metric.avg_7d === undefined) return '';
      return '<span class="avg7">vs ' + fmtNumber(metric.avg_7d, metric.decimals, metric.format) + ' avg</span>';
    }

    function targetLabel(metric) {
      if (!metric.target) return '';
      return '<span class="target">/ ' + fmtNumber(metric.target, metric.decimals, metric.format) + '</span>';
    }

    function renderConcerns() {
      const host = document.getElementById('concerns-host');
      host.innerHTML = '';
      if (!state.concerns || !state.concerns.length) return;
      const div = document.createElement('div');
      div.className = 'concerns';
      div.innerHTML =
        '<div class="icon">!</div>' +
        '<ul>' + state.concerns.map(c =>
          '<li>' + escapeHtml(c) + '</li>'
        ).join('') + '</ul>';
      host.appendChild(div);
    }

    function escapeHtml(s) {
      return (s + '').replace(/[&<>\"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      })[ch]);
    }

    function renderCard(metric, accent) {
      const card = document.createElement('div');
      card.className = 'card' + (metric.edited ? ' edited' : '') +
                       (metric.target ? ' has-target' : '');
      card.style.color = accent;
      const formatted = fmtNumber(metric.value, metric.decimals, metric.format);
      const valueClass = metric.value === null || metric.value === undefined ? 'value dim' : 'value';
      let pct = 0;
      if (metric.target && metric.value !== null && metric.value !== undefined) {
        pct = Math.min(100, (Number(metric.value) / metric.target) * 100);
      }

      card.innerHTML =
        '<div class="accent-bar"></div>' +
        '<div class="label">' +
          '<span>' + escapeHtml(metric.label) + '</span>' +
          '<span class="edited-pip" title="Manually edited"></span>' +
          '<button class="edit-btn">Edit</button>' +
        '</div>' +
        '<div class="value-row">' +
          '<span class="' + valueClass + '">' + formatted + '</span>' +
          (metric.unit ? '<span class="unit">' + escapeHtml(metric.unit) + '</span>' : '') +
          targetLabel(metric) +
        '</div>' +
        '<div class="delta-row">' +
          deltaPill(metric) +
          avgLabel(metric) +
        '</div>' +
        '<form class="edit-form">' +
          '<input type="number" step="any" value="' + (metric.value !== null && metric.value !== undefined ? metric.value : '') + '">' +
          '<button type="submit">Save</button>' +
          '<button type="button" class="clear" title="Clear override">×</button>' +
        '</form>' +
        '<div class="spark"><canvas></canvas></div>' +
        '<div class="progress"><div class="fill" style="width: ' + pct + '%"></div></div>';

      const editBtn = card.querySelector('.edit-btn');
      const form = card.querySelector('.edit-form');
      const input = form.querySelector('input');
      const clearBtn = form.querySelector('.clear');

      editBtn.addEventListener('click', () => {
        const editing = card.classList.toggle('editing');
        editBtn.classList.toggle('editing', editing);
        editBtn.textContent = editing ? 'Cancel' : 'Edit';
        if (editing) setTimeout(() => input.focus(), 50);
      });
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        await saveOverride(metric.id, input.value);
      });
      clearBtn.addEventListener('click', async () => {
        await saveOverride(metric.id, '');
      });

      // Sparkline
      const canvas = card.querySelector('canvas');
      drawSpark(canvas, metric, accent);
      return card;
    }

    function drawSpark(canvas, metric, accent) {
      const points = (metric.history || []).map(([d, v]) => ({
        x: d, y: Number(v)
      }));
      if (points.length < 2) {
        const ctx = canvas.getContext('2d');
        canvas.height = 56;
        ctx.fillStyle = 'rgba(15,20,30,0.45)';
        ctx.font = '11px JetBrains Mono';
        ctx.textAlign = 'center';
        ctx.fillText('— not enough history —', canvas.width / 2, 30);
        return;
      }
      // eslint-disable-next-line no-undef
      new Chart(canvas, {
        type: 'line',
        data: {
          labels: points.map(p => p.x),
          datasets: [{
            data: points.map(p => p.y),
            borderColor: accent,
            backgroundColor: hexToRgba(accent, 0.16),
            fill: true,
            borderWidth: 1.6,
            tension: 0.34,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: accent,
            pointHoverBorderColor: '#ffffff',
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 600 },
          interaction: { intersect: false, mode: 'index' },
          plugins: {
            legend: { display: false },
            tooltip: {
              /* Inverted for light theme: dark card with white title and
                 accent-coloured value, so the hover stays readable on
                 the now-white page. */
              backgroundColor: 'rgba(26,29,35,0.95)',
              titleColor: '#ffffff',
              bodyColor: accent,
              borderColor: 'rgba(0,0,0,0.08)',
              borderWidth: 1,
              padding: 8,
              displayColors: false,
              titleFont: { family: 'JetBrains Mono', size: 11 },
              bodyFont: { family: 'JetBrains Mono', size: 12 },
              callbacks: {
                title: (ctxs) => {
                  try {
                    const d = new Date(ctxs[0].label + 'T00:00:00');
                    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
                  } catch (e) { return ctxs[0].label; }
                },
                label: (c) => {
                  return fmtNumber(c.parsed.y, metric.decimals, metric.format) + (metric.unit ? ' ' + metric.unit : '');
                },
              },
            },
          },
          scales: {
            x: { display: false },
            y: { display: false, beginAtZero: false },
          },
        },
      });
    }

    function hexToRgba(hex, alpha) {
      const m = hex.replace('#', '').match(/.{2}/g);
      // Fallback fill colour when an accent hex can't be parsed. On the
      // light theme we want a faint dark wash rather than a faint white
      // one (which would be invisible against the page).
      if (!m) return 'rgba(15,20,30,' + alpha + ')';
      const [r, g, b] = m.map(h => parseInt(h, 16));
      return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
    }

    function renderCategories() {
      const host = document.getElementById('cats');
      host.innerHTML = '';
      if (!state.has_data) {
        host.innerHTML =
          '<div class="empty"><h2>No exports yet</h2>' +
          '<p>Run a manual export in Health Auto Export on the iPhone, ' +
          'or wait for the next scheduled push to iCloud.</p></div>';
        return;
      }
      state.categories.forEach(cat => {
        const sec = document.createElement('section');
        sec.className = 'cat';
        const populated = cat.metrics.filter(m => m.value !== null && m.value !== undefined).length;
        sec.innerHTML =
          '<header class="cat-head">' +
            '<span class="pip" style="background:' + cat.accent + ';color:' + cat.accent + '"></span>' +
            '<h2>' + escapeHtml(cat.label) + '</h2>' +
            '<span class="blurb">' + escapeHtml(cat.blurb || '') + '</span>' +
            '<span class="count">' + populated + ' / ' + cat.metrics.length + '</span>' +
          '</header>' +
          '<div class="cards"></div>';
        const cards = sec.querySelector('.cards');
        cat.metrics.forEach(m => cards.appendChild(renderCard(m, cat.accent)));
        host.appendChild(sec);
      });
    }

    function renderHeader() {
      document.getElementById('today-label').textContent =
        state.has_data ? fmtDate(state.today) : 'awaiting data';
      const stamp = document.getElementById('stamp');
      if (!state.exported_at) {
        stamp.textContent = 'no exports found';
        stamp.classList.add('stale');
      } else {
        const stale = (state.stale_hours !== null && state.stale_hours > 24);
        stamp.textContent =
          (stale ? 'STALE · ' : 'Last sync · ') + fmtTimestamp(state.exported_at);
        stamp.classList.toggle('stale', stale);
      }
    }

    let toastTimer = null;
    function toast(msg, bad) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.toggle('bad', !!bad);
      t.classList.add('show');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
    }

    async function saveOverride(metricId, value) {
      try {
        const resp = await fetch('/health/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            date: state.today,
            metric: metricId,
            value: value === '' ? null : value,
          }),
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const j = await resp.json();
        if (j.payload) {
          state = j.payload;
          renderHeader();
          renderConcerns();
          renderCategories();
        }
        toast(value === '' ? 'Override cleared' : 'Saved');
      } catch (e) {
        toast('Save failed: ' + e.message, true);
      }
    }

    async function reloadData() {
      try {
        const resp = await fetch('/health/data.json', { cache: 'no-store' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        state = await resp.json();
        renderHeader();
        renderConcerns();
        renderCategories();
        toast('Refreshed');
      } catch (e) {
        toast('Refresh failed: ' + e.message, true);
      }
    }

    document.getElementById('refresh').addEventListener('click', reloadData);
    document.getElementById('reload').addEventListener('click', reloadData);

    // First render
    renderHeader();
    renderConcerns();
    renderCategories();

    // Auto-refresh every 30 minutes — Health Auto Export typically pushes
    // hourly, so this catches new data without nagging.
    setInterval(reloadData, 30 * 60 * 1000);
  </script>
</body>
</html>
"""
