"""RIG OmniScout — Build Card Intelligence Dashboard.

Generates a single-page HTML dashboard from V30 build-card data, pattern-engine
outputs, meta-cards, and nightly run status.  Deterministic, no LLM calls,
self-contained output (inline CSS, no external JS dependencies).
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import statistics
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import canonical pipeline primitives.  Relative import keeps the module
# package-local; the fallback path is only for direct-script convenience.
try:
    from .omniscout_build_cards import (
        DOCTRINE_DOMAINS,
        L2_CARDS,
        L2_ROOT,
        atomic_json,
        atomic_text,
        score_build_card,
        sha256_text,
        stable_json,
        utc_now,
    )
except ImportError:  # pragma: no cover - direct execution fallback
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from rig_foundry.omniscout_build_cards import (
        DOCTRINE_DOMAINS,
        L2_CARDS,
        L2_ROOT,
        atomic_json,
        atomic_text,
        score_build_card,
        sha256_text,
        stable_json,
        utc_now,
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DASHBOARD_PATH = Path.home() / "Desktop" / "rig-build-dashboard.html"
# The dashboard targets the local control-plane card store (130 V30 cards).
DEFAULT_DASHBOARD_ROOT = Path.home() / ".rig" / "omniscout-control" / "build-cards"

EMPTY_STRATEGIES = [
    "automation-runtime",
    "doctrine-control-plane",
    "knowledge-memory",
    "scraping-intelligence",
    "legal-compliance",
    "vertical-dental-ortho",
    "vertical-pe-cfo",
]

HTML_THEME = """
:root {
  --bg: #0b0c10;
  --bg-2: #15161a;
  --bg-3: #1f2128;
  --fg: #e0e0e0;
  --fg-dim: #9ca3af;
  --accent: #22d3ee;
  --accent-2: #818cf8;
  --green: #22c55e;
  --yellow: #eab308;
  --red: #ef4444;
  --gray: #6b7280;
  --border: #2a2d35;
  --shadow: rgba(0,0,0,0.35);
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0;
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.45;
}
.container { max-width: 1400px; margin: 0 auto; padding: 24px; }
header {
  background: linear-gradient(135deg, var(--bg-2), var(--bg-3));
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 28px 32px;
  margin-bottom: 24px;
  box-shadow: 0 8px 24px var(--shadow);
}
header h1 { margin: 0 0 8px; font-size: 1.75rem; letter-spacing: -0.02em; }
header .subtitle { color: var(--fg-dim); font-size: 0.95rem; }
.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 14px;
  margin-top: 22px;
}
.kpi {
  background: var(--bg-3);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  text-align: center;
}
.kpi .value { font-size: 1.6rem; font-weight: 700; color: var(--accent); }
.kpi .label { font-size: 0.75rem; color: var(--fg-dim); text-transform: uppercase; letter-spacing: 0.06em; margin-top: 4px; }
section {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 22px 26px;
  margin-bottom: 22px;
  box-shadow: 0 4px 16px var(--shadow);
}
section h2 { margin: 0 0 18px; font-size: 1.15rem; color: var(--accent); }
section h3 { margin: 0 0 12px; font-size: 0.95rem; color: var(--fg); }
.grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 22px; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th, td { padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: left; }
th { color: var(--fg-dim); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; background: var(--bg-3); }
tr:hover td { background: rgba(255,255,255,0.03); }
.sortable { cursor: pointer; user-select: none; }
.sortable:hover { color: var(--accent); }
.pill {
  display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 0.72rem;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em;
}
.pill-green { background: rgba(34,197,94,0.14); color: var(--green); border: 1px solid rgba(34,197,94,0.35); }
.pill-yellow { background: rgba(234,179,8,0.14); color: var(--yellow); border: 1px solid rgba(234,179,8,0.35); }
.pill-red { background: rgba(239,68,68,0.14); color: var(--red); border: 1px solid rgba(239,68,68,0.35); }
.pill-gray { background: rgba(107,114,128,0.14); color: var(--gray); border: 1px solid rgba(107,114,128,0.35); }
.row-green td { border-left: 3px solid var(--green); }
.row-yellow td { border-left: 3px solid var(--yellow); }
.row-red td { border-left: 3px solid var(--red); }
.row-gray td { border-left: 3px solid var(--gray); }
.bar-chart { display: flex; flex-direction: column; gap: 10px; }
.bar-row { display: flex; align-items: center; gap: 12px; }
.bar-label { width: 200px; font-size: 0.82rem; color: var(--fg); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-track { flex: 1; height: 22px; background: var(--bg-3); border-radius: 5px; overflow: hidden; position: relative; }
.bar-fill { height: 100%; border-radius: 5px; transition: width 0.4s ease; }
.bar-value { width: 44px; text-align: right; font-size: 0.8rem; font-weight: 700; color: var(--accent); }
.bar-meta { font-size: 0.75rem; color: var(--fg-dim); margin-top: 2px; }
.contra-row { margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
.contra-row:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
.contra-title { font-size: 0.85rem; color: var(--fg); margin-bottom: 4px; }
.contra-meta { font-size: 0.75rem; color: var(--fg-dim); }
.drift-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.drift-card { background: var(--bg-3); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
.drift-score { font-size: 1.3rem; font-weight: 700; color: var(--accent-2); }
.drift-ents { font-size: 0.75rem; color: var(--fg-dim); margin-top: 6px; }
.meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.meta-card { background: var(--bg-3); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.meta-card h4 { margin: 0 0 8px; font-size: 0.9rem; color: var(--fg); }
.meta-stat { font-size: 0.8rem; color: var(--fg-dim); }
.status-row { display: flex; gap: 24px; flex-wrap: wrap; }
.status-item { min-width: 180px; }
.status-label { font-size: 0.72rem; color: var(--fg-dim); text-transform: uppercase; }
.status-value { font-size: 1rem; color: var(--fg); margin-top: 3px; }
footer { text-align: center; color: var(--fg-dim); font-size: 0.75rem; margin-top: 10px; padding-bottom: 30px; }
.scroll-box { max-height: 520px; overflow: auto; border: 1px solid var(--border); border-radius: 8px; }
.scroll-box table { margin: 0; min-width: 100%; }
@media (max-width: 860px) {
  .grid-2, .drift-grid, .meta-grid { grid-template-columns: 1fr; }
  .bar-label { width: 140px; }
}
""".strip()

SORT_SCRIPT = """
function sortTable(tableId, col, type) {
  const table = document.getElementById(tableId);
  const tbody = table.querySelector('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  let dir = table.dataset.sortDir === 'asc' ? -1 : 1;
  if (table.dataset.sortCol === String(col)) { dir = -dir; }
  table.dataset.sortCol = col;
  table.dataset.sortDir = dir === 1 ? 'asc' : 'desc';
  rows.sort((a, b) => {
    let av = a.children[col].dataset.sort || a.children[col].textContent.trim();
    let bv = b.children[col].dataset.sort || b.children[col].textContent.trim();
    if (type === 'num') { av = parseFloat(av) || 0; bv = parseFloat(bv) || 0; }
    if (av < bv) return -dir; if (av > bv) return dir; return 0;
  });
  rows.forEach(r => tbody.appendChild(r));
}
""".strip()


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------


def load_v30_cards(l2_root: Path | None = None) -> list[dict[str, Any]]:
    """Load all V30 (schema v10) build cards."""
    root = Path(l2_root or L2_ROOT)
    cards: list[dict[str, Any]] = []
    for path in sorted((root / "cards").glob("l2-*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if card.get("schema") == "rig.omniscout.build-card.v10":
            cards.append(card)
    return cards


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_status(card: dict[str, Any]) -> str:
    autobuilt = card.get("autobuilt") or {}
    outcome = card.get("outcome") or {}
    if autobuilt.get("verified") and autobuilt.get("proof_sealed"):
        return "verified"
    if autobuilt.get("built"):
        return "built"
    if outcome.get("status") in ("FAILED", "BUILD_FAILED", "TEST_FAILED"):
        return "failed"
    return "pending"


def _council_verdict(card: dict[str, Any]) -> str:
    council = card.get("council") or {}
    synthesis = council.get("synthesis") or {}
    return synthesis.get("overall_verdict") or "—"


def _revenue_model(card: dict[str, Any]) -> str:
    bi = card.get("business_intelligence") or {}
    return bi.get("revenue_model") or "—"


def _card_display_id(card: dict[str, Any]) -> str:
    return card.get("card_id") or "—"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Pattern engine data (deterministic, computed from card data)
# ---------------------------------------------------------------------------


def load_pattern_anticrowd(
    l2_root: Path | None, cards: list[dict[str, Any]]
) -> dict[str, Any]:
    """Anti-Crowd Scores for empty strategies.

    High ACS = attractive whitespace: the strategy has no cards, but adjacent
    doctrine domains are active.  Deterministic score derived from corpus data.
    """
    path = Path(l2_root or L2_ROOT) / "pattern-anticrowd.json"
    cached = load_json(path, {})
    if cached and isinstance(cached, dict) and "scores" in cached:
        return cached

    # Build keyword -> strategy map from DOCTRINE_DOMAINS.
    keyword_to_empty: dict[str, set[str]] = defaultdict(set)
    for empty in EMPTY_STRATEGIES:
        for keyword in empty.replace("-", " ").split():
            keyword_to_empty[keyword].add(empty)

    scores: dict[str, float] = {}
    for empty in EMPTY_STRATEGIES:
        related = 0
        empty_tokens = set(empty.replace("-", " ").split())
        for card in cards:
            text = " ".join(
                [
                    str(card.get("title", "")),
                    str(card.get("summary", "")),
                    str(card.get("claim", "")),
                    " ".join(str(t) for t in card.get("tags", [])),
                ]
            ).lower()
            tokens = set(re.findall(r"[a-z0-9]{3,}", text))
            if empty_tokens & tokens:
                related += 1
                continue
            sid = card.get("strategy", {}).get("strategy_id", "")
            if any(part in sid for part in empty_tokens):
                related += 1
        # Base opportunity 95, penalise only slightly for related corpus noise.
        acs = max(60.0, min(98.0, 95.0 - related * 0.6))
        scores[empty] = round(acs, 1)

    actions = {
        "automation-runtime": "Produce first runtime-hardened build card",
        "doctrine-control-plane": "Draft governance + operator-doctrine card",
        "knowledge-memory": "Seed semantic-memory architecture card",
        "scraping-intelligence": "Open scraping-intelligence white-space card",
        "legal-compliance": "Investigate compliance-as-code candidate",
        "vertical-dental-ortho": "Investigate dental-ortho vertical proof card",
        "vertical-pe-cfo": "Investigate PE/CFO vertical proof card",
    }
    rows = [
        {
            "strategy_id": sid,
            "acs": scores[sid],
            "action": actions.get(sid, "Investigate whitespace"),
        }
        for sid in EMPTY_STRATEGIES
    ]
    rows.sort(key=lambda r: r["acs"], reverse=True)
    return {"schema": "rig.pattern.anticrowd.v1", "scores": rows}


def load_pattern_contradiction(
    l2_root: Path | None, cards: list[dict[str, Any]]
) -> dict[str, Any]:
    """Contradiction Arbitrage: top contradictions by CAR score."""
    path = Path(l2_root or L2_ROOT) / "pattern-contradiction.json"
    cached = load_json(path, {})
    if cached and isinstance(cached, dict) and "contradictions" in cached:
        return cached

    card_index = {c.get("card_id"): c for c in cards}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for card in cards:
        for con in card.get("semantic_links", {}).get("contradictions", []):
            a = card.get("card_id")
            b = con.get("card_id")
            key = tuple(sorted([a or "", b or ""]))
            if key in seen:
                continue
            seen.add(key)
            sim = _safe_float(con.get("similarity"), 0.0)
            car = round(sim * 100, 1)
            if car >= 25:
                state = "ACTIVE"
            elif car >= 12:
                state = "WATCH"
            else:
                state = "LATENT"
            other = card_index.get(b or "") or {}
            rows.append(
                {
                    "pair": [a, b],
                    "titles": [card.get("title", "—"), other.get("title", con.get("title", "—"))],
                    "similarity": sim,
                    "car": car,
                    "state": state,
                    "reason": con.get("reason", "")[:120],
                }
            )
    rows.sort(key=lambda r: r["car"], reverse=True)
    return {"schema": "rig.pattern.contradiction.v1", "contradictions": rows[:10]}


def load_pattern_drift(
    l2_root: Path | None, cards: list[dict[str, Any]]
) -> dict[str, Any]:
    """Epistemic Drift: top strategies by drift score + novel entities."""
    path = Path(l2_root or L2_ROOT) / "pattern-drift.json"
    cached = load_json(path, {})
    if cached and isinstance(cached, dict) and "drifts" in cached:
        return cached

    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in cards:
        sid = card.get("strategy", {}).get("strategy_id") or "unknown"
        by_strategy[sid].append(card)

    rows: list[dict[str, Any]] = []
    for sid, scards in by_strategy.items():
        if sid == "unknown":
            continue
        scores = [_safe_float(c.get("score", {}).get("total"), 0.0) for c in scards]
        std = statistics.stdev(scores) if len(scores) > 1 else 0.0
        ages = [
            _safe_float(c.get("temporal_validity", {}).get("age_years"), 0.0)
            for c in scards
        ]
        avg_age = sum(ages) / len(ages) if ages else 0.0
        entities: Counter = Counter()
        for c in scards:
            for e in c.get("entities", {}).get("entities", []):
                entities[e.get("name", "")] += 1
        novel = [name for name, count in entities.most_common() if count == 1][:5]
        drift = round(std * 3.5 + avg_age * 8.0 + len(novel) * 1.2, 1)
        rows.append(
            {
                "strategy_id": sid,
                "drift_score": drift,
                "card_count": len(scards),
                "avg_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
                "novel_entities": novel,
            }
        )
    rows.sort(key=lambda r: r["drift_score"], reverse=True)
    return {"schema": "rig.pattern.drift.v1", "drifts": rows[:10]}


def load_pattern_cards(l2_root: Path | None) -> list[dict[str, Any]]:
    """Load generated pattern cards if any exist."""
    path = Path(l2_root or L2_ROOT) / "pattern-cards"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for f in sorted(path.glob("*.json")):
        data = load_json(f, None)
        if data:
            rows.append(data)
    return rows


def load_meta_cards(l2_root: Path | None) -> list[dict[str, Any]]:
    """Deduplicate meta-cards by title, returning one row per product cluster."""
    path = Path(l2_root or L2_ROOT) / "meta-cards"
    if not path.exists():
        return []
    seen: dict[str, dict[str, Any]] = {}
    for f in sorted(path.glob("meta-*.json")):
        data = load_json(f, {})
        title = data.get("title") or f.stem
        if title in seen:
            continue
        seen[title] = data
    rows = []
    for title, data in seen.items():
        rows.append(
            {
                "title": title,
                "card_count": _safe_int(data.get("card_count"), 0),
                "avg_score": _safe_float(data.get("avg_score"), 0.0),
                "combined_revenue": _safe_float(
                    data.get("combined_revenue_potential"), 0.0
                ),
                "revenue_model": data.get("combined_revenue_model") or "—",
                "investment_thesis": data.get("investment_thesis") or "",
            }
        )
    rows.sort(key=lambda r: r["combined_revenue"], reverse=True)
    return rows


def load_nightly(l2_root: Path | None) -> dict[str, Any]:
    path = Path(l2_root or L2_ROOT) / "latest-nightly.json"
    return load_json(path, {}) or {}


# ---------------------------------------------------------------------------
# Build / 256GB results
# ---------------------------------------------------------------------------


def build_256gb_results(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Table of builds that ran on the 256GB node."""
    deep_model = os.environ.get("OMNISCOUT_DEEP_MODEL", "qwen3-coder:30b")
    rows: list[dict[str, Any]] = []
    for card in cards:
        outcome = card.get("outcome") or {}
        if outcome.get("tested_on") != "rig256gb":
            continue
        autobuilt = card.get("autobuilt") or {}
        status = _build_status(card)
        started = _parse_dt(outcome.get("build_started_at"))
        shipped = _parse_dt(outcome.get("shipped_at"))
        elapsed_s = (
            round((shipped - started).total_seconds(), 1)
            if started and shipped
            else None
        )
        rows.append(
            {
                "card_id": card.get("card_id", "—"),
                "title": card.get("title", "—"),
                "strategy_id": card.get("strategy", {}).get("strategy_id", "—"),
                "status": status,
                "model": deep_model,
                "elapsed_s": elapsed_s,
                "tested_at": outcome.get("tested_at", "—"),
            }
        )
    rows.sort(key=lambda r: r["tested_at"] or "", reverse=True)
    return rows


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------


def _esc(text: Any) -> str:
    return html.escape(str(text))


def _status_pill(status: str) -> str:
    mapping = {
        "verified": ("Verified", "pill-green"),
        "built": ("Built", "pill-yellow"),
        "failed": ("Failed", "pill-red"),
        "pending": ("Pending", "pill-gray"),
    }
    label, cls = mapping.get(status, (status.title(), "pill-gray"))
    return f'<span class="pill {cls}">{_esc(label)}</span>'


def _row_class(status: str) -> str:
    return {
        "verified": "row-green",
        "built": "row-yellow",
        "failed": "row-red",
    }.get(status, "row-gray")


def _bar_color(value: float) -> str:
    if value >= 80:
        return "linear-gradient(90deg, #22c55e, #4ade80)"
    if value >= 60:
        return "linear-gradient(90deg, #eab308, #facc15)"
    return "linear-gradient(90deg, #ef4444, #f87171)"


def _header_html(date_str: str, total: int, built: int, sealed: int) -> str:
    return f"""
<header>
  <h1>RIG OmniScout — Build Card Intelligence Dashboard</h1>
  <div class="subtitle">Generated {date_str}</div>
  <div class="kpi-row">
    <div class="kpi"><div class="value">{total}</div><div class="label">Total Cards</div></div>
    <div class="kpi"><div class="value">{built}</div><div class="label">Built</div></div>
    <div class="kpi"><div class="value">{sealed}</div><div class="label">Proofs Sealed</div></div>
    <div class="kpi"><div class="value">{total - built}</div><div class="label">Pending</div></div>
  </div>
</header>
"""


def _pattern_anticrowd_html(data: dict[str, Any]) -> str:
    rows = data.get("scores", [])
    bars = []
    for row in rows:
        sid = row.get("strategy_id", "")
        acs = _safe_float(row.get("acs"), 0.0)
        action = row.get("action", "")
        bars.append(
            f"""
      <div class="bar-row">
        <div class="bar-label" title="{_esc(sid)}">{_esc(sid)}</div>
        <div class="bar-track"><div class="bar-fill" style="width:{acs}%; background:{_bar_color(acs)}"></div></div>
        <div class="bar-value">{acs:.1f}</div>
      </div>
      <div class="bar-meta">{_esc(action)}</div>
"""
        )
    return f"""
<section>
  <h2>Pattern Engine Scores</h2>
  <div class="grid-2">
    <div>
      <h3>Anti-Crowd Scores — Empty Strategy Whitespace</h3>
      <div class="bar-chart">{''.join(bars)}</div>
    </div>
"""


def _pattern_contradiction_html(data: dict[str, Any]) -> str:
    rows = data.get("contradictions", [])
    items = []
    for row in rows:
        titles = row.get("titles", ["—", "—"])
        car = _safe_float(row.get("car"), 0.0)
        state = row.get("state", "LATENT")
        reason = row.get("reason", "")
        state_class = (
            "pill-red" if state == "ACTIVE" else "pill-yellow" if state == "WATCH" else "pill-gray"
        )
        items.append(
            f"""
      <div class="contra-row">
        <div class="contra-title">{_esc(titles[0][:72])} <span style="color:var(--fg-dim)">⇄</span> {_esc(titles[1][:72])}</div>
        <div class="contra-meta">
          <span class="pill {state_class}">{state}</span>
          CAR {car:.1f} — {_esc(reason)}
        </div>
      </div>
"""
        )
    return f"""
    <div>
      <h3>Contradiction Arbitrage — Top 10</h3>
      {''.join(items) if items else '<div class="bar-meta">No contradictions detected.</div>'}
    </div>
  </div>
"""


def _pattern_drift_html(data: dict[str, Any]) -> str:
    rows = data.get("drifts", [])
    cards = []
    for row in rows:
        sid = row.get("strategy_id", "")
        drift = _safe_float(row.get("drift_score"), 0.0)
        count = _safe_int(row.get("card_count"), 0)
        avg = _safe_float(row.get("avg_score"), 0.0)
        ents = row.get("novel_entities", [])
        ent_str = ", ".join(ents) if ents else "—"
        cards.append(
            f"""
      <div class="drift-card">
        <div style="font-size:0.78rem;color:var(--fg-dim)">{_esc(sid)} · {count} cards · avg {avg:.1f}</div>
        <div class="drift-score">{drift:.1f}</div>
        <div class="drift-ents">Novel entities: {_esc(ent_str[:90])}</div>
      </div>
"""
        )
    return f"""
  <h3 style="margin-top:22px">Epistemic Drift — Top 10 Strategies</h3>
  <div class="drift-grid">{''.join(cards)}</div>
</section>
"""


def _portfolio_html(cards: list[dict[str, Any]]) -> str:
    rows = []
    for card in cards:
        sid = card.get("strategy", {}).get("strategy_id", "unknown")
        tier = card.get("strategy", {}).get("tier", "—")
        rank = card.get("score", {}).get("rank", "—")
        score = _safe_float(card.get("score", {}).get("total"), 0.0)
        status = _build_status(card)
        verdict = _council_verdict(card)
        revenue = _revenue_model(card)
        rows.append(
            f"""
      <tr class="{_row_class(status)}">
        <td>{_esc(_card_display_id(card))}</td>
        <td>{_esc(card.get('title','—')[:70])}</td>
        <td>{_esc(sid)}</td>
        <td data-sort="{tier}">{_esc(tier)}</td>
        <td data-sort="{score}">{score:.0f}</td>
        <td>{_status_pill(status)}</td>
        <td>{_esc('Yes' if card.get('autobuilt',{}).get('proof_sealed') else 'No')}</td>
        <td>{_esc(verdict)}</td>
        <td>{_esc(revenue)}</td>
      </tr>
"""
        )
    return f"""
<section>
  <h2>Build Card Portfolio</h2>
  <div class="scroll-box">
    <table id="portfolio-table" data-sort-col="" data-sort-dir="">
      <thead>
        <tr>
          <th>Card ID</th>
          <th>Title</th>
          <th class="sortable" onclick="sortTable('portfolio-table',2,'str')">Strategy ▾</th>
          <th class="sortable" onclick="sortTable('portfolio-table',3,'str')">Tier ▾</th>
          <th class="sortable" onclick="sortTable('portfolio-table',4,'num')">Score ▾</th>
          <th>Build Status</th>
          <th>Proof Sealed</th>
          <th>Council Verdict</th>
          <th>Revenue Model</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</section>
"""


def _format_revenue_model(value: Any) -> str:
    if isinstance(value, dict):
        models = value.get("revenue_models") or []
        prices = value.get("price_ranges") or []
        parts = []
        if models:
            parts.append(str(models[0]))
        if prices:
            parts.append(f"({prices[0]})")
        return " ".join(parts) if parts else "—"
    return str(value) if value else "—"


def _meta_cards_html(rows: list[dict[str, Any]]) -> str:
    cards = []
    for row in rows:
        rev = _format_revenue_model(row.get("revenue_model"))
        thesis = str(row.get("investment_thesis") or "")
        if not thesis and isinstance(row.get("revenue_model"), dict):
            thesis = str((row.get("revenue_model") or {}).get("revenue_ideas", [""])[0] or "")
        cards.append(
            f"""
      <div class="meta-card">
        <h4>{_esc(row.get('title','—')[:55])}</h4>
        <div class="meta-stat">
          <strong>{row.get('card_count',0)}</strong> cards ·
          avg score <strong>{row.get('avg_score',0.0):.1f}</strong> ·
          combined revenue <strong>${row.get('combined_revenue',0.0):.1f}M</strong>
        </div>
        <div class="meta-stat" style="margin-top:4px;color:var(--fg-dim)">{_esc(rev)}</div>
        <div class="meta-stat" style="margin-top:6px;font-size:0.75rem">{_esc(thesis[:140])}</div>
      </div>
"""
        )
    return f"""
<section>
  <h2>Meta-Cards — Product Candidates ({len(rows)} clusters)</h2>
  <div class="meta-grid">{''.join(cards)}</div>
</section>
"""


def _nightly_html(nightly: dict[str, Any]) -> str:
    started = nightly.get("started_at", "—")
    finished = nightly.get("finished_at", "—")
    elapsed = _safe_float(nightly.get("elapsed_s"), 0.0)
    produced = nightly.get("produced_this_run", 0)
    target = nightly.get("target", 0)
    enrich = nightly.get("enrichment", {})
    enrich_v3 = nightly.get("enrichment_v3", {})
    enrich_v10 = nightly.get("enrichment_v10", {})
    disk = nightly.get("disk", {}) or {}

    # Next scheduled run: tomorrow at 04:00 UTC, derived from finished_at.
    next_run = "—"
    try:
        finished_dt = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
        next_dt = finished_dt.replace(hour=4, minute=0, second=0, microsecond=0)
        if next_dt <= finished_dt:
            next_dt = next_dt.replace(day=next_dt.day + 1)
        next_run = next_dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        pass

    return f"""
<section>
  <h2>Nightly Status</h2>
  <div class="status-row">
    <div class="status-item"><div class="status-label">Last Run Started</div><div class="status-value">{_esc(started)}</div></div>
    <div class="status-item"><div class="status-label">Last Run Finished</div><div class="status-value">{_esc(finished)}</div></div>
    <div class="status-item"><div class="status-label">Elapsed</div><div class="status-value">{elapsed:.1f}s</div></div>
    <div class="status-item"><div class="status-label">Cards Produced</div><div class="status-value">{produced} / {target}</div></div>
    <div class="status-item"><div class="status-label">Enriched</div><div class="status-value">{enrich.get('enriched',0)} v1 · {enrich_v3.get('enriched_v3',0)} v3 · {enrich_v10.get('enriched_v10',0)} v10</div></div>
    <div class="status-item"><div class="status-label">Exported</div><div class="status-value">{enrich_v10.get('obsidian_exported',0)} to Obsidian</div></div>
    <div class="status-item"><div class="status-label">Disk</div><div class="status-value">{disk.get('free_gib',0):.1f} GiB free ({disk.get('used_pct',0):.1f}% used)</div></div>
    <div class="status-item"><div class="status-label">Next Scheduled Run</div><div class="status-value">{_esc(next_run)}</div></div>
  </div>
</section>
"""


def _build256_html(rows: list[dict[str, Any]]) -> str:
    table_rows = []
    for row in rows:
        status = row.get("status", "pending")
        elapsed = row.get("elapsed_s")
        elapsed_str = f"{elapsed:.1f}s" if elapsed is not None else "—"
        table_rows.append(
            f"""
      <tr class="{_row_class(status)}">
        <td>{_esc(row.get('card_id','—'))}</td>
        <td>{_esc(row.get('title','—')[:60])}</td>
        <td>{_esc(row.get('strategy_id','—'))}</td>
        <td>{_status_pill(status)}</td>
        <td>{_esc(row.get('model','—'))}</td>
        <td>{elapsed_str}</td>
      </tr>
"""
        )
    return f"""
<section>
  <h2>256GB Build Results ({len(rows)} builds)</h2>
  <div class="scroll-box">
    <table>
      <thead>
        <tr>
          <th>Card ID</th>
          <th>Title</th>
          <th>Strategy</th>
          <th>Status</th>
          <th>Model</th>
          <th>Elapsed</th>
        </tr>
      </thead>
      <tbody>{''.join(table_rows)}</tbody>
    </table>
  </div>
</section>
"""


def generate_html(context: dict[str, Any]) -> str:
    """Assemble the complete self-contained HTML document."""
    header = _header_html(
        context["generated_at"],
        context["total_cards"],
        context["total_built"],
        context["total_sealed"],
    )
    pattern = (
        _pattern_anticrowd_html(context["anticrowd"])
        + _pattern_contradiction_html(context["contradiction"])
        + _pattern_drift_html(context["drift"])
    )
    portfolio = _portfolio_html(context["cards"])
    meta = _meta_cards_html(context["meta_cards"])
    nightly = _nightly_html(context["nightly"])
    build256 = _build256_html(context["build256"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RIG OmniScout — Build Card Intelligence Dashboard</title>
  <style>{HTML_THEME}</style>
</head>
<body>
  <div class="container">
    {header}
    {pattern}
    {portfolio}
    {meta}
    {nightly}
    {build256}
    <footer>Generated by rig_foundry.pattern_dashboard · deterministic · no LLM calls</footer>
  </div>
  <script>{SORT_SCRIPT}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_dashboard(
    l2_root: Path | str | None = None,
    output_path: Path | str | None = None,
) -> dict[str, Any]:
    """Build the dashboard HTML and write it to disk.

    Returns a summary dict with file path, counts, and pattern-engine data.
    """
    if l2_root:
        root = Path(l2_root)
    elif DEFAULT_DASHBOARD_ROOT.exists() and any(DEFAULT_DASHBOARD_ROOT.glob("cards/l2-*.json")):
        root = DEFAULT_DASHBOARD_ROOT
    else:
        root = L2_ROOT
    out = Path(output_path or DASHBOARD_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)

    cards = load_v30_cards(root)
    total_cards = len(cards)
    total_built = sum(1 for c in cards if c.get("autobuilt", {}).get("built"))
    total_sealed = sum(1 for c in cards if c.get("autobuilt", {}).get("proof_sealed"))

    anticrowd = load_pattern_anticrowd(root, cards)
    contradiction = load_pattern_contradiction(root, cards)
    drift = load_pattern_drift(root, cards)
    meta_cards = load_meta_cards(root)
    nightly = load_nightly(root)
    build256 = build_256gb_results(cards)

    context = {
        "generated_at": utc_now(),
        "l2_root": str(root),
        "total_cards": total_cards,
        "total_built": total_built,
        "total_sealed": total_sealed,
        "cards": cards,
        "anticrowd": anticrowd,
        "contradiction": contradiction,
        "drift": drift,
        "meta_cards": meta_cards,
        "nightly": nightly,
        "build256": build256,
    }

    html_text = generate_html(context)
    atomic_text(out, html_text)  # atomic write as plain HTML

    # Also emit a small digest next to the HTML for downstream consumers.
    digest = {
        "html_path": str(out),
        "html_sha256": sha256_text(html_text),
        "cards_count": total_cards,
        "built_count": total_built,
        "sealed_count": total_sealed,
        "pattern_anticrowd_scores": len(anticrowd.get("scores", [])),
        "pattern_contradictions": len(contradiction.get("contradictions", [])),
        "pattern_drifts": len(drift.get("drifts", [])),
        "meta_card_clusters": len(meta_cards),
        "build256_count": len(build256),
        "generated_at": context["generated_at"],
    }
    digest_path = out.with_suffix(".digest.json")
    atomic_json(digest_path, digest)

    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RIG OmniScout build-card intelligence dashboard"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="generate",
        choices=["generate"],
        help="generate the dashboard HTML and open it",
    )
    parser.add_argument(
        "--l2-root",
        default=None,
        help="Override L2_ROOT path",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output HTML path",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the dashboard in a browser",
    )
    args = parser.parse_args(argv)

    digest = generate_dashboard(l2_root=args.l2_root, output_path=args.output)
    print(stable_json(digest))

    if not args.no_open:
        try:
            webbrowser.open(f"file://{digest['html_path']}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
