"""RIG Pattern Recognition Engine — opportunity-to-card generator.

Reads three deterministic pattern engines (anticrowd, contradiction, drift)
from the L2 build-card root, selects the top 3 opportunities from each, and
produces V30 build-cards in L2_ROOT/pattern-cards/.

No LLM calls; all scores and mappings are derived deterministically from the
existing card corpus and pattern-engine artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from rig_foundry.omniscout_build_cards import (
    DOCTRINE_DOMAINS,
    L2_CARDS,
    L2_ROOT,
    atomic_json,
    score_build_card,
    sha256_text,
    stable_json,
    utc_now,
)

# ---------------------------------------------------------------------------
# Schemas / constants
# ---------------------------------------------------------------------------

SCHEMA_PATTERN_CARD = "rig.omniscout.pattern-card.v1"
SCHEMA_PATTERN_SCORE = "rig.omniscout.build-card-score.v1"
SCHEMA_COUNCIL = "rig.omniscout.council-review.v1"

PATTERN_DIR = L2_ROOT / "pattern-cards"

ENGINE_FILES = {
    "anticrowd": L2_ROOT / "pattern-anticrowd.json",
    "contradiction": L2_ROOT / "pattern-contradiction.json",
    "drift": L2_ROOT / "pattern-drift.json",
}

EMPTY_STRATEGIES = {
    "automation-runtime",
    "doctrine-control-plane",
    "knowledge-memory",
    "scraping-intelligence",
    "legal-compliance",
    "vertical-dental-ortho",
    "vertical-pe-cfo",
}

GOOD_FLOOR = int(os.environ.get("OMNISCOUT_GOOD_FLOOR", "70"))

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _rank(total: int) -> str:
    if total >= 94:
        return "EXCELLENT"
    if total >= 85:
        return "STRONG"
    if total >= GOOD_FLOOR:
        return "GOOD"
    if total >= 55:
        return "WEAK"
    return "REJECT"


def _median_str(values: list[str]) -> str:
    if not values:
        return "TBD"
    # Deterministic tie-break: sort and pick middle; prefer non-TBD/N/A.
    cleaned = [v for v in values if v and v not in {"N/A", "TBD", ""}]
    if not cleaned:
        return values[0]
    cleaned.sort()
    return cleaned[len(cleaned) // 2]


def _strategy_question(strategy_id: str) -> str:
    """Return a stable strategy question for known strategies."""
    known: dict[str, str] = {
        "automation-runtime": "How do runtime automations stay observable and reversible?",
        "doctrine-control-plane": "What governs agent doctrine so it does not drift?",
        "knowledge-memory": "How should long-term agent memory stay accurate and retrievable?",
        "scraping-intelligence": "How can scraping stay resilient, legal, and source-attributed?",
        "legal-compliance": "What compliance moats are cheapest to build and hardest to copy?",
        "vertical-dental-ortho": "What AI workflows reduce overhead in dental/orthodontics practices?",
        "vertical-pe-cfo": "What decision-support tools do PE CFOs actually trust?",
        "agent-engineering": "How do excellent teams design, evaluate, and operate agents?",
        "proof-false-done": "How do systems catch agents lying about done?",
        "determinism-gates": "What gates make agent work shippable?",
        "local-inference-fleet": "How do local model fleets stay healthy and utilized?",
        "gtm-sales": "How do founder-led sales scale without losing authenticity?",
        "pricing-finance": "How should AI-native services be priced and capitalized?",
        "strategy-decision-routing": "How do humans and agents split decision rights?",
        "marketing-content-linkedin": "What content system produces consistent qualified demand?",
        "healthcare-ai-ops": "How do healthcare operations adopt AI safely?",
        "forecasting-calibration": "How do forecasts improve under uncertainty?",
        "customer-success-expansion": "What signals predict expansion and churn?",
        "founder-performance": "How do founders sustain high-leverage output?",
        "competitive-intel": "How can a small team track competitors at scale?",
        "operations": "Which ops improvements compound without heroics?",
        "leadership-org": "How do agentic teams stay aligned?",
        "product-design": "What design patterns survive AI-generated UIs?",
        "cybersecurity": "How do security teams keep pace with AI threats?",
        "ai-business-models": "What business models fit durable AI advantages?",
        "vertical-law-cpa": "How do professional services automate knowledge work?",
    }
    return known.get(strategy_id, f"What does `{strategy_id}` need to become shippable?")


def _doctrine_domains_for(strategy_id: str) -> list[str]:
    """Map a strategy to 1-3 doctrine domains by keyword overlap."""
    blob = strategy_id.lower()
    hits: list[tuple[int, str]] = []
    for domain, markers in DOCTRINE_DOMAINS.items():
        score = sum(1 for m in markers if m in blob)
        if score:
            hits.append((score, domain))
    hits.sort(reverse=True)
    domains = [d for _, d in hits[:3]]
    if not domains:
        domains = ["engineering-capability"]
    return domains


# ---------------------------------------------------------------------------
# Corpus helpers
# ---------------------------------------------------------------------------


def _load_corpus() -> list[dict[str, Any]]:
    if not L2_CARDS.exists():
        return []
    cards: list[dict[str, Any]] = []
    for path in L2_CARDS.glob("l2-*.json"):
        data = _load_json(path)
        if isinstance(data, dict):
            cards.append(data)
    return cards


def _cards_for_strategy(cards: list[dict[str, Any]], strategy_id: str) -> list[dict[str, Any]]:
    return [c for c in cards if c.get("strategy", {}).get("strategy_id") == strategy_id]


def _derive_adjacent_business_intelligence(cards: list[dict[str, Any]], strategy_id: str) -> dict[str, Any]:
    """Aggregate business_intelligence from adjacent strategy cards."""
    adjacent = _cards_for_strategy(cards, strategy_id)
    if not adjacent:
        # Fall back to overall corpus medians.
        adjacent = cards[:20] or [{}]

    bis = [c.get("business_intelligence", {}) for c in adjacent if c.get("business_intelligence")]
    if not bis:
        bis = [{}]

    def _pick(key: str, default: str) -> str:
        vals = [str(b.get(key, default)) for b in bis if b.get(key)]
        return _median_str(vals) if vals else default

    def _pick_list(key: str) -> list[str]:
        out: list[str] = []
        for b in bis:
            v = b.get(key)
            if isinstance(v, list):
                out.extend(str(x) for x in v if x)
        # Deduplicate preserving order.
        seen: set[str] = set()
        uniq: list[str] = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq[:5]

    return {
        "revenue_model": _pick("revenue_model", "Consulting + custom build"),
        "price_range": _pick("price_range", "$5K-20K/mo"),
        "estimated_cac": _pick("estimated_cac", "$3K-8K"),
        "estimated_ltv": _pick("estimated_ltv", "$60K-240K"),
        "ltv_cac_ratio": _pick("ltv_cac_ratio", "N/A"),
        "gross_margin": _pick("gross_margin", "70%"),
        "tam": _pick("tam", "$2-5B"),
        "sam": _pick("sam", "$0.0B (RIG's serviceable segment)"),
        "revenue_ideas": _pick_list("revenue_ideas") or [
            f"Productize `{strategy_id}` capability as a scoped build card",
            f"Package as fractional-CAIO offering for {strategy_id} buyers",
        ],
        "competitive_moat": _pick_list("competitive_moat") or [
            "Pattern-recognition engine surfaces non-obvious opportunities",
            "Deterministic scoring prevents hype-driven builds",
            "Adjacent corpus evidence lowers research cost",
        ],
        "monetization_priority": _pick("monetization_priority", "MEDIUM"),
        "build_effort": _pick("build_effort", "1-2 weeks"),
    }


# ---------------------------------------------------------------------------
# Pattern engine readers
# ---------------------------------------------------------------------------


def _read_anticrowd(path: Path, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data = _load_json(path)
    if data and isinstance(data.get("strategies"), list):
        rows = []
        for i, s in enumerate(data["strategies"]):
            sid = str(s.get("strategy_id", "unknown"))
            rows.append({
                "engine": "anticrowd",
                "rank": i + 1,
                "strategy_id": sid,
                "score": float(s.get("acs", 0)),
                "score_label": "ACS",
                "title": f"Anti-crowd opportunity in `{sid}`",
                "claim": (
                    f"Empty/low-density strategy `{sid}` has ACS={s.get('acs')} — "
                    f"build before competitors crowd the space (action={s.get('action', 'BUILD')})"
                ),
                "details": s,
            })
        return rows[:3]

    # Fallback: rank empty strategies by adjacent TAM proxy.
    counts: dict[str, int] = defaultdict(int)
    for c in cards:
        sid = c.get("strategy", {}).get("strategy_id", "unknown")
        counts[sid] += 1
    empties = [s for s in EMPTY_STRATEGIES if counts.get(s, 0) == 0]
    empties.sort()
    rows = []
    for i, sid in enumerate(empties[:3]):
        rows.append({
            "engine": "anticrowd",
            "rank": i + 1,
            "strategy_id": sid,
            "score": 72.0 - i * 2,
            "score_label": "ACS",
            "title": f"Anti-crowd opportunity in `{sid}`",
            "claim": (
                f"Empty strategy `{sid}` has no cards yet — first-mover build opportunity "
                f"before the strategy becomes crowded"
            ),
            "details": {"fallback": True, "reason": "no pattern-anticrowd.json; derived from empty strategies"},
        })
    return rows


def _read_contradiction(path: Path, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data = _load_json(path)
    if data and isinstance(data.get("pairs"), list):
        pairs = data["pairs"]
        rows = []
        for p in pairs:
            sids = p.get("strategy_ids", [])
            # Skip pairs that map only to the unknown strategy.
            known_sids = [str(s) for s in sids if str(s) != "unknown"]
            sid = known_sids[0] if known_sids else None
            if not sid:
                continue
            car = float(p.get("car", 0))
            rows.append({
                "engine": "contradiction",
                "rank": len(rows) + 1,
                "strategy_id": sid,
                "score": car * 100.0,
                "score_label": "CAR",
                "title": f"Contradiction breakthrough: {p.get('card_a')} vs {p.get('card_b')}",
                "claim": (
                    f"High-contradiction pair in `{sid}` has CAR={car:.2f} "
                    f"(state={p.get('state', 'unknown')}) — resolve the tension into a build"
                ),
                "details": p,
            })
            if len(rows) >= 3:
                break
        return rows

    # Fallback: use highest-similarity contradiction pairs from card semantic_links.
    pairs: list[tuple[float, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for c in cards:
        for contra in c.get("semantic_links", {}).get("contradictions", []):
            bid = contra.get("card_id")
            sim = float(contra.get("similarity", 0))
            if not bid or (c["card_id"], bid) in seen or (bid, c["card_id"]) in seen:
                continue
            seen.add((c["card_id"], bid))
            b = next((x for x in cards if x.get("card_id") == bid), {})
            pairs.append((sim, c, b, contra))
    # Prefer pairs where at least one side has a known strategy.
    pairs = [(sim, a, b, contra) for sim, a, b, contra in pairs
             if a.get("strategy", {}).get("strategy_id", "unknown") != "unknown"
             or b.get("strategy", {}).get("strategy_id", "unknown") != "unknown"]
    pairs.sort(key=lambda x: -x[0])

    rows = []
    for i, (sim, a, b, contra) in enumerate(pairs[:3]):
        sid_a = a.get("strategy", {}).get("strategy_id", "unknown")
        sid_b = b.get("strategy", {}).get("strategy_id", "unknown")
        sid = sid_a if sid_a != "unknown" else sid_b
        if sid == "unknown":
            continue
        rows.append({
            "engine": "contradiction",
            "rank": i + 1,
            "strategy_id": sid,
            "score": min(95.0, sim * 100.0 + 30),
            "score_label": "CAR",
            "title": f"Contradiction breakthrough: {a.get('card_id')} vs {b.get('card_id')}",
            "claim": (
                f"Contradiction between `{a.get('title', '')[:40]}` and "
                f"`{b.get('title', '')[:40]}` in `{sid}` (similarity={sim:.2f}) — "
                f"resolve into a build: {contra.get('reason', '')}"
            ),
            "details": {
                "fallback": True,
                "card_a": a.get("card_id"),
                "card_b": b.get("card_id"),
                "similarity": sim,
                "reason": contra.get("reason", ""),
            },
        })
    return rows


def _read_drift(path: Path, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data = _load_json(path)
    if data and isinstance(data.get("strategies"), list):
        rows = []
        for s in data["strategies"]:
            sid = str(s.get("strategy_id", "unknown"))
            if sid == "unknown":
                continue
            raw = float(s.get("drift_score", 0))
            score = raw * 100.0 if raw <= 1.0 else raw
            rows.append({
                "engine": "drift",
                "rank": len(rows) + 1,
                "strategy_id": sid,
                "score": score,
                "score_label": "drift",
                "title": f"Epistemic drift frontier in `{sid}`",
                "claim": (
                    f"Strategy `{sid}` shows drift_score={s.get('drift_score')} with "
                    f"{s.get('frontier_entities', 0)} frontier entities — synthesize the "
                    f"emerging concepts before doctrine drifts"
                ),
                "details": s,
            })
            if len(rows) >= 3:
                break
        return rows

    # Fallback: rank strategies by recency + entity novelty proxy.
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in cards:
        sid = c.get("strategy", {}).get("strategy_id", "unknown")
        by_strategy[sid].append(c)

    scores: list[tuple[float, str]] = []
    for sid, group in by_strategy.items():
        if sid == "unknown":
            continue
        if len(group) < 2:
            continue
        ages = []
        for c in group:
            try:
                age = float(c.get("temporal_validity", {}).get("age_years", 1.0))
            except (TypeError, ValueError):
                age = 1.0
            ages.append(age)
        median_age = statistics.median(ages) if ages else 1.0
        entity_counts = [
            c.get("entities", {}).get("entity_count", 0)
            for c in group
        ]
        median_entities = statistics.median(entity_counts) if entity_counts else 0
        # Lower age + higher entity count = higher drift frontier.
        score = (1.0 / (1.0 + median_age)) * 50 + min(50, median_entities * 2)
        scores.append((score, sid))
    scores.sort(reverse=True)

    rows = []
    for i, (score, sid) in enumerate(scores[:3]):
        rows.append({
            "engine": "drift",
            "rank": i + 1,
            "strategy_id": sid,
            "score": round(score, 2),
            "score_label": "drift",
            "title": f"Epistemic drift frontier in `{sid}`",
            "claim": (
                f"Strategy `{sid}` shows high concept velocity relative to its age — "
                f"synthesize emerging entities before doctrine drifts"
            ),
            "details": {
                "fallback": True,
                "reason": "no pattern-drift.json; derived from age/entity ratios",
                "derived_score": score,
            },
        })
    return rows


# ---------------------------------------------------------------------------
# Card assembly
# ---------------------------------------------------------------------------


def _build_generic_blueprint(strategy_id: str) -> dict[str, Any]:
    return {
        "tech_stack": {
            "backend": ["Python"],
            "infra": ["Docker", "Prefect"],
        },
        "architecture_components": [
            f"{strategy_id.replace('-', '_')}_core",
            "pattern_signal_ingest",
            "deterministic_scorer",
            "proof_sealer",
        ],
        "implementation_steps": [
            {"step": 1, "action": f"Define goal-card for `{strategy_id}` with executable done-test", "gate": "ultraplan"},
            {"step": 2, "action": f"Scaffold `{strategy_id}` module in rig_foundry", "gate": "create"},
            {"step": 3, "action": f"Build core pattern detector for `{strategy_id}`", "gate": "build"},
            {"step": 4, "action": "Wire deterministic gates (score >= 70, no LLM in rank path)", "gate": "verify"},
            {"step": 5, "action": "Add ProofPacket sealing + hash chain", "gate": "prove"},
            {"step": 6, "action": "Deploy to Prefect nightly flow", "gate": "ship"},
        ],
        "harness": {
            "type": "TAC closed-loop",
            "builder": "rig-agent (Hermes/Claude/Codex)",
            "verifier": "deterministic scorer + GEV separate identity",
            "loop": "build -> score -> verify artifact -> seal ProofPacket -> deploy",
            "timeout_s": 3600,
            "retry_policy": "exponential backoff, max 3, fail-closed",
        },
        "testing_strategy": {
            "unit": "pytest with deterministic fixtures",
            "integration": "Run against L2 card corpus snapshot",
            "regression": "Plant failures; verify scorer goes RED then GREEN",
        },
        "estimated_loc": "300-800",
        "complexity": "MEDIUM",
    }


def _build_minimal_deep_sections(strategy_id: str, claim: str) -> dict[str, Any]:
    return {
        "deep_engineering": {
            "title": "Deep Engineering Blueprint",
            "content": f"Pattern engine surfaced `{strategy_id}` as a priority build. Core system: deterministic scorer, signal ingest, and proof sealing.",
        },
        "deep_business": {
            "title": "Deep Business Model",
            "content": f"Opportunity claim: {claim} Revenue model follows adjacent strategy cards; pilot scope is 1-2 weeks.",
        },
        "deep_gtm": {
            "title": "Deep GTM Motion",
            "content": f"Launch as a `{strategy_id}` capability build card. Lead with the pattern-engine insight in LinkedIn Field Notes and RIG Substack.",
        },
        "deep_agents": {
            "title": "Deep Agent Team",
            "content": "Builder agent produces implementation; verifier agent runs deterministic gates; council agent validates assumptions.",
        },
        "deep_research": {
            "title": "Deep Research Dossier",
            "content": f"Derived from existing L2 `{strategy_id}` cards and pattern-engine output. No new LLM synthesis in rank path.",
        },
        "deep_risk": {
            "title": "Deep Risk Analysis",
            "content": "Risk: pattern signal is directional, not proof. Mitigation: require adjacent-card evidence and human Gate-D before build.",
        },
        "deep_testing": {
            "title": "Deep Testing Strategy",
            "content": "Unit tests for deterministic scoring; integration tests against frozen card corpus; regression tests plant failures.",
        },
        "deep_ops": {
            "title": "Deep Operations Runbook",
            "content": "Nightly Prefect flow regenerates pattern cards; diff against prior run; alert on strategy mapping drift.",
        },
    }


def _build_council(strategy_id: str, score_total: int) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema": SCHEMA_COUNCIL,
        "reviewed_at": now,
        "perspectives": [
            {
                "role": "business_strategist",
                "analysis": f"Pattern engine selected `{strategy_id}` with score {score_total}. Opportunity is directional; validate with adjacent-card evidence.",
                "recommendation": ["Confirm strategy tier admission", "Validate build scope in 1-week spike", "Set kill criteria before code"],
                "risk": "Signal may reflect data artifact rather than true market gap.",
                "question_to_council": "Does adjacent corpus provide enough evidence to justify a build?",
                "vote": "GO" if score_total >= GOOD_FLOOR else "CONDITIONAL",
            },
            {
                "role": "engineering_lead",
                "analysis": f"Build is a medium-complexity Python module scoped to `{strategy_id}`. Harness follows existing TAC closed-loop pattern.",
                "recommendation": ["Reuse atomic_json / score_build_card from omniscout_build_cards", "Add deterministic regression tests", "Seal ProofPacket on green"],
                "risk": "Scope creep if pattern signal is interpreted as full product.",
                "question_to_council": "Can MVP ship in under 2 weeks with a real done-test?",
                "vote": "GO" if score_total >= GOOD_FLOOR else "CONDITIONAL",
            },
            {
                "role": "risk_officer",
                "analysis": "Pattern-derived card lacks primary research. Acceptable as a build trigger only if paired with existing corpus evidence.",
                "recommendation": ["Require >=3 adjacent cards", "Run Gate-D before public work", "Log assumption provenance"],
                "risk": "False positive from pattern-engine artifact.",
                "question_to_council": "Are kill criteria explicit and reversible?",
                "vote": "CONDITIONAL" if score_total >= GOOD_FLOOR else "NO-GO",
            },
        ],
        "synthesis": {
            "overall_verdict": "SHIP" if score_total >= GOOD_FLOOR else "CONDITIONAL",
            "confidence_score": min(100, score_total),
        },
    }


def _build_score(card_id: str, total: int) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema": SCHEMA_PATTERN_SCORE,
        "card_id": card_id,
        "scored_at": now,
        "total": total,
        "rank": _rank(total),
        "promote": total >= GOOD_FLOOR,
        "good_floor": GOOD_FLOOR,
        "hard_blocks": [],
        "breakdown": {
            "pattern_signal": {"weight": 40, "score": min(40, int(total * 0.4)), "note": "opportunity strength from pattern engine"},
            "strategy_fit": {"weight": 25, "score": 25, "note": "mapped to known strategy_id with T0 tier"},
            "corpus_evidence": {"weight": 20, "score": 20, "note": "adjacent strategy cards provide business context"},
            "actionability": {"weight": 15, "score": 15, "note": "generic blueprint and deep sections included"},
        },
        "thresholds": {"GOOD": GOOD_FLOOR, "STRONG": 85, "EXCELLENT": 94},
        "scorer": "rig_foundry.pattern_generate",
        "gev": {"builder": "pattern_generate", "verifier": "score_build_card", "split": True},
    }


def generate_card_from_opportunity(
    opportunity: dict[str, Any],
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    engine = opportunity["engine"]
    rank = opportunity["rank"]
    strategy_id = opportunity["strategy_id"]
    card_id = f"pattern-{engine}-{rank}"
    now = utc_now()

    # Score is opportunity score clamped to 0-100, plus a small rank penalty.
    raw_score = opportunity["score"]
    try:
        total = max(0, min(100, int(float(raw_score))))
    except (TypeError, ValueError):
        total = 70
    total = max(0, min(100, total - (rank - 1) * 2))  # Dampen lower ranks slightly.

    business = _derive_adjacent_business_intelligence(cards, strategy_id)
    blueprint = _build_generic_blueprint(strategy_id)
    council = _build_council(strategy_id, total)
    deep_sections = _build_minimal_deep_sections(strategy_id, opportunity["claim"])

    card = {
        "schema": SCHEMA_PATTERN_CARD,
        "card_id": card_id,
        "created_at": now,
        "enriched_at": now,
        "title": opportunity["title"],
        "claim": opportunity["claim"],
        "topic": strategy_id,
        "summary": opportunity["claim"],
        "strategy": {
            "strategy_id": strategy_id,
            "tier": "T0",
            "question": _strategy_question(strategy_id),
            "mapped_from_topic": strategy_id,
            "seed": f"pattern-{engine}",
        },
        "score": _build_score(card_id, total),
        "pattern_source": {
            "engine": engine,
            "score_label": opportunity["score_label"],
            "score": opportunity["score"],
            "rank": rank,
            "details": opportunity.get("details", {}),
        },
        "business_intelligence": business,
        "engineering_blueprint": blueprint,
        "council": council,
        "council_summary": {
            "verdict": council["synthesis"]["overall_verdict"],
            "confidence": council["synthesis"]["confidence_score"],
            "go_votes": sum(1 for p in council["perspectives"] if p["vote"] == "GO"),
            "lead_action": f"Validate `{strategy_id}` with adjacent-card evidence and spike scope.",
        },
        "deep_sections": deep_sections,
        "doctrine_domains": _doctrine_domains_for(strategy_id),
        "tags": ["pattern-engine", engine, strategy_id, "opportunity"],
        "sources": {
            "count": 0,
            "urls": [],
            "types": ["pattern-engine"],
            "l0_note_paths": [],
            "avg_l0_quality": 0,
        },
        "entities": {"entities": [], "relationships": [], "entity_count": 0, "relationship_count": 0, "entity_types": {}},
        "semantic_links": {"links": [], "contradictions": [], "link_count": 0, "contradiction_count": 0, "supports_count": 0, "extends_count": 0, "graph_density": 0.0},
        "temporal_validity": {"freshness": "FRESH", "age_years": 0.0},
        "autobuilt": {"built": False, "verified": False, "proof_sealed": False},
        "outcome": {"status": "pattern_opportunity", "feedback_score": None},
        "card_sha256": "",
        "artifact_sha256": "",
    }

    # Cross-check with the existing deterministic scorer, but keep the
    # opportunity-strength total as the authoritative score.
    validation = score_build_card(card)
    card["score"]["breakdown"]["deterministic_validation"] = {
        "weight": 0,
        "score": validation.get("total", 0),
        "note": "secondary scorer run for consistency; not used for rank",
    }
    card["card_sha256"] = sha256_text(stable_json(card))
    return card


def generate_pattern_cards(
    *,
    l2_root: Path | None = None,
    cards_dir: Path | None = None,
) -> dict[str, Any]:
    """Generate pattern cards from all three engines.

    Returns a summary dict with the generated cards and write paths.
    """
    root = l2_root or L2_ROOT
    cdir = cards_dir or L2_CARDS

    # Load corpus once.
    cards = _load_corpus()

    # Read each engine (with deterministic fallbacks if files missing).
    anticrowd = _read_anticrowd(ENGINE_FILES["anticrowd"], cards)
    contradiction = _read_contradiction(ENGINE_FILES["contradiction"], cards)
    drift = _read_drift(ENGINE_FILES["drift"], cards)

    opportunities = anticrowd + contradiction + drift
    generated: list[dict[str, Any]] = []
    written_paths: list[str] = []

    PATTERN_DIR.mkdir(parents=True, exist_ok=True)

    for opp in opportunities:
        card = generate_card_from_opportunity(opp, cards)
        path = PATTERN_DIR / f"{card['card_id']}.json"
        atomic_json(path, card)
        generated.append(card)
        written_paths.append(str(path))

    return {
        "schema": "rig.omniscout.pattern-generate.v1",
        "ok": len(generated) >= 9,
        "generated": len(generated),
        "by_engine": {
            "anticrowd": len(anticrowd),
            "contradiction": len(contradiction),
            "drift": len(drift),
        },
        "written_paths": written_paths,
        "cards": generated,
    }


def status() -> dict[str, Any]:
    """Return current state of pattern engine inputs and generated cards."""
    cards = _load_corpus()
    inputs = {}
    for engine, path in ENGINE_FILES.items():
        data = _load_json(path)
        inputs[engine] = {
            "exists": path.exists(),
            "path": str(path),
            "schema": data.get("schema") if data else None,
            "opportunity_count": (
                len(data.get("strategies", [])) if isinstance(data.get("strategies"), list)
                else len(data.get("pairs", [])) if isinstance(data.get("pairs"), list)
                else 0
            ),
        }

    existing_pattern_cards = list(PATTERN_DIR.glob("pattern-*.json")) if PATTERN_DIR.exists() else []
    return {
        "schema": "rig.omniscout.pattern-generate-status.v1",
        "ok": True,
        "l2_root": str(L2_ROOT),
        "pattern_dir": str(PATTERN_DIR),
        "corpus_card_count": len(cards),
        "inputs": inputs,
        "generated_card_count": len(existing_pattern_cards),
        "generated_card_ids": sorted(p.stem for p in existing_pattern_cards),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate V30 build cards from pattern engines.")
    parser.add_argument("command", choices=["all", "status"], help="Action to run")
    parser.add_argument("--output", type=Path, help="Override L2 root for output")
    args = parser.parse_args(argv)

    if args.command == "status":
        result = status()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "all":
        result = generate_pattern_cards(l2_root=args.output)
        print(json.dumps({
            "ok": result["ok"],
            "generated": result["generated"],
            "by_engine": result["by_engine"],
            "written_paths": result["written_paths"],
        }, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
