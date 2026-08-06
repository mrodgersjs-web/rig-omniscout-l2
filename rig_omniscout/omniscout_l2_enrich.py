"""Enrich L2 build cards to v2: analysis, direction, prompts, math, tags, images.

Upgrades every card from a note into a full analytical artifact with:
- analysis: mechanism breakdown, evidence quality, tradeoffs, confidence
- direction: where this leads, strategic implications, build-next
- prompts: reusable agent/human prompts for the card's domain
- math: LaTeX formulas where applicable (scoring, probability, complexity, economics)
- tags: rich auto-generated taxonomy
- image: deterministic SVG concept diagram (not random AI art)
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from rig_foundry.omniscout_build_cards import (  # noqa: E402
    L2_CARDS,
    L2_ROOT,
    atomic_json,
    atomic_text,
    sha256_text,
    slugify,
    stable_json,
    utc_now,
    DOCTRINE_DOMAINS,
    score_build_card,
)

JAKE = Path(os.environ.get("JAKESTUDIO_VAULT", str(Path.home() / "Documents" / "JakeStudio")))
SCHEMA_V2 = "rig.omniscout.build-card.v2"

# ---------------------------------------------------------------------------
# Tag generation
# ---------------------------------------------------------------------------

TAG_TAXONOMY: dict[str, list[str]] = {
    "domain": ["agents", "inference", "memory", "scraping", "automation", "proof", "doctrine", "fleet", "gtm", "healthcare", "finance", "design", "security", "leadership"],
    "method": ["empirical", "theoretical", "survey", "benchmark", "meta-analysis", "system-design", "case-study"],
    "rig-pillar": ["product-truth", "revenue-wedge", "moat-depth", "operator-readiness"],
    "card-type": ["mechanism", "pattern", "evidence-pack", "build-slice", "doctrine-candidate"],
    "quality": ["high-signal", "multi-source", "consensus-backed", "consensus-mcp"],
}


def generate_tags(card: dict[str, Any]) -> list[str]:
    """Rich tag taxonomy from card content."""
    blob = " ".join([
        str(card.get("title") or ""),
        str(card.get("claim") or ""),
        str(card.get("summary") or ""),
        str(card.get("mechanism") or ""),
        " ".join(str(d) for d in card.get("doctrine_domains") or []),
        str((card.get("strategy") or {}).get("strategy_id") or ""),
        str((card.get("strategy") or {}).get("tier") or ""),
    ]).lower()

    tags: list[str] = []
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    tier = (card.get("strategy") or {}).get("tier") or "na"
    rank = (card.get("score") or {}).get("rank") or "?"

    # structural
    tags.append(f"strategy:{sid}")
    tags.append(f"tier:{tier}")
    tags.append(f"rank:{str(rank).lower()}")
    tags.append("build-card-v2")

    # consensus
    if (card.get("consensus") or {}).get("used"):
        tags.append("consensus-backed")
        tags.append("consensus-mcp")

    # domain tags from doctrine match
    for domain, kws in DOCTRINE_DOMAINS.items():
        if any(k in blob for k in kws):
            tags.append(f"domain:{domain}")

    # method tags
    method_map = {
        "empirical": ["experiment", "benchmark", "measurement", "evaluat"],
        "theoretical": ["theorem", "proof", "formal", "axiom", "lemma"],
        "survey": ["survey", "review", "landscape", "taxonomy"],
        "meta-analysis": ["meta-analysis", "systematic review"],
        "system-design": ["architecture", "system design", "pipeline", "infrastructure"],
    }
    for method, kws in method_map.items():
        if any(k in blob for k in kws):
            tags.append(f"method:{method}")

    # RIG pillar
    if tier == "T0":
        tags.append("pillar:product-truth")
    elif tier == "T1":
        tags.append("pillar:revenue-wedge")
    elif tier == "T2":
        tags.append("pillar:moat-depth")

    # card-type
    if card.get("mechanism") and len(str(card.get("mechanism") or "")) > 200:
        tags.append("type:mechanism")
    if card.get("pattern", {}).get("name"):
        tags.append("type:pattern")
    if (card.get("idea") or {}).get("done_test"):
        tags.append("type:build-slice")
    if rank in {"STRONG", "EXCELLENT"}:
        tags.append("type:doctrine-candidate")

    # dedupe
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:30]


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def generate_analysis(card: dict[str, Any]) -> dict[str, Any]:
    """Structured analytical breakdown — deterministic from card fields."""
    score = card.get("score") or {}
    breakdown = score.get("breakdown") or {}
    sources = card.get("sources") or {}
    evidence = card.get("evidence") or []
    mechanism = str(card.get("mechanism") or "")
    claim = str(card.get("claim") or "")
    risks = card.get("risks") or []
    assumptions = card.get("assumptions") or []
    kill = card.get("kill_criteria") or []
    consensus = card.get("consensus") or {}

    # evidence quality assessment
    ev_urls = sum(1 for e in evidence if isinstance(e, dict) and e.get("url"))
    ev_with_numbers = sum(
        1
        for e in evidence
        if isinstance(e, dict) and re.search(r"\d", str(e.get("quote_or_fact") or ""))
    )
    consensus_count = int(consensus.get("count") or 0)

    # mechanism depth
    mech_words = len(mechanism.split())
    mech_sentences = len(re.findall(r"[.!?]+", mechanism))
    has_steps = bool(re.search(r"\b\d+[.)]|\bstep\b|\bfirst\b|\bthen\b", mechanism, re.I))

    # confidence model
    ms = (breakdown.get("multi_source") or {}).get("score", 0)
    md = (breakdown.get("mechanism_density") or {}).get("score", 0)
    ea = (breakdown.get("evidence_anchoring") or {}).get("score", 0)
    ts = (breakdown.get("tac_structure") or {}).get("score", 0)
    total = score.get("total", 0)

    # confidence bands
    if total >= 90:
        confidence = "HIGH"
        confidence_rationale = "Multi-source + Consensus-backed + mechanism-dense + executable done-test"
    elif total >= 75:
        confidence = "MEDIUM-HIGH"
        confidence_rationale = "Solid multi-source evidence with mechanism, but gaps in depth or coverage"
    elif total >= 60:
        confidence = "MEDIUM"
        confidence_rationale = "Partial evidence; mechanism may be thin or sources limited"
    else:
        confidence = "LOW"
        confidence_rationale = "Insufficient evidence or mechanism for operational use"

    # tradeoffs
    tradeoffs: list[str] = []
    if md < 10:
        tradeoffs.append("Mechanism under-specified — operational risk if deployed without deeper analysis")
    if ms < 12:
        tradeoffs.append("Source diversity limited — confirmation bias risk")
    if consensus_count < 2:
        tradeoffs.append("Consensus backing thin — claims not cross-validated against peer-reviewed literature")
    if not (card.get("tac") or {}).get("verifier"):
        tradeoffs.append("No independent verifier named — GEV separation incomplete")
    if has_steps and mech_sentences >= 3:
        tradeoffs.append("Step-by-step mechanism present — good for implementation but may over-specify")

    # gaps
    gaps: list[str] = []
    if not risks:
        gaps.append("No explicit risks listed — add failure modes before operationalizing")
    if not kill:
        gaps.append("No kill criteria — when should we abandon this line?")
    if not assumptions:
        gaps.append("No assumptions stated — hidden dependencies may break the pattern")
    if ev_urls < 3:
        gaps.append(f"Only {ev_urls} cited evidence URLs — need ≥3 for doctrine promotion")

    return {
        "confidence": confidence,
        "confidence_rationale": confidence_rationale,
        "evidence_quality": {
            "evidence_urls": ev_urls,
            "evidence_with_numbers": ev_with_numbers,
            "consensus_papers": consensus_count,
            "source_count": int(sources.get("count") or 0),
            "verdict": "strong" if ev_urls >= 3 and consensus_count >= 2 else ("adequate" if ev_urls >= 2 else "weak"),
        },
        "mechanism_depth": {
            "word_count": mech_words,
            "sentence_count": mech_sentences,
            "has_steps": has_steps,
            "verdict": "deep" if mech_words >= 200 and has_steps else ("moderate" if mech_words >= 80 else "shallow"),
        },
        "tradeoffs": tradeoffs or ["No significant tradeoffs detected at current evidence level"],
        "gaps": gaps or ["No critical gaps detected"],
        "score_decomposition": {
            "multi_source": f"{ms}/18",
            "mechanism": f"{md}/18",
            "evidence": f"{ea}/14",
            "tac_structure": f"{ts}/12",
            "total": f"{total}/100",
        },
    }


# ---------------------------------------------------------------------------
# Direction
# ---------------------------------------------------------------------------


def generate_direction(card: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Forward-looking strategic direction."""
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    tier = (card.get("strategy") or {}).get("tier") or "na"
    rank = (card.get("score") or {}).get("rank") or "?"
    title = card.get("title") or ""
    idea = card.get("idea") or {}
    next_actions = card.get("next_actions") or []
    pattern = card.get("pattern") or {}
    confidence = analysis.get("confidence", "MEDIUM")

    # where this leads
    if tier == "T0":
        leads_to = f"This card advances RIG's core product truth in `{sid}`. "
        if rank in {"STRONG", "EXCELLENT"}:
            leads_to += "Promote to doctrine-candidate queue for agent-loadable rules."
        else:
            leads_to += "Strengthen evidence before doctrine promotion."
    elif tier == "T1":
        leads_to = f"This card supports RIG's revenue wedge in `{sid}`. "
        leads_to += "Route to GTM/vertical agent packs once evidence is confirmed."
    else:
        leads_to = f"This card expands RIG's moat depth in `{sid}`. "
        leads_to += "Archive for horizon scanning; promote if signal repeats."

    # strategic implications
    implications: list[str] = []
    if confidence == "HIGH":
        implications.append("Evidence is strong enough to act on now — build the slice")
    elif confidence == "MEDIUM-HIGH":
        implications.append("Evidence is actionable but verify one more independent source first")
    else:
        implications.append("Evidence needs strengthening before operational deployment")

    if pattern.get("name"):
        implications.append(f"Pattern `{pattern['name']}` is reusable across multiple RIG workstreams")
    if idea.get("done_test"):
        implications.append("Build slice has an executable done-test — ready for engineering")

    # build-next
    build_next: list[str] = []
    if idea.get("name"):
        build_next.append(f"Implement `{idea['name']}` as a governed build slice with ProofPacket")
    build_next.append(f"Create a regression test proving the `{sid}` pattern works as claimed")
    if rank in {"STRONG", "EXCELLENT"}:
        build_next.append("Draft doctrine promotion packet (v0→v1) for agent-loadable rule")
    build_next.append("Schedule weekly review to track if new evidence contradicts or extends this card")

    # risk direction
    risk_direction = "Standard monitoring"
    gaps = analysis.get("gaps") or []
    if any("kill" in g.lower() for g in gaps):
        risk_direction = "Define kill criteria before investing engineering time"
    elif any("risk" in g.lower() for g in gaps):
        risk_direction = "Enumerate failure modes before operationalizing"

    return {
        "leads_to": leads_to,
        "strategic_implications": implications,
        "build_next": build_next[:5],
        "risk_direction": risk_direction,
        "doctrine_promotion_path": {
            "current_rank": rank,
            "eligible_for_v1": rank in {"STRONG", "EXCELLENT"} and confidence in {"HIGH", "MEDIUM-HIGH"},
            "next_step": "Draft promotion packet with mechanism + regression test + 3+ independent sources" if rank in {"STRONG", "EXCELLENT"} else "Strengthen evidence to STRONG+ first",
        },
        "agent_routing": {
            "department": _route_to_department(sid),
            "action": "ingest as build-card substrate" if tier in {"T0", "T1"} else "archive for horizon scan",
        },
    }


def _route_to_department(sid: str) -> str:
    routing = {
        "proof-false-done": "intelligence",
        "agent-engineering": "intelligence",
        "determinism-gates": "intelligence",
        "local-inference-fleet": "data",
        "knowledge-memory": "intelligence",
        "automation-runtime": "operations",
        "scraping-intelligence": "intelligence",
        "doctrine-control-plane": "intelligence",
        "gtm-sales": "gtm",
        "pricing-finance": "gtm",
        "healthcare-ai-ops": "gtm",
        "marketing-content-linkedin": "content",
        "forecasting-calibration": "intelligence",
        "strategy-decision-routing": "strategy",
        "founder-performance": "operations",
        "ai-business-models": "gtm",
        "vertical-law-cpa": "gtm",
        "vertical-dental-ortho": "gtm",
        "vertical-pe-cfo": "gtm",
        "customer-success-expansion": "gtm",
        "competitive-intel": "intelligence",
        "cybersecurity": "intelligence",
        "product-design": "design",
        "leadership-org": "operations",
        "legal-compliance": "operations",
        "operations": "operations",
    }
    return routing.get(sid, "intelligence")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def generate_prompts(card: dict[str, Any]) -> list[dict[str, str]]:
    """Reusable prompts for agents and humans based on the card."""
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    title = card.get("title") or ""
    claim = str(card.get("claim") or "")[:200]
    pattern = card.get("pattern") or {}
    idea = card.get("idea") or {}
    mechanism = str(card.get("mechanism") or "")[:400]

    prompts: list[dict[str, str]] = []

    # agent prompt — build slice
    prompts.append({
        "name": f"build-{slugify(title, 30)}",
        "type": "agent",
        "prompt": (
            f"You are a RIG build agent. Implement the build slice from this card.\n\n"
            f"Strategy: {sid}\nClaim: {claim}\nMechanism: {mechanism}\n"
            f"Done-test: {idea.get('done_test', 'define one')}\n\n"
            f"1. Implement the slice\n"
            f"2. Run the done-test\n"
            f"3. Seal a ProofPacket with the artifact hash\n"
            f"4. Report: PASS/FAIL + evidence"
        ),
    })

    # agent prompt — contradiction check
    prompts.append({
        "name": f"contradict-{slugify(title, 30)}",
        "type": "agent",
        "prompt": (
            f"You are an adversarial verifier. Try to REFUTE this claim:\n\n"
            f"Claim: {claim}\n\n"
            f"Search for contradicting evidence. If you find any, report the contradiction. "
            f"If you cannot refute after 3 searches, report CONFIRMED."
        ),
    })

    # human prompt — strategic review
    prompts.append({
        "name": f"review-{slugify(title, 30)}",
        "type": "human",
        "prompt": (
            f"Strategic review prompt for: {title}\n\n"
            f"1. Does this claim change how we operate in {sid}?\n"
            f"2. What's the cost of being wrong?\n"
            f"3. Should we promote this to a doctrine rule?\n"
            f"4. What would kill this line of inquiry?"
        ),
    })

    # agent prompt — doctrine promotion draft
    if pattern.get("name"):
        prompts.append({
            "name": f"doctrine-{slugify(pattern['name'], 30)}",
            "type": "agent",
            "prompt": (
                f"Draft a doctrine promotion packet (v0→v1) for this pattern:\n\n"
                f"Pattern: {pattern.get('name')}\n"
                f"Description: {pattern.get('description')}\n"
                f"When to use: {pattern.get('when_to_use')}\n"
                f"When NOT to use: {pattern.get('when_not_to_use')}\n\n"
                f"Format: named rule + mechanism + regression test + 3 independent sources"
            ),
        })

    return prompts


# ---------------------------------------------------------------------------
# Mathematical formulas
# ---------------------------------------------------------------------------


def generate_math(card: dict[str, Any], analysis: dict[str, Any]) -> list[dict[str, str]]:
    """Generate relevant LaTeX formulas based on card domain and content."""
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    claim = str(card.get("claim") or "").lower()
    blob = str(card.get("summary") or "") + str(card.get("mechanism") or "")
    score = card.get("score") or {}
    total = int(score.get("total") or 0)
    formulas: list[dict[str, str]] = []

    # scoring model formula (always relevant — it IS the card's own score)
    breakdown = score.get("breakdown") or {}
    ms = (breakdown.get("multi_source") or {}).get("score", 0)
    md = (breakdown.get("mechanism_density") or {}).get("score", 0)
    ea = (breakdown.get("evidence_anchoring") or {}).get("score", 0)
    ts = (breakdown.get("tac_structure") or {}).get("score", 0)
    df = (breakdown.get("doctrine_fit") or {}).get("score", 0)
    np_ = (breakdown.get("novelty_pattern") or {}).get("score", 0)
    act = (breakdown.get("actionability") or {}).get("score", 0)
    gev = (breakdown.get("gev_separation") or {}).get("score", 0)
    formulas.append({
        "name": "Card Quality Score",
        "latex": (
            f"S = S_{{ms}} + S_{{md}} + S_{{ea}} + S_{{tac}} + S_{{df}} + S_{{np}} + S_{{act}} + S_{{gev}} "
            f"= {ms} + {md} + {ea} + {ts} + {df} + {np_} + {act} + {gev} = {total}"
        ),
        "description": "Weighted composite (0-100). Weights: multi_source(18) + mechanism(18) + evidence(14) + TAC(12) + doctrine(12) + novelty(10) + actionability(8) + GEV(8).",
    })

    # domain-specific formulas
    if sid in {"proof-false-done", "determinism-gates", "agent-engineering"}:
        formulas.append({
            "name": "False-Completion Escape Rate",
            "latex": r"R_{escape} = \frac{N_{claimed\_done} - N_{proven\_done}}{N_{total\_tasks}}",
            "description": "RIG honesty metric. Target: $R_{escape} = 0$. Every claimed-done must have a real command exit code behind it.",
        })
        formulas.append({
            "name": "GEV Separation Contract",
            "latex": r"I_{gev} = \mathbb{1}[team(E) \neq team(V) \wedge identity(E) \neq identity(V)]",
            "description": "Builder $E$ and verifier $V$ must be different teams AND different service identities. $I_{gev}=1$ means independent verification.",
        })

    if sid in {"local-inference-fleet"}:
        formulas.append({
            "name": "Fleet Throughput",
            "latex": r"T_{fleet} = \sum_{i=1}^{n} \frac{c_i}{t_i} \cdot u_i",
            "description": "Aggregate tokens/sec across $n$ nodes, where $c_i$=concurrency, $t_i$=avg latency, $u_i$=utilization [0,1].",
        })
        formulas.append({
            "name": "Model Routing Cost",
            "latex": r"C_{route}(q) = \min_{m \in M} \left[ \alpha \cdot L_m(q) + \beta \cdot P_m(q) \right]",
            "description": "Route query $q$ to model $m$ that minimizes latency $L$ + quality penalty $P$, weighted by $\\alpha, \\beta$.",
        })

    if sid in {"forecasting-calibration"}:
        formulas.append({
            "name": "Brier Score",
            "latex": r"BS = \frac{1}{N}\sum_{i=1}^{N}(f_i - o_i)^2",
            "description": "Mean squared error of probabilistic forecasts. $f_i$=forecast probability, $o_i$=outcome (0 or 1). Lower is better. $BS=0$ is perfect.",
        })
        formulas.append({
            "name": "Calibration Curve",
            "latex": r"\text{calibration}(p) = P(\text{event} \mid \text{forecast} = p)",
            "description": "Perfect calibration: $P(\\text{event} \\mid f=p) = p$ for all $p \\in [0,1]$.",
        })

    if sid in {"gtm-sales", "pricing-finance", "ai-business-models"}:
        formulas.append({
            "name": "CAC / LTV Ratio",
            "latex": r"\text{health} = \frac{LTV}{CAC} \geq 3",
            "description": "Lifetime Value must exceed Customer Acquisition Cost by 3x for sustainable SaaS. $LTV = ARPU \\times \\text{gross margin} \\times \\text{lifetime}$.",
        })
        formulas.append({
            "name": "Pipeline Coverage",
            "latex": r"C = \frac{\sum_{i} v_i \cdot p_i}{\text{quota}}",
            "description": "Weighted pipeline $C$ must be $\\geq 3.0\\times$ quota for 80% confidence of hitting target.",
        })

    if sid in {"knowledge-memory"}:
        formulas.append({
            "name": "RAG Recall@K",
            "latex": r"\text{Recall}@K = \frac{|\text{relevant} \cap \text{retrieved}_K|}{|\text{relevant}|}",
            "description": "Fraction of relevant documents in top-$K$ retrieved. Target: $\\text{Recall}@10 \\geq 0.85$ for production RAG.",
        })

    if sid in {"scraping-intelligence"}:
        formulas.append({
            "name": "Evidence Independence",
            "latex": r"I_{src} = |\{domain(u) : u \in U_{card}\}|",
            "description": "Count of distinct domains in card source URLs. $I_{src} \\geq 3$ for promotion eligibility.",
        })

    if sid in {"automation-runtime"}:
        formulas.append({
            "name": "Workflow Reliability",
            "latex": r"R_{wf} = \prod_{i=1}^{n} (1 - p_i^{fail})",
            "description": "End-to-end reliability of $n$-step workflow. Even $p_i=0.99$ across 50 steps gives $R \\approx 0.60$.",
        })

    # general: information gain / entropy if claim mentions probability or information
    if any(w in claim or w in blob.lower() for w in ["entropy", "information", "probability", "uncertainty", "mutual information"]):
        formulas.append({
            "name": "Shannon Entropy",
            "latex": r"H(X) = -\sum_{x} p(x) \log_2 p(x)",
            "description": "Information-theoretic uncertainty of random variable $X$. Higher $H$ = more uncertainty.",
        })

    return formulas


# ---------------------------------------------------------------------------
# SVG image generation (deterministic concept diagram)
# ---------------------------------------------------------------------------


def generate_svg_image(card: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Generate a deterministic SVG concept diagram for the card."""
    title = str(card.get("title") or "Build Card")[:60]
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    tier = (card.get("strategy") or {}).get("tier") or "na"
    rank = (card.get("score") or {}).get("rank") or "?"
    total = int((card.get("score") or {}).get("total") or 0)
    score_breakdown = (card.get("score") or {}).get("breakdown") or {}
    source_count = int((card.get("sources") or {}).get("count") or 0)
    consensus_count = int((card.get("consensus") or {}).get("count") or 0)
    claim = str(card.get("claim") or "")[:100]

    # color by tier
    colors = {
        "T0": ("#1a1a2e", "#16213e", "#0f3460", "#e94560"),
        "T1": ("#0d1b2a", "#1b263b", "#415a77", "#778da9"),
        "T2": ("#1a1a1a", "#2d2d2d", "#404040", "#808080"),
    }
    bg, panel, accent, highlight = colors.get(str(tier), colors["T2"])

    # score dimensions for radar
    dims = [
        ("Multi-Source", (score_breakdown.get("multi_source") or {}).get("score", 0), 18),
        ("Mechanism", (score_breakdown.get("mechanism_density") or {}).get("score", 0), 18),
        ("Evidence", (score_breakdown.get("evidence_anchoring") or {}).get("score", 0), 14),
        ("TAC", (score_breakdown.get("tac_structure") or {}).get("score", 0), 12),
        ("Doctrine", (score_breakdown.get("doctrine_fit") or {}).get("score", 0), 12),
        ("Novelty", (score_breakdown.get("novelty_pattern") or {}).get("score", 0), 10),
        ("Action", (score_breakdown.get("actionability") or {}).get("score", 0), 8),
        ("GEV", (score_breakdown.get("gev_separation") or {}).get("score", 0), 8),
    ]

    # radar polygon points
    cx, cy, max_r = 250, 270, 130
    n = len(dims)
    radar_pts: list[str] = []
    for i, (_, score_val, max_val) in enumerate(dims):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        r = (score_val / max_val if max_val else 0) * max_r
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        radar_pts.append(f"{x:.1f},{y:.1f}")

    # axis labels
    axis_labels = ""
    for i, (label, _, _) in enumerate(dims):
        angle = -math.pi / 2 + 2 * math.pi * i / n
        lx = cx + (max_r + 25) * math.cos(angle)
        ly = cy + (max_r + 25) * math.sin(angle)
        axis_labels += f'<text x="{lx:.1f}" y="{ly:.1f}" fill="#888" font-size="10" text-anchor="middle">{label}</text>\n'

    # confidence color
    conf = analysis.get("confidence", "MEDIUM")
    conf_color = {"HIGH": "#00ff88", "MEDIUM-HIGH": "#88ff00", "MEDIUM": "#ffaa00", "LOW": "#ff4444"}.get(conf, "#ffaa00")

    # math formula preview
    math_preview = ""
    formulas = card.get("math") or []
    if formulas:
        first = formulas[0]
        formula_text = str(first.get("name") or "")[:30]
        math_preview = f'<text x="50" y="540" fill="{highlight}" font-size="11" font-family="monospace">Formula: {formula_text}</text>'

    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 600" width="500" height="600">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" style="stop-color:{bg}"/>
      <stop offset="100%" style="stop-color:{panel}"/>
    </linearGradient>
  </defs>
  <rect width="500" height="600" fill="url(#bg)"/>

  <!-- Header -->
  <rect x="20" y="15" width="460" height="55" rx="8" fill="{panel}" opacity="0.8"/>
  <text x="30" y="38" fill="{highlight}" font-size="13" font-weight="bold">RIG OmniScout L2 Build Card</text>
  <text x="30" y="58" fill="#ccc" font-size="11">{title}</text>

  <!-- Strategy badge -->
  <rect x="350" y="20" width="120" height="22" rx="11" fill="{accent}" opacity="0.6"/>
  <text x="410" y="35" fill="#fff" font-size="10" text-anchor="middle">{tier} · {sid}</text>

  <!-- Rank badge -->
  <rect x="350" y="46" width="120" height="22" rx="11" fill="{highlight}" opacity="0.8"/>
  <text x="410" y="61" fill="#000" font-size="11" font-weight="bold" text-anchor="middle">{rank} · {total}/100</text>

  <!-- Confidence badge -->
  <rect x="20" y="80" width="460" height="24" rx="4" fill="{conf_color}" opacity="0.15"/>
  <circle cx="35" cy="92" r="5" fill="{conf_color}"/>
  <text x="48" y="96" fill="{conf_color}" font-size="11" font-weight="bold">CONFIDENCE: {conf}</text>
  <text x="250" y="96" fill="#888" font-size="10">Sources: {source_count} · Consensus: {consensus_count} papers</text>

  <!-- Score Radar -->
  <g transform="translate(0,10)">
    {axis_labels}
    <!-- grid circles -->
    <circle cx="{cx}" cy="{cy}" r="{max_r*0.25:.0f}" fill="none" stroke="#333" stroke-width="0.5"/>
    <circle cx="{cx}" cy="{cy}" r="{max_r*0.5:.0f}" fill="none" stroke="#333" stroke-width="0.5"/>
    <circle cx="{cx}" cy="{cy}" r="{max_r*0.75:.0f}" fill="none" stroke="#333" stroke-width="0.5"/>
    <circle cx="{cx}" cy="{cy}" r="{max_r}" fill="none" stroke="#444" stroke-width="1"/>
    <!-- axes -->
    {''.join(f'<line x1="{cx}" y1="{cy}" x2="{cx+max_r*math.cos(-math.pi/2+2*math.pi*i/n):.1f}" y2="{cy+max_r*math.sin(-math.pi/2+2*math.pi*i/n):.1f}" stroke="#333" stroke-width="0.5"/>' for i in range(n))}
    <!-- score polygon -->
    <polygon points="{' '.join(radar_pts)}" fill="{highlight}" fill-opacity="0.2" stroke="{highlight}" stroke-width="2"/>
    <!-- center dot -->
    <circle cx="{cx}" cy="{cy}" r="3" fill="{highlight}"/>
  </g>

  <!-- Claim -->
  <rect x="20" y="430" width="460" height="50" rx="6" fill="{panel}" opacity="0.6"/>
  <text x="30" y="450" fill="{highlight}" font-size="10" font-weight="bold">CLAIM</text>
  <text x="30" y="468" fill="#ddd" font-size="10">{claim}</text>

  <!-- Direction -->
  <rect x="20" y="490" width="460" height="40" rx="6" fill="{accent}" opacity="0.3"/>
  <text x="30" y="508" fill="{highlight}" font-size="10" font-weight="bold">DIRECTION</text>
  <text x="30" y="522" fill="#ccc" font-size="9">Route: {analysis.get('evidence_quality',{}).get('verdict','')} → {('doctrine-candidate' if rank in ('STRONG','EXCELLENT') else 'strengthen-evidence')}</text>

  {math_preview}

  <!-- Footer -->
  <text x="250" y="585" fill="#555" font-size="8" text-anchor="middle">rig.omniscout.build-card.v2 · {card.get('card_id','')}</text>
</svg>"""

    image_dir = L2_ROOT / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    svg_path = image_dir / f"{card.get('card_id')}.svg"
    svg_path.write_text(svg, encoding="utf-8")

    return {
        "type": "svg",
        "path": str(svg_path),
        "url": f"rig://omniscout/l2/{card.get('card_id')}/image.svg",
        "description": f"Deterministic concept diagram for '{title[:40]}' — score radar + confidence + claim + direction",
    }


# ---------------------------------------------------------------------------
# Enrich single card
# ---------------------------------------------------------------------------


def enrich_card(card_path: Path) -> dict[str, Any]:
    """Upgrade a v1 card to v2 with analysis, direction, prompts, math, tags, image."""
    card = json.loads(card_path.read_text(encoding="utf-8"))

    # skip if already v2
    if card.get("schema") == SCHEMA_V2:
        return {"ok": True, "card_id": card.get("card_id"), "status": "already_v2", "path": str(card_path)}

    analysis = generate_analysis(card)
    direction = generate_direction(card, analysis)
    prompts = generate_prompts(card)
    math_formulas = generate_math(card, analysis)
    tags = generate_tags(card)

    # upgrade card
    card["schema"] = SCHEMA_V2
    card["analysis"] = analysis
    card["direction"] = direction
    card["prompts"] = prompts
    card["math"] = math_formulas
    card["tags"] = tags
    card["enriched_at"] = utc_now()

    # generate image
    image = generate_svg_image(card, analysis)
    card["image"] = image

    # re-score (v2 has more fields, score may improve)
    new_score = score_build_card(card)
    card["score"] = new_score
    card["artifact_sha256"] = sha256_text(stable_json(card))

    # write back
    atomic_json(card_path, card)

    # regenerate markdown
    md = _card_v2_to_markdown(card)
    atomic_text(card_path.with_suffix(".md"), md)

    return {
        "ok": True,
        "card_id": card.get("card_id"),
        "status": "enriched_to_v2",
        "path": str(card_path),
        "score": new_score.get("total"),
        "rank": new_score.get("rank"),
        "tags_count": len(tags),
        "prompts_count": len(prompts),
        "math_count": len(math_formulas),
        "has_image": True,
        "has_analysis": True,
        "has_direction": True,
    }


def _card_v2_to_markdown(card: dict[str, Any]) -> str:
    """Full v2 markdown with all sections."""
    score = card.get("score") or {}
    analysis = card.get("analysis") or {}
    direction = card.get("direction") or {}
    prompts = card.get("prompts") or []
    math_formulas = card.get("math") or []
    tags = card.get("tags") or []
    image = card.get("image") or {}

    lines = [
        f"# {card.get('title')}",
        "",
        f"- **card_id:** `{card.get('card_id')}`",
        f"- **schema:** `{card.get('schema')}`",
        f"- **rank:** **{score.get('rank')}** ({score.get('total')}/100)",
        f"- **topic:** {card.get('topic')}",
        f"- **strategy:** `{(card.get('strategy') or {}).get('strategy_id')}` ({(card.get('strategy') or {}).get('tier')})",
        f"- **sources:** {(card.get('sources') or {}).get('count')}",
        f"- **consensus:** {(card.get('consensus') or {}).get('via')} n={(card.get('consensus') or {}).get('count')}",
        f"- **promote:** {score.get('promote')}",
        f"- **created:** {card.get('created_at')}",
        f"- **enriched:** {card.get('enriched_at')}",
        "",
    ]

    # Tags
    if tags:
        lines += ["## Tags", ""]
        for t in tags:
            lines.append(f"`{t}`")
        lines.append("")

    # Image reference
    if image.get("path"):
        lines += ["## Concept Diagram", "", f"![diagram]({image['path']})", ""]

    # Claim
    lines += ["## Claim", str(card.get("claim") or ""), ""]

    # Summary
    lines += ["## Summary", str(card.get("summary") or ""), ""]

    # Mechanism
    lines += ["## Mechanism", str(card.get("mechanism") or ""), ""]

    # Analysis
    if analysis:
        lines += [
            "## Analysis",
            "",
            f"**Confidence:** {analysis.get('confidence')} — {analysis.get('confidence_rationale')}",
            "",
            "### Evidence Quality",
            f"- Evidence URLs: {analysis.get('evidence_quality', {}).get('evidence_urls', 0)}",
            f"- Evidence with numbers: {analysis.get('evidence_quality', {}).get('evidence_with_numbers', 0)}",
            f"- Consensus papers: {analysis.get('evidence_quality', {}).get('consensus_papers', 0)}",
            f"- Source count: {analysis.get('evidence_quality', {}).get('source_count', 0)}",
            f"- Verdict: **{analysis.get('evidence_quality', {}).get('verdict', 'unknown')}**",
            "",
            "### Mechanism Depth",
            f"- Words: {analysis.get('mechanism_depth', {}).get('word_count', 0)}",
            f"- Sentences: {analysis.get('mechanism_depth', {}).get('sentence_count', 0)}",
            f"- Has steps: {analysis.get('mechanism_depth', {}).get('has_steps', False)}",
            f"- Verdict: **{analysis.get('mechanism_depth', {}).get('verdict', 'unknown')}**",
            "",
            "### Tradeoffs",
        ]
        for t in analysis.get("tradeoffs") or []:
            lines.append(f"- {t}")
        lines += ["", "### Gaps", ""]
        for g in analysis.get("gaps") or []:
            lines.append(f"- {g}")
        lines += [
            "",
            "### Score Decomposition",
            f"- Multi-source: {analysis.get('score_decomposition', {}).get('multi_source', '?')}",
            f"- Mechanism: {analysis.get('score_decomposition', {}).get('mechanism', '?')}",
            f"- Evidence: {analysis.get('score_decomposition', {}).get('evidence', '?')}",
            f"- TAC: {analysis.get('score_decomposition', {}).get('tac_structure', '?')}",
            f"- Total: {analysis.get('score_decomposition', {}).get('total', '?')}",
            "",
        ]

    # Direction
    if direction:
        lines += [
            "## Direction",
            "",
            f"**Leads to:** {direction.get('leads_to', '')}",
            "",
            "### Strategic Implications",
        ]
        for imp in direction.get("strategic_implications") or []:
            lines.append(f"- {imp}")
        lines += ["", "### Build Next", ""]
        for bn in direction.get("build_next") or []:
            lines.append(f"- {bn}")
        lines += [
            "",
            f"**Risk direction:** {direction.get('risk_direction', '')}",
            "",
            "### Doctrine Promotion Path",
            f"- Current rank: {direction.get('doctrine_promotion_path', {}).get('current_rank', '?')}",
            f"- Eligible for v1: {direction.get('doctrine_promotion_path', {}).get('eligible_for_v1', False)}",
            f"- Next step: {direction.get('doctrine_promotion_path', {}).get('next_step', '')}",
            "",
            "### Agent Routing",
            f"- Department: `{direction.get('agent_routing', {}).get('department', '')}`",
            f"- Action: {direction.get('agent_routing', {}).get('action', '')}",
            "",
        ]

    # Pattern
    lines += [
        "## Pattern",
        f"```json\n{json.dumps(card.get('pattern') or {}, indent=2)}\n```",
        "",
    ]

    # Idea
    lines += [
        "## Idea / Build slice",
        f"```json\n{json.dumps(card.get('idea') or {}, indent=2)}\n```",
        "",
    ]

    # TAC
    lines += [
        "## TAC",
        f"```json\n{json.dumps(card.get('tac') or {}, indent=2)}\n```",
        "",
    ]

    # Math
    if math_formulas:
        lines += ["## Mathematical Formulas", ""]
        for f in math_formulas:
            lines.append(f"### {f['name']}")
            lines.append(f"$$")
            lines.append(f["latex"])
            lines.append(f"$$")
            lines.append(f"*{f['description']}*")
            lines.append("")

    # Prompts
    if prompts:
        lines += ["## Prompts", ""]
        for pr in prompts:
            lines.append(f"### {pr['name']} ({pr['type']})")
            lines.append(f"```")
            lines.append(pr["prompt"])
            lines.append(f"```")
            lines.append("")

    # Score breakdown
    lines += [
        "## Score breakdown",
        f"```json\n{json.dumps(score.get('breakdown') or {}, indent=2)}\n```",
        "",
    ]

    # Evidence
    lines += ["## Evidence", ""]
    for e in card.get("evidence") or []:
        if isinstance(e, dict):
            lines.append(f"- {e.get('url')}: {e.get('quote_or_fact')}")
    lines += ["", "## Sources", *[f"- {u}" for u in (card.get("sources") or {}).get("urls") or []], ""]

    # Next actions
    lines += ["## Next actions", *[f"- {a}" for a in card.get("next_actions") or []], ""]

    # Hard blocks
    lines += ["## Hard blocks", *([f"- {b}" for b in score.get("hard_blocks") or []] or ["- none"]), ""]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Batch enrichment
# ---------------------------------------------------------------------------


def enrich_all() -> dict[str, Any]:
    """Enrich every v1 card in L2_CARDS to v2."""
    ensure_dirs()
    started = utc_now()
    t0 = time.time()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    cards = sorted(L2_CARDS.glob("l2-*.json"))
    for path in cards:
        try:
            results.append(enrich_card(path))
        except Exception as exc:  # noqa: BLE001
            errors.append({"card_id": path.stem, "error": str(exc)[:300]})

    summary = {
        "schema": "rig.omniscout.l2-enrichment.v1",
        "ok": len(errors) == 0,
        "started_at": started,
        "finished_at": utc_now(),
        "elapsed_s": round(time.time() - t0, 2),
        "total_cards": len(cards),
        "enriched": sum(1 for r in results if r.get("status") == "enriched_to_v2"),
        "already_v2": sum(1 for r in results if r.get("status") == "already_v2"),
        "errors": errors,
        "at": utc_now(),
    }
    (L2_ROOT / "latest-enrichment.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def ensure_dirs() -> None:
    L2_CARDS.mkdir(parents=True, exist_ok=True)
    (L2_ROOT / "images").mkdir(parents=True, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Enrich L2 build cards to v2")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("all", help="enrich all cards")
    p_one = sub.add_parser("one", help="enrich one card")
    p_one.add_argument("path")
    sub.add_parser("status", help="show enrichment status")
    args = parser.parse_args(argv)

    if args.cmd == "all":
        out = enrich_all()
    elif args.cmd == "one":
        out = enrich_card(Path(args.path))
    elif args.cmd == "status":
        v1 = sum(
            1
            for p in L2_CARDS.glob("l2-*.json")
            if json.loads(p.read_text()).get("schema") != SCHEMA_V2
        )
        v2 = sum(
            1
            for p in L2_CARDS.glob("l2-*.json")
            if json.loads(p.read_text()).get("schema") == SCHEMA_V2
        )
        out = {"v1": v1, "v2": v2, "total": v1 + v2}
    else:
        parser.error("unknown")
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
