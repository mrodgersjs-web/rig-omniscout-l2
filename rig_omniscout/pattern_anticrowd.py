"""Anti-Crowd Score (ACS) engine — 1000x upgrade for RIG build-card strategy zones.

Scores every strategy zone (not just empty ones) with a multi-dimensional
opportunity model: market sizing, regulatory moat, competitor density, RIG
unfair advantage, revenue potential, and time-to-build.  All deterministic;
no LLM calls.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any

from rig_foundry.omniscout_build_cards import (
    L2_CARDS,
    L2_ROOT,
    atomic_json,
    load_topic_strategy,
    sha256_text,
    stable_json,
    utc_now,
)

SCHEMA = "rig.omniscout.pattern-anticrowd.v2"
OUTPUT_PATH = L2_ROOT / "pattern-anticrowd.json"

DEFAULT_TAM = 2.0e9
DEFAULT_LTV = 120_000.0

# ---------------------------------------------------------------------------
# Keyword / regulatory lexicons
# ---------------------------------------------------------------------------

REGULATORY_KEYWORDS = frozenset(
    [
        "legal",
        "compliance",
        "compliant",
        "regulatory",
        "regulation",
        "gdpr",
        "hipaa",
        "sox",
        "pci",
        "audit",
        "auditing",
        "certification",
        "standard",
        "doctrine",
        "healthcare",
        "dental",
        "ortho",
        "cfo",
        "law",
        "cpa",
        "attorney",
        "fda",
        "hippa",
        "privacy",
        "consent",
        "kyc",
        "aml",
        "finra",
        "sec",
        "irs",
        "phi",
        "pii",
        "soc2",
        "iso27001",
        "hipaa-compliant",
    ]
)

HIGH_BARRIER_KEYWORDS = frozenset(
    ["legal", "compliance", "regulatory", "doctrine", "healthcare", "dental"]
)
SCRAPING_KEYWORDS = frozenset(["scraping", "scrape", "crawl"])


# ---------------------------------------------------------------------------
# Text / parsing helpers
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens of length >= 3."""
    return {t for t in re.findall(r"[a-z0-9]{3,}", str(text).lower())}


def _slug_tokens(text: str) -> set[str]:
    """Tokens derived from strategy_id / label slugs."""
    return {t for t in re.findall(r"[a-z0-9]{2,}", str(text).lower())}


def _strategy_keywords(strategy_id: str, meta: dict[str, Any]) -> set[str]:
    """Extract searchable keywords from a strategy definition."""
    parts = [strategy_id]
    if "question" in meta and meta["question"]:
        parts.append(str(meta["question"]))
    for label in meta.get("label_match") or []:
        parts.append(str(label))
    for query in meta.get("consensus_queries") or []:
        parts.append(str(query))
    return _tokenize(" ".join(parts))


def _build_adjacency(strategy_defs: dict[str, Any]) -> dict[str, set[str]]:
    """Map each strategy_id to a set of adjacent strategy_ids."""
    keywords = {
        sid: _strategy_keywords(sid, meta)
        for sid, meta in strategy_defs.get("strategies", {}).items()
    }
    adjacency: dict[str, set[str]] = {sid: set() for sid in keywords}
    for sid, kws in keywords.items():
        for other_sid, other_kws in keywords.items():
            if other_sid == sid:
                continue
            if kws & other_kws:
                adjacency[sid].add(other_sid)
    tiers = {
        sid: meta.get("tier", "T3")
        for sid, meta in strategy_defs.get("strategies", {}).items()
    }
    for sid in keywords:
        if not adjacency[sid]:
            tier = tiers.get(sid, "T3")
            adjacency[sid] = {
                other for other, t in tiers.items() if other != sid and t == tier
            }
    return adjacency


def _text_blob(card: dict[str, Any]) -> str:
    """Flatten a card into searchable text."""
    parts: list[str] = []
    for key in ("claim", "summary", "mechanism", "why_not_median", "title", "topic"):
        parts.append(str(card.get(key) or ""))
    evidence = card.get("evidence") or []
    if isinstance(evidence, list):
        for ev in evidence:
            if isinstance(ev, dict):
                parts.append(str(ev.get("content") or ev.get("quote") or ev.get("url") or ""))
            else:
                parts.append(str(ev))
    deep = card.get("deep_sections") or {}
    if isinstance(deep, dict):
        for section in deep.values():
            if isinstance(section, dict):
                parts.append(str(section.get("content") or ""))
    consensus = card.get("consensus") or {}
    if isinstance(consensus, dict):
        for res in consensus.get("results") or []:
            if isinstance(res, dict):
                parts.append(str(res.get("abstract") or res.get("title") or ""))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Dollar / quantity parsers
# ---------------------------------------------------------------------------


def _parse_dollar_range(value: Any) -> tuple[float, float] | None:
    """Parse '$2-5B' or '$60K-240K' into (min, max)."""
    if value is None:
        return None
    s = str(value).lower().replace(",", "").replace("$", "")
    nums: list[float] = []
    # Match ranges like 2-5b, 60k-240k, or standalone 12b
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([kmbtq]?)\b", s):
        n = float(m.group(1))
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12, "q": 1e15, "": 1}.get(
            m.group(2), 1
        )
        nums.append(n * mult)
    if not nums:
        return None
    if len(nums) == 1:
        return (nums[0], nums[0])
    return (min(nums), max(nums))


def _parse_tam(value: Any) -> float | None:
    """Parse TAM to a single representative float (midpoint of range)."""
    rng = _parse_dollar_range(value)
    if not rng:
        return None
    return sum(rng) / 2.0


def _parse_loc(value: Any) -> float | None:
    """Parse '300-800' into midpoint LOC."""
    rng = _parse_dollar_range(value)
    if not rng:
        return None
    return sum(rng) / 2.0


_BUILD_EFFORT_DAYS = {
    "hour": 1,
    "day": 1,
    "week": 7,
    "month": 30,
    "quarter": 90,
    "year": 365,
}


def _parse_build_effort(value: Any) -> float | None:
    """Heuristic: '1-2 weeks' -> ~10.5 days."""
    if value is None:
        return None
    s = str(value).lower()
    nums: list[float] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)", s):
        nums.append(float(m.group(1)))
    unit = "week"
    for u, days in _BUILD_EFFORT_DAYS.items():
        if u in s or (u == "day" and "days" in s):
            unit = u
            break
    if not nums:
        return None
    if len(nums) == 1:
        return nums[0] * _BUILD_EFFORT_DAYS[unit]
    return (sum(nums) / len(nums)) * _BUILD_EFFORT_DAYS[unit]


def _format_dollar(n: float) -> str:
    if n >= 1e9:
        return f"${n/1e9:.1f}B"
    if n >= 1e6:
        return f"${n/1e6:.1f}M"
    if n >= 1e3:
        return f"${n/1e3:.1f}K"
    return f"${n:.0f}"


# ---------------------------------------------------------------------------
# RIG advantage corpus
# ---------------------------------------------------------------------------


def _rig_corpus_text() -> str:
    """Build a searchable text corpus from RIG code, skills, and doctrine."""
    parts: list[str] = []
    here = Path(__file__).resolve().parent
    roots = [
        here,
        here.parents[1] / "src" / "rig_foundry",
        Path.home() / ".rig" / "doctrine",
        Path.home() / ".claude" / "skills",
        Path.home() / ".codex" / "skills",
    ]
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("**/*.py", "**/*.md", "**/*.json", "**/*.yaml", "**/*.yml"):
            try:
                for path in root.glob(pattern):
                    if path.is_dir() or path in seen:
                        continue
                    seen.add(path)
                    try:
                        text = path.read_text(encoding="utf-8", errors="ignore")
                        if len(text) < 5_000_000:
                            parts.append(text)
                    except OSError:
                        pass
            except OSError:
                pass
    return "\n".join(parts).lower()


_RIG_CORPUS: str | None = None


def _rig_corpus() -> str:
    global _RIG_CORPUS
    if _RIG_CORPUS is None:
        _RIG_CORPUS = _rig_corpus_text()
    return _RIG_CORPUS


# ---------------------------------------------------------------------------
# Strategy-level extraction
# ---------------------------------------------------------------------------


def _extract_entities(cards: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Return typed entity name sets for a list of cards."""
    out: dict[str, set[str]] = {}
    for card in cards:
        entities = (card.get("entities") or {}).get("entities") or []
        for ent in entities or []:
            etype = str(ent.get("type") or "UNKNOWN").upper()
            name = str(ent.get("name") or "").strip().lower()
            if name:
                out.setdefault(etype, set()).add(name)
    return out


def _mean_adjacent_tam(
    strategy_id: str,
    adjacent: set[str],
    cards_by_strategy: dict[str, list[dict[str, Any]]],
) -> float:
    """Average TAM across cards in adjacent strategies, fallback to DEFAULT_TAM."""
    values: list[float] = []
    for adj_sid in adjacent:
        for card in cards_by_strategy.get(adj_sid, []):
            bi = card.get("business_intelligence") or {}
            tam = _parse_tam(bi.get("tam"))
            if tam is not None:
                values.append(tam)
    if not values:
        return DEFAULT_TAM
    return sum(values) / len(values)


def _strategy_tam_stats(
    strategy_id: str,
    cards: list[dict[str, Any]],
    adjacent: set[str],
    cards_by_strategy: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Compute TAM min/max/median for cards in the strategy."""
    values: list[float] = []
    for card in cards:
        bi = card.get("business_intelligence") or {}
        tam = _parse_tam(bi.get("tam"))
        if tam is not None:
            values.append(tam)
    if not values:
        fallback = _mean_adjacent_tam(strategy_id, adjacent, cards_by_strategy)
        values = [fallback]
    return {
        "min": min(values),
        "max": max(values),
        "median": float(statistics.median(values)),
    }


def _strategy_revenue_potential(cards: list[dict[str, Any]]) -> float:
    """Sum median LTV across cards."""
    total = 0.0
    for card in cards:
        bi = card.get("business_intelligence") or {}
        ltv = _parse_tam(bi.get("estimated_ltv"))
        if ltv is None:
            ltv = DEFAULT_LTV
        total += ltv
    return total


def _strategy_time_to_build(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate build days from build_effort + estimated_loc."""
    days_from_effort: list[float] = []
    locs: list[float] = []
    for card in cards:
        bi = card.get("business_intelligence") or {}
        bp = card.get("engineering_blueprint") or {}
        effort = _parse_build_effort(bi.get("build_effort"))
        if effort is not None:
            days_from_effort.append(effort)
        loc = _parse_loc(bp.get("estimated_loc"))
        if loc is not None:
            locs.append(loc)
    if days_from_effort:
        days = float(statistics.median(days_from_effort))
    elif locs:
        days = float(statistics.median(locs)) / 100.0
    else:
        days = 21.0
    # Faster builds score higher
    time_score = math.exp(-days / 60.0)
    return {
        "estimated_days": round(days, 2),
        "time_to_build_score": round(time_score, 4),
    }


def _strategy_regulatory_barrier(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Count compliance-related term occurrences across evidence/summary/mechanism."""
    text = " ".join(_text_blob(card) for card in cards).lower()
    counts = {kw: text.count(kw) for kw in REGULATORY_KEYWORDS}
    total = sum(counts.values())
    # Saturate around 20 hits
    score = min(total / 20.0, 1.0)
    return {"score": round(score, 4), "hit_count": total, "top_hits": counts}


def _strategy_competitor_density(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Count TOOL (and COMPANY) entities; more tools = more crowded market."""
    entities = _extract_entities(cards)
    tools = entities.get("TOOL", set()) | entities.get("FRAMEWORK", set())
    companies = entities.get("COMPANY", set())
    # Density saturates around 15 unique tools/companies
    density = min((len(tools) + len(companies) * 0.5) / 15.0, 1.0)
    return {
        "tool_count": len(tools),
        "company_count": len(companies),
        "competitor_density": round(density, 4),
    }


def _strategy_rig_advantage(
    strategy_id: str,
    strategy_meta: dict[str, Any],
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fraction of strategy entities that appear in RIG's existing corpus."""
    entities = _extract_entities(cards)
    # Focus on concrete entity types (tool/company/product/framework) and avoid
    # noise from ultra-generic concept names.
    concrete: set[str] = set()
    for etype in ("TOOL", "PRODUCT", "FRAMEWORK", "COMPANY", "PLATFORM"):
        concrete |= entities.get(etype, set())
    # If no concrete entities, fall back to concept entities with length > 4.
    if not concrete:
        for etype, names in entities.items():
            if etype not in {"TOOL", "PRODUCT", "FRAMEWORK", "COMPANY", "PLATFORM"}:
                concrete |= {n for n in names if len(n) > 4}
    # For empty or very thin strategies, use strategy-definition slugs as the
    # unfair-advantage signal set.
    if not concrete:
        concrete = {
            kw
            for kw in (_strategy_keywords(strategy_id, strategy_meta) | _slug_tokens(strategy_id))
            if len(kw) > 3
        }
    corpus = _rig_corpus()
    matched: set[str] = set()
    for name in concrete:
        if name in corpus:
            matched.add(name)
    # Also count partial phrase matches for multi-word entities.
    for name in concrete:
        if " " in name and name in corpus:
            matched.add(name)
    total = len(concrete) or 1
    score = min(len(matched) / max(total * 0.3, 1.0), 1.0)
    return {
        "matched_entities": sorted(matched),
        "matched_count": len(matched),
        "total_entities": len(concrete),
        "rig_advantage": round(score, 4),
    }


def _legacy_regulatory_barrier(strategy_id: str) -> float:
    """R component used inside the preserved ACS formula."""
    low = strategy_id.lower()
    if any(k in low for k in HIGH_BARRIER_KEYWORDS):
        return 0.9
    if any(k in low for k in SCRAPING_KEYWORDS):
        return 0.7
    return 0.4


def _legacy_risk_penalty(strategy_id: str) -> float:
    """C_risk component used inside the preserved ACS formula."""
    low = strategy_id.lower()
    if any(k in low for k in REGULATORY_KEYWORDS):
        return 0.6
    return 0.3


def _legacy_rig_advantage(
    strategy_id: str,
    strategy_meta: dict[str, Any],
    all_cards: list[dict[str, Any]],
) -> float:
    """U component used inside the preserved ACS formula."""
    keywords = _strategy_keywords(strategy_id, strategy_meta)
    if not keywords:
        return 0.0
    matches = 0
    for card in all_cards:
        entities = (card.get("entities") or {}).get("entities", [])
        for entity in entities:
            name = str(entity.get("name") or "").lower()
            if any(kw in name or name in kw for kw in keywords):
                matches += 1
                break
    return min(matches / 10.0, 1.0)


# ---------------------------------------------------------------------------
# Core ACS + 1000x dimensions
# ---------------------------------------------------------------------------


def compute_acs(
    strategy_id: str,
    strategy_meta: dict[str, Any],
    cards_in_strategy: list[dict[str, Any]],
    adjacent_strategies: set[str],
    cards_by_strategy: dict[str, list[dict[str, Any]]],
    all_cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute the Anti-Crowd Score + 1000x opportunity dimensions.

    Returns a dict with the legacy ACS (0-100), 8+ score dimensions, and
    a strategy recommendation.
    """
    card_count = len(cards_in_strategy)
    emptiness = 1.0 / (1.0 + card_count)

    # Legacy market component (adjacent TAM, preserved exactly).
    adjacent_tam = _mean_adjacent_tam(
        strategy_id, adjacent_strategies, cards_by_strategy
    )
    market = math.log10(adjacent_tam) / 12.0

    # Legacy components (preserved).
    regulatory_legacy = _legacy_regulatory_barrier(strategy_id)
    risk = _legacy_risk_penalty(strategy_id)
    rig_adv_legacy = _legacy_rig_advantage(strategy_id, strategy_meta, all_cards)

    raw = (
        math.pow(emptiness, 1.5) * market * regulatory_legacy * rig_adv_legacy
    ) / (1.0 + risk)
    acs = round(raw * 100.0, 2)

    # ---- 1000x dimensions -------------------------------------------------
    tam_stats = _strategy_tam_stats(
        strategy_id, cards_in_strategy, adjacent_strategies, cards_by_strategy
    )
    median_tam = tam_stats["median"]
    market_size_score = min(max(math.log10(median_tam) / 12.0, 0.0), 1.0)

    reg = _strategy_regulatory_barrier(cards_in_strategy)
    comp = _strategy_competitor_density(cards_in_strategy)
    rig = _strategy_rig_advantage(strategy_id, strategy_meta, cards_in_strategy)
    revenue = _strategy_revenue_potential(cards_in_strategy)
    ttb = _strategy_time_to_build(cards_in_strategy)

    acs_norm = acs / 100.0
    opportunity = (
        acs_norm
        * market_size_score
        * (1.0 - comp["competitor_density"])
        * (1.0 + reg["score"])
        * rig["rig_advantage"]
        * ttb["time_to_build_score"]
    )
    risk_adjusted = round(min(opportunity, 1.0) * 100.0, 2)

    recommendation = _recommended_action(risk_adjusted)

    return {
        # Identifiers
        "strategy_id": strategy_id,
        "tier": strategy_meta.get("tier", "T3"),
        "card_count": card_count,
        # Legacy ACS (preserved formula)
        "emptiness": round(emptiness, 4),
        "market": round(market, 4),
        "adjacent_tam": adjacent_tam,
        "regulatory_barrier_legacy": regulatory_legacy,
        "rig_advantage_legacy": round(rig_adv_legacy, 4),
        "risk_penalty": risk,
        "acs": acs,
        "adjacent_strategies": sorted(adjacent_strategies),
        # 1000x dimensions
        "market_size": tam_stats,
        "market_size_score": round(market_size_score, 4),
        "regulatory_barrier": reg["score"],
        "regulatory_hits": reg["hit_count"],
        "competitor_density": comp["competitor_density"],
        "tool_count": comp["tool_count"],
        "company_count": comp["company_count"],
        "rig_advantage": rig["rig_advantage"],
        "rig_matched_entities": rig["matched_entities"],
        "rig_matched_count": rig["matched_count"],
        "revenue_potential": round(revenue, 2),
        "time_to_build": ttb,
        "risk_adjusted_opportunity_score": risk_adjusted,
        "recommendation": recommendation,
    }


def _recommended_action(score: float) -> str:
    """Map ri[REDACTED] opportunity score to a build recommendation."""
    if score >= 25.0:
        return "DOMINATE"
    if score >= 10.0:
        return "ENTER"
    if score >= 3.0:
        return "WATCH"
    return "IGNORE"


# ---------------------------------------------------------------------------
# Load / score
# ---------------------------------------------------------------------------


def _load_cards() -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Return cards grouped by strategy_id plus the flat list."""
    cards_by_strategy: dict[str, list[dict[str, Any]]] = {}
    all_cards: list[dict[str, Any]] = []
    if not L2_CARDS.exists():
        return cards_by_strategy, all_cards
    for path in sorted(L2_CARDS.glob("l2-*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        all_cards.append(card)
        sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
        cards_by_strategy.setdefault(sid, []).append(card)
    return cards_by_strategy, all_cards


def score_all_strategies() -> dict[str, Any]:
    """Score every strategy in TOPIC_STRATEGY and return a ranked result."""
    strategy_defs = load_topic_strategy()
    strategies = strategy_defs.get("strategies", {})
    adjacency = _build_adjacency(strategy_defs)
    cards_by_strategy, all_cards = _load_cards()

    scored: list[dict[str, Any]] = []
    empty_strategies: list[str] = []
    nearly_empty_strategies: list[str] = []

    for sid, meta in strategies.items():
        cards = cards_by_strategy.get(sid, [])
        count = len(cards)
        if count == 0:
            empty_strategies.append(sid)
        elif count <= 2:
            nearly_empty_strategies.append(sid)
        result = compute_acs(
            sid,
            meta,
            cards,
            adjacency.get(sid, set()),
            cards_by_strategy,
            all_cards,
        )
        scored.append(result)

    scored.sort(key=lambda x: x["risk_adjusted_opportunity_score"], reverse=True)

    top = scored[0] if scored else None
    summary = {
        "schema": SCHEMA,
        "scored_at": utc_now(),
        "total_strategies_scored": len(scored),
        "empty_strategies": sorted(empty_strategies),
        "nearly_empty_strategies": sorted(nearly_empty_strategies),
        "strategies": scored,
        "top_strategy": top["strategy_id"] if top else None,
        "top_acs": top["acs"] if top else None,
        "top_risk_adjusted": top["risk_adjusted_opportunity_score"] if top else None,
        "top_recommendation": top["recommendation"] if top else None,
    }

    brief = generate_brief(summary)
    summary["brief"] = brief["top_opportunities"]
    summary["synergies"] = brief["synergies"]

    summary["artifact_sha256"] = sha256_text(
        stable_json({k: v for k, v in summary.items() if k != "artifact_sha256"})
    )
    return summary


# ---------------------------------------------------------------------------
# Synergy detection
# ---------------------------------------------------------------------------


def _strategy_entity_set(cards: list[dict[str, Any]]) -> set[str]:
    """Return the union of concrete entity names for a strategy."""
    entities = _extract_entities(cards)
    out: set[str] = set()
    for etype in ("TOOL", "PRODUCT", "FRAMEWORK", "COMPANY", "PLATFORM", "CONCEPT"):
        out |= {n for n in entities.get(etype, set()) if len(n) > 3}
    return out


def detect_synergies(
    cards_by_strategy: dict[str, list[dict[str, Any]]],
    threshold: float = 0.15,
) -> list[dict[str, Any]]:
    """Find strategy pairs that share entities and could be combined."""
    entity_sets = {
        sid: _strategy_entity_set(cards) for sid, cards in cards_by_strategy.items()
    }
    pairs: list[dict[str, Any]] = []
    sids = sorted(entity_sets)
    for i, sid_a in enumerate(sids):
        for sid_b in sids[i + 1 :]:
            a = entity_sets[sid_a]
            b = entity_sets[sid_b]
            if not a or not b:
                continue
            inter = a & b
            if not inter:
                continue
            jaccard = len(inter) / len(a | b)
            overlap = len(inter) / min(len(a), len(b))
            if jaccard >= threshold or (overlap >= 0.25 and len(inter) >= 2):
                pairs.append(
                    {
                        "strategy_a": sid_a,
                        "strategy_b": sid_b,
                        "shared_entities": sorted(inter),
                        "shared_count": len(inter),
                        "jaccard": round(jaccard, 4),
                        "overlap": round(overlap, 4),
                    }
                )
    pairs.sort(key=lambda x: x["jaccard"], reverse=True)
    return pairs


# ---------------------------------------------------------------------------
# Daily brief
# ---------------------------------------------------------------------------


def generate_brief(summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate the daily anti-crowd brief: top 5 opportunities + synergies."""
    if summary is None:
        summary = score_all_strategies()

    strategies = summary.get("strategies", [])
    top5 = strategies[:5]
    cards_by_strategy, _ = _load_cards()
    synergies = detect_synergies(cards_by_strategy)

    opportunities: list[dict[str, Any]] = []
    for rank, s in enumerate(top5, start=1):
        justification = (
            f"#{rank} {s['strategy_id']} ({s['tier']}) — recommendation: {s['recommendation']}. "
            f"ACS={s['acs']:.1f}; ri[REDACTED] opportunity={s['risk_adjusted_opportunity_score']:.1f}. "
            f"Market median TAM {_format_dollar(s['market_size']['median'])}, "
            f"score {s['market_size_score']:.2f}. "
            f"Regulatory moat {s['regulatory_barrier']:.2f} ({s['regulatory_hits']} hits). "
            f"Competitor density {s['competitor_density']:.2f} from "
            f"{s['tool_count']} tools / {s['company_count']} companies. "
            f"RIG advantage {s['rig_advantage']:.2f} with "
            f"{s['rig_matched_count']} matched entities. "
            f"Revenue potential {_format_dollar(s['revenue_potential'])} across "
            f"{s['card_count']} cards; estimated build time "
            f"{s['time_to_build']['estimated_days']:.0f} days."
        )
        opportunities.append(
            {
                "rank": rank,
                "strategy_id": s["strategy_id"],
                "tier": s["tier"],
                "recommendation": s["recommendation"],
                "acs": s["acs"],
                "risk_adjusted_opportunity_score": s["risk_adjusted_opportunity_score"],
                "justification": justification,
                "market_size_median": s["market_size"]["median"],
                "market_size_score": s["market_size_score"],
                "regulatory_barrier": s["regulatory_barrier"],
                "competitor_density": s["competitor_density"],
                "rig_advantage": s["rig_advantage"],
                "revenue_potential": s["revenue_potential"],
                "time_to_build_days": s["time_to_build"]["estimated_days"],
            }
        )

    return {
        "schema": SCHEMA,
        "generated_at": utc_now(),
        "top_opportunities": opportunities,
        "synergies": synergies[:20],
        "summary": {
            "total_strategies_scored": summary.get("total_strategies_scored", 0),
            "top_strategy": summary.get("top_strategy"),
            "top_risk_adjusted": summary.get("top_risk_adjusted"),
        },
    }


# ---------------------------------------------------------------------------
# Reporting / CLI
# ---------------------------------------------------------------------------


def _print_report(summary: dict[str, Any]) -> None:
    print(f"Anti-Crowd Score Report ({summary.get('scored_at')})")
    print("-" * 90)
    print(
        f"{'Rank':>4} {'Strategy':<30} {'Cards':>5} {'ACS':>8} "
        f"{'RiskAdj':>8} {'Rec':>10}"
    )
    print("-" * 90)
    for rank, item in enumerate(summary.get("strategies", []), start=1):
        print(
            f"{rank:>4} {item['strategy_id']:<30} {item['card_count']:>5} "
            f"{item['acs']:>8.2f} {item['risk_adjusted_opportunity_score']:>8.2f} "
            f"{item['recommendation']:>10}"
        )
    print("-" * 90)
    top = summary.get("top_strategy")
    if top:
        print(
            f"Top: {top} (ACS={summary['top_acs']}, "
            f"risk_adjusted={summary['top_risk_adjusted']}, "
            f"action={summary['top_recommendation']})"
        )


def _print_brief(brief: dict[str, Any]) -> None:
    print(f"Daily Anti-Crowd Brief ({brief.get('generated_at')})")
    print("=" * 90)
    for opp in brief.get("top_opportunities", []):
        print(opp["justification"])
        print("-" * 90)
    print("Top synergies:")
    for syn in brief.get("synergies", [])[:10]:
        print(
            f"  {syn['strategy_a']} <-> {syn['strategy_b']}: "
            f"{syn['shared_count']} shared ({', '.join(syn['shared_entities'][:5])})"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anti-Crowd Score engine (1000x)")
    parser.add_argument(
        "cmd",
        choices=["all", "brief", "status"],
        default="all",
        nargs="?",
        help="Run full scoring, print daily brief, or print status.",
    )
    args = parser.parse_args(argv)

    if args.cmd == "status":
        if OUTPUT_PATH.exists():
            summary = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            _print_report(summary)
            return 0
        print("No pattern-anticrowd.json found; run 'all' first.")
        return 1

    if args.cmd == "brief":
        if OUTPUT_PATH.exists():
            summary = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
        else:
            summary = score_all_strategies()
            atomic_json(OUTPUT_PATH, summary)
        brief = generate_brief(summary)
        _print_brief(brief)
        return 0

    summary = score_all_strategies()
    atomic_json(OUTPUT_PATH, summary)
    _print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
