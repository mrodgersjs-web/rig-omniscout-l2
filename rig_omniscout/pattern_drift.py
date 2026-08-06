"""Epistemic Drift engine — 1000x upgrade.

Detects concept vocabulary neighborhoods, cross-domain bridge concepts,
temporal drift velocity, and category-formation frontiers from V30 build
cards.  All scoring is deterministic (A1 — no LLM calls).

Key exports:
  - compute_drift(cards=None, output_path=None) -> dict
  - score_all_strategies(cards) -> dict[str, dict]
  - generate_brief(report, top_n=5) -> dict
  - main(argv=None) -> int

CLI commands: all | brief | status
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rig_foundry.omniscout_build_cards import (
    DOCTRINE_DOMAINS,
    L2_CARDS,
    L2_ROOT,
    atomic_json,
    sha256_text,
    stable_json,
    utc_now,
)

SCHEMA = "rig.omniscout.pattern-drift.v2"

# Strategies that are intentionally empty in the current card corpus.
EMPTY_STRATEGIES = {
    "automation-runtime",
    "doctrine-control-plane",
    "knowledge-memory",
    "scraping-intelligence",
    "legal-compliance",
    "vertical-dental-ortho",
    "vertical-pe-cfo",
}

# Weights for the leading-indicator composite (0-1 scale).
COMPOSITE_WEIGHTS = {
    "frontier": 0.30,
    "bridge": 0.30,
    "velocity": 0.20,
    "cross_domain": 0.20,
}

# Threshold that flags a strategy as a 6-month leading indicator.
LEADING_INDICATOR_THRESHOLD = 0.40

# Text tokenization for claim / mechanism / evidence vocabulary.
_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; return None on failure."""
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _entity_key(entity: dict[str, Any]) -> tuple[str, str]:
    """Canonical (name, type) key for an entity."""
    name = (entity.get("name") or "").strip().lower()
    typ = (entity.get("type") or "UNKNOWN").strip().upper()
    return (name, typ)


def _tokenize_text(text: str | None) -> list[str]:
    """Extract lowercase alphanumeric tokens from free text."""
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _card_vocabulary(card: dict[str, Any]) -> dict[str, Any]:
    """Extract all vocabulary signals from a single card.

    Returns:
        {
            "entities": list of normalized entity dicts,
            "entity_keys": set of (name, type) keys,
            "text_tokens": list of tokens from claim/mechanism/evidence,
            "claim": str,
            "mechanism": str,
            "evidence_quotes": list[str],
        }
    """
    card_id = card.get("card_id", "")
    created = _parse_iso(card.get("created_at"))

    entities = _card_entities(card)
    entity_keys = {_entity_key(e) for e in entities}

    claim = str(card.get("claim") or "")
    mechanism = str(card.get("mechanism") or "")
    evidence_quotes: list[str] = []
    for ev in card.get("evidence", []) or []:
        quote = ev.get("quote_or_fact") if isinstance(ev, dict) else None
        if quote:
            evidence_quotes.append(str(quote))

    all_text = " ".join([claim, mechanism, " ".join(evidence_quotes)])
    text_tokens = _tokenize_text(all_text)

    return {
        "entities": entities,
        "entity_keys": entity_keys,
        "text_tokens": text_tokens,
        "claim": claim,
        "mechanism": mechanism,
        "evidence_quotes": evidence_quotes,
        "card_id": card_id,
        "created_at": created,
    }


def load_cards(cards_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load every l2-*.json build card from the card directory."""
    directory = cards_dir or L2_CARDS
    if not directory.exists():
        return []
    cards: list[dict[str, Any]] = []
    for path in sorted(directory.glob("l2-*.json")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                cards.append(json.load(fh))
        except Exception:
            continue
    return cards


def _strategy_id(card: dict[str, Any]) -> str:
    """Return the strategy_id for a card, defaulting to 'unknown'."""
    return str(card.get("strategy", {}).get("strategy_id") or "unknown").strip() or "unknown"


def _card_entities(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized entity records for a single card."""
    raw = card.get("entities", {}).get("entities", []) or []
    card_id = card.get("card_id", "")
    created = _parse_iso(card.get("created_at"))
    out: list[dict[str, Any]] = []
    for ent in raw:
        if not isinstance(ent, dict):
            continue
        name, typ = _entity_key(ent)
        if not name:
            continue
        out.append(
            {
                "name": name,
                "type": typ,
                "entity_id": ent.get("entity_id", ""),
                "domain": ent.get("domain", ""),
                "card_id": card_id,
                "created_at": created,
            }
        )
    return out


def _global_entity_card_count(cards: list[dict[str, Any]]) -> Counter[tuple[str, str]]:
    """Count in how many distinct cards each (name, type) entity appears."""
    counts: Counter[tuple[str, str]] = Counter()
    for card in cards:
        for key in {_entity_key(e) for e in _card_entities(card)}:
            counts[key] += 1
    return counts


def build_cooccurrence_matrix(
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build global entity co-occurrence matrix and PMI scores.

    For every card we collect the unique (name, type) entities present,
    then count every undirected pair once.  PMI is computed as:

        PMI(x, y) = log2( P(x,y) / (P(x) * P(y)) )

    where probabilities are card-level frequencies.

    Returns:
        {
            "entity_card_count": Counter[(name, type) -> int],
            "pair_cooccurrence": Counter[((name,type),(name,type)) -> int],
            "pmi_scores": dict[pair -> float],
            "total_cards": int,
            "unique_entities": int,
            "top_pairs": [...],
            "top_pmi_pairs": [...],
        }
    """
    entity_card_count: Counter[tuple[str, str]] = Counter()
    pair_cooccurrence: Counter[tuple[tuple[str, str], tuple[str, str]]] = Counter()

    for card in cards:
        keys = sorted({_entity_key(e) for e in _card_entities(card)})
        for key in keys:
            entity_card_count[key] += 1
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                pair_cooccurrence[(keys[i], keys[j])] += 1

    total_cards = max(len(cards), 1)
    pmi_scores: dict[tuple[tuple[str, str], tuple[str, str]], float] = {}

    for (a, b), co_count in pair_cooccurrence.items():
        p_xy = co_count / total_cards
        p_x = entity_card_count[a] / total_cards
        p_y = entity_card_count[b] / total_cards
        if p_x > 0.0 and p_y > 0.0 and p_xy > 0.0:
            pmi_scores[(a, b)] = math.log2(p_xy / (p_x * p_y))

    top_pairs = [
        {
            "entity_a": {"name": a[0], "type": a[1]},
            "entity_b": {"name": b[0], "type": b[1]},
            "cooccurrence_count": count,
        }
        for (a, b), count in pair_cooccurrence.most_common(100)
    ]

    top_pmi_pairs = [
        {
            "entity_a": {"name": a[0], "type": a[1]},
            "entity_b": {"name": b[0], "type": b[1]},
            "pmi": round(pmi, 6),
            "cooccurrence_count": pair_cooccurrence[(a, b)],
        }
        for (a, b), pmi in sorted(pmi_scores.items(), key=lambda x: -x[1])[:100]
    ]

    return {
        "entity_card_count": entity_card_count,
        "pair_cooccurrence": pair_cooccurrence,
        "pmi_scores": pmi_scores,
        "total_cards": len(cards),
        "unique_entities": len(entity_card_count),
        "top_pairs": top_pairs,
        "top_pmi_pairs": top_pmi_pairs,
    }


def build_ego_vectors(
    cards: list[dict[str, Any]],
    cooccurrence: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Build PMI-weighted ego-vectors for each strategy.

    For every entity in a strategy, we aggregate the PMI of every
    co-occurring pair it participates in.  The result is a weighted
    neighbor profile that surfaces which entities sit at the center of
    the strategy's concept neighborhood.
    """
    pmi_scores = cooccurrence["pmi_scores"]
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in cards:
        by_strategy[_strategy_id(card)].append(card)

    ego_vectors: dict[str, list[dict[str, Any]]] = {}
    for sid, strategy_cards in by_strategy.items():
        neighbor_weight: Counter[tuple[str, str]] = Counter()
        neighbor_pair_count: Counter[tuple[str, str]] = Counter()

        for card in strategy_cards:
            keys = sorted({_entity_key(e) for e in _card_entities(card)})
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    pair = (keys[i], keys[j])
                    pmi = pmi_scores.get(pair, 0.0)
                    if pmi > 0.0:
                        neighbor_weight[keys[i]] += pmi
                        neighbor_weight[keys[j]] += pmi
                        neighbor_pair_count[keys[i]] += 1
                        neighbor_pair_count[keys[j]] += 1

        ego_vectors[sid] = [
            {
                "name": key[0],
                "type": key[1],
                "pmi_weight": round(float(weight), 6),
                "cooccurrence_pairs": neighbor_pair_count[key],
            }
            for key, weight in neighbor_weight.most_common(20)
        ]

    return ego_vectors


def _compute_bridge_concepts(
    cards: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Identify bridge concepts — entities that appear in 3+ strategy_ids.

    Bridge concepts migrate across strategies and are leading indicators
    of new category formation.
    """
    entity_strategies: dict[tuple[str, str], set[str]] = defaultdict(set)
    entity_domains: dict[tuple[str, str], set[str]] = defaultdict(set)
    entity_card_count: Counter[tuple[str, str]] = Counter()

    for card in cards:
        sid = _strategy_id(card)
        doctrine_domains = set(card.get("doctrine_domains", []) or [])
        seen_in_card: set[tuple[str, str]] = set()
        for ent in _card_entities(card):
            key = _entity_key(ent)
            entity_strategies[key].add(sid)
            entity_domains[key].update(doctrine_domains)
            if ent.get("domain"):
                entity_domains[key].add(ent["domain"])
            if key not in seen_in_card:
                entity_card_count[key] += 1
                seen_in_card.add(key)

    bridges: dict[tuple[str, str], dict[str, Any]] = {}
    for key, strategies in entity_strategies.items():
        if len(strategies) >= 3:
            bridges[key] = {
                "name": key[0],
                "type": key[1],
                "card_count": entity_card_count[key],
                "strategy_count": len(strategies),
                "strategies": sorted(strategies),
                "domains": sorted(entity_domains[key]),
            }

    return bridges


def _split_temporal(
    strategy_cards: list[dict[str, Any]],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Return (oldest_entities, newest_entities) for a strategy.

    Cards are sorted by created_at.  The oldest third and newest third are
    compared.  Entities that appear only in the newest slice are "newly
    emerged"; entities only in the oldest slice are "deprecated".
    """
    parsed = []
    for card in strategy_cards:
        dt = _parse_iso(card.get("created_at"))
        if dt is None:
            dt = datetime.min.replace(tzinfo=timezone.utc)
        parsed.append((dt, card))
    parsed.sort(key=lambda x: x[0])

    n = len(parsed)
    if n == 0:
        return set(), set()

    split_size = max(1, n // 3)
    oldest = [card for _, card in parsed[:split_size]]
    newest = [card for _, card in parsed[-split_size:]]

    oldest_entities = {_entity_key(e) for card in oldest for e in _card_entities(card)}
    newest_entities = {_entity_key(e) for card in newest for e in _card_entities(card)}

    return oldest_entities, newest_entities


def _strategy_tier(strategy_cards: list[dict[str, Any]]) -> str:
    """Return the strategy tier from the first card that has one."""
    for card in strategy_cards:
        tier = card.get("strategy", {}).get("tier")
        if tier:
            return str(tier)
    return ""


def score_all_strategies(cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Score every strategy by upgraded epistemic-drift metrics.

    Returns a dict keyed by strategy_id with:
      - concept frontier ratios (frontier / emerging / established)
      - bridge concept density
      - cross-domain entity ratio
      - temporal drift velocity
      - PMI-weighted ego-vector
      - leading-indicator composite score
      - 6-month prediction flag
    """
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in cards:
        by_strategy[_strategy_id(card)].append(card)

    for sid in EMPTY_STRATEGIES:
        if sid not in by_strategy:
            by_strategy[sid] = []

    global_entity_card_count = _global_entity_card_count(cards)
    bridge_concepts = _compute_bridge_concepts(cards)
    cooccurrence = build_cooccurrence_matrix(cards)
    ego_vectors = build_ego_vectors(cards, cooccurrence)

    strategy_scores: dict[str, dict[str, Any]] = {}

    for sid, strategy_cards in by_strategy.items():
        entities_per_card: list[list[dict[str, Any]]] = []
        all_entities: list[dict[str, Any]] = []
        for card in strategy_cards:
            card_ents = _card_entities(card)
            entities_per_card.append(card_ents)
            all_entities.extend(card_ents)

        unique_keys = {_entity_key(e) for e in all_entities}
        total_entities = len(unique_keys)

        tier = _strategy_tier(strategy_cards)

        if total_entities == 0:
            strategy_scores[sid] = {
                "strategy_id": sid,
                "card_count": len(strategy_cards),
                "tier": tier,
                "total_entities": 0,
                "frontier_entities": 0,
                "frontier_ratio": 0.0,
                "emerging_entities": 0,
                "emerging_ratio": 0.0,
                "established_entities": 0,
                "established_ratio": 0.0,
                "dominant_type": "",
                "cross_domain_entities": 0,
                "cross_domain_ratio": 0.0,
                "bridge_concepts": 0,
                "bridge_ratio": 0.0,
                "bridge_concept_names": [],
                "newly_emerged_entities": 0,
                "newly_emerged_ratio": 0.0,
                "deprecated_entities": 0,
                "deprecated_ratio": 0.0,
                "drift_velocity": 0.0,
                "drift_score": 0.0,
                "concept_velocity": 0.0,
                "frontier_score": 0.0,
                "bridge_score": 0.0,
                "velocity_score": 0.0,
                "cross_domain_score": 0.0,
                "composite_drift_score": 0.0,
                "leading_indicator": False,
                "prediction": "insufficient signal",
                "entity_types": {},
                "frontier_entity_names": [],
                "emerging_entity_names": [],
                "established_entity_names": [],
                "cross_domain_entity_names": [],
                "newly_emerged_entity_names": [],
                "deprecated_entity_names": [],
                "ego_vector": [],
            }
            continue

        # Entity type distribution.
        type_counts = Counter(key[1] for key in unique_keys)
        dominant_type = type_counts.most_common(1)[0][0]

        # Concept frontier tiers.
        frontier_keys = {k for k in unique_keys if global_entity_card_count[k] == 1}
        emerging_keys = {k for k in unique_keys if global_entity_card_count[k] in (2, 3)}
        established_keys = {k for k in unique_keys if global_entity_card_count[k] >= 4}

        frontier_entities = len(frontier_keys)
        emerging_entities = len(emerging_keys)
        established_entities = len(established_keys)
        frontier_ratio = frontier_entities / total_entities
        emerging_ratio = emerging_entities / total_entities
        established_ratio = established_entities / total_entities

        # Cross-domain: entities whose type differs from the dominant type.
        cross_domain_keys = {k for k in unique_keys if k[1] != dominant_type}
        cross_domain_entities = len(cross_domain_keys)
        cross_domain_ratio = cross_domain_entities / total_entities

        # Bridge concepts inside this strategy.
        strategy_bridge_keys = {k for k in unique_keys if k in bridge_concepts}
        bridge_entities = len(strategy_bridge_keys)
        bridge_ratio = bridge_entities / total_entities

        # Temporal drift: oldest vs newest cards.
        oldest_entities, newest_entities = _split_temporal(strategy_cards)
        newly_emerged_keys = newest_entities - oldest_entities
        deprecated_keys = oldest_entities - newest_entities
        newly_emerged_entities = len(newly_emerged_keys)
        deprecated_entities = len(deprecated_keys)
        newly_emerged_ratio = newly_emerged_entities / total_entities
        deprecated_ratio = deprecated_entities / total_entities
        drift_velocity = newly_emerged_ratio

        # Legacy 24h concept velocity (kept for continuity).
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        recent_entity_keys: set[tuple[str, str]] = set()
        for card in strategy_cards:
            created = _parse_iso(card.get("created_at"))
            if created and created >= cutoff:
                recent_entity_keys.update({_entity_key(e) for e in _card_entities(card)})
        concept_velocity = len({k for k in unique_keys if k in recent_entity_keys}) / total_entities

        # Legacy drift_score (frontier * cross-domain).
        drift_score = frontier_ratio * cross_domain_ratio

        # Leading-indicator composite.
        frontier_score = frontier_ratio * COMPOSITE_WEIGHTS["frontier"]
        bridge_score = bridge_ratio * COMPOSITE_WEIGHTS["bridge"]
        velocity_score = drift_velocity * COMPOSITE_WEIGHTS["velocity"]
        cross_domain_score = cross_domain_ratio * COMPOSITE_WEIGHTS["cross_domain"]
        composite = frontier_score + bridge_score + velocity_score + cross_domain_score
        composite = min(1.0, max(0.0, composite))

        leading_indicator = composite > LEADING_INDICATOR_THRESHOLD
        prediction = (
            "6-month leading indicator — high frontier + bridge density"
            if leading_indicator
            else "category edge" if composite > 0.25 else "mainstream or nascent"
        )

        strategy_scores[sid] = {
            "strategy_id": sid,
            "card_count": len(strategy_cards),
            "tier": tier,
            "total_entities": total_entities,
            "frontier_entities": frontier_entities,
            "frontier_ratio": round(frontier_ratio, 6),
            "emerging_entities": emerging_entities,
            "emerging_ratio": round(emerging_ratio, 6),
            "established_entities": established_entities,
            "established_ratio": round(established_ratio, 6),
            "dominant_type": dominant_type,
            "cross_domain_entities": cross_domain_entities,
            "cross_domain_ratio": round(cross_domain_ratio, 6),
            "bridge_concepts": bridge_entities,
            "bridge_ratio": round(bridge_ratio, 6),
            "bridge_concept_names": sorted([k[0] for k in strategy_bridge_keys]),
            "newly_emerged_entities": newly_emerged_entities,
            "newly_emerged_ratio": round(newly_emerged_ratio, 6),
            "deprecated_entities": deprecated_entities,
            "deprecated_ratio": round(deprecated_ratio, 6),
            "drift_velocity": round(drift_velocity, 6),
            "drift_score": round(drift_score, 6),
            "concept_velocity": round(concept_velocity, 6),
            "frontier_score": round(frontier_score, 6),
            "bridge_score": round(bridge_score, 6),
            "velocity_score": round(velocity_score, 6),
            "cross_domain_score": round(cross_domain_score, 6),
            "composite_drift_score": round(composite, 6),
            "leading_indicator": leading_indicator,
            "prediction": prediction,
            "entity_types": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
            "frontier_entity_names": sorted([k[0] for k in frontier_keys]),
            "emerging_entity_names": sorted([k[0] for k in emerging_keys]),
            "established_entity_names": sorted([k[0] for k in established_keys]),
            "cross_domain_entity_names": sorted([k[0] for k in cross_domain_keys]),
            "newly_emerged_entity_names": sorted([k[0] for k in newly_emerged_keys]),
            "deprecated_entity_names": sorted([k[0] for k in deprecated_keys]),
            "ego_vector": ego_vectors.get(sid, []),
        }

    return strategy_scores


def _compute_top_novel_entities(
    cards: list[dict[str, Any]],
    global_entity_card_count: Counter[tuple[str, str]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the top globally rare / novel entities.

    Entities are frontier (appear in exactly one card).  They are ranked by
    type rarity and then alphabetically to keep output deterministic.
    """
    type_rank = {"TOOL": 0, "METRIC": 1, "COMPANY": 2, "METHOD": 3, "CONCEPT": 4}
    frontier: list[dict[str, Any]] = []

    seen: set[tuple[str, str]] = set()
    for card in cards:
        sid = _strategy_id(card)
        for ent in _card_entities(card):
            key = _entity_key(ent)
            if key in seen:
                continue
            seen.add(key)
            if global_entity_card_count[key] == 1:
                frontier.append(
                    {
                        "name": key[0],
                        "type": key[1],
                        "strategy_id": sid,
                        "card_id": ent.get("card_id", ""),
                    }
                )

    frontier.sort(key=lambda e: (type_rank.get(e["type"], 99), e["name"]))
    return frontier[:limit]


def _compute_cross_domain_migration_candidates(
    strategy_scores: dict[str, dict[str, Any]],
    cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Identify entities migrating across semantic domains.

    A migration candidate is an entity whose type differs from the dominant
    type of at least one strategy in which it appears.  Entities that show up
    as cross-domain in multiple strategies are the strongest migration signals.
    """
    entity_strategies: dict[tuple[str, str], set[str]] = defaultdict(set)
    entity_cross_domain_strategies: dict[tuple[str, str], set[str]] = defaultdict(set)
    entity_card_count: Counter[tuple[str, str]] = Counter()

    for card in cards:
        sid = _strategy_id(card)
        dominant = strategy_scores.get(sid, {}).get("dominant_type", "")
        seen_in_card: set[tuple[str, str]] = set()
        for ent in _card_entities(card):
            key = _entity_key(ent)
            entity_strategies[key].add(sid)
            if key not in seen_in_card:
                entity_card_count[key] += 1
                seen_in_card.add(key)
            if dominant and key[1] != dominant:
                entity_cross_domain_strategies[key].add(sid)

    candidates: list[dict[str, Any]] = []
    for key, cross_strategies in entity_cross_domain_strategies.items():
        if not cross_strategies:
            continue
        strategies = entity_strategies[key]
        candidates.append(
            {
                "name": key[0],
                "type": key[1],
                "card_count": entity_card_count[key],
                "strategy_count": len(strategies),
                "cross_domain_strategy_count": len(cross_strategies),
                "strategies": sorted(strategies),
                "cross_domain_strategies": sorted(cross_strategies),
            }
        )

    type_rank = {"TOOL": 0, "METRIC": 1, "COMPANY": 2, "METHOD": 3, "CONCEPT": 4}
    candidates.sort(
        key=lambda e: (
            -e["cross_domain_strategy_count"],
            -e["strategy_count"],
            type_rank.get(e["type"], 99),
            e["name"],
        )
    )
    return candidates


def _vocabulary_summary(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate token-level vocabulary across all cards."""
    token_counter: Counter[str] = Counter()
    strategy_tokens: dict[str, Counter[str]] = defaultdict(Counter)
    for card in cards:
        vocab = _card_vocabulary(card)
        sid = _strategy_id(card)
        for token in vocab["text_tokens"]:
            token_counter[token] += 1
            strategy_tokens[sid][token] += 1

    return {
        "unique_token_count": len(token_counter),
        "top_tokens": [
            {"token": token, "count": count}
            for token, count in token_counter.most_common(50)
        ],
        "strategy_token_count": {
            sid: len(counter) for sid, counter in strategy_tokens.items()
        },
    }


def compute_drift(
    cards: list[dict[str, Any]] | None = None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Compute the full epistemic drift report.

    Args:
        cards: optional pre-loaded card list; otherwise loaded from disk.
        output_path: optional path to write the JSON report.

    Returns:
        Deterministic drift report dict.
    """
    cards = cards if cards is not None else load_cards()
    cooccurrence = build_cooccurrence_matrix(cards)
    strategy_scores = score_all_strategies(cards)

    global_entity_card_count = cooccurrence["entity_card_count"]
    bridge_concepts = _compute_bridge_concepts(cards)
    top_novel = _compute_top_novel_entities(cards, global_entity_card_count, limit=10)
    migration_candidates = _compute_cross_domain_migration_candidates(strategy_scores, cards)
    vocab_summary = _vocabulary_summary(cards)

    ranked_strategies = sorted(
        strategy_scores.values(),
        key=lambda s: (
            -s["composite_drift_score"],
            -s["frontier_ratio"],
            -s["bridge_ratio"],
            s["strategy_id"],
        ),
    )

    leading_indicator_strategies = [
        s["strategy_id"]
        for s in ranked_strategies
        if s["composite_drift_score"] > LEADING_INDICATOR_THRESHOLD
    ]

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "computed_at": utc_now(),
        "card_count": len(cards),
        "strategy_count": len(ranked_strategies),
        "unique_entities": cooccurrence["unique_entities"],
        "bridge_concept_count": len(bridge_concepts),
        "leading_indicator_count": len(leading_indicator_strategies),
        "leading_indicator_strategies": leading_indicator_strategies,
        "strategies": ranked_strategies,
        "top_novel_entities": top_novel,
        "cross_domain_migration_candidates": migration_candidates,
        "bridge_concepts": sorted(
            bridge_concepts.values(),
            key=lambda b: (-b["strategy_count"], -b["card_count"], b["name"]),
        ),
        "cooccurrence_matrix": {
            "total_cards": cooccurrence["total_cards"],
            "unique_entities": cooccurrence["unique_entities"],
            "pair_count": len(cooccurrence["pair_cooccurrence"]),
            "top_pairs": cooccurrence["top_pairs"],
            "top_pmi_pairs": cooccurrence["top_pmi_pairs"],
        },
        "vocabulary_summary": vocab_summary,
    }

    report["report_sha256"] = sha256_text(stable_json(report))

    if output_path is not None:
        atomic_json(output_path, report)

    return report


def generate_brief(report: dict[str, Any], top_n: int = 5) -> dict[str, Any]:
    """Generate a daily drift brief for the top N strategies.

    The brief highlights frontier analysis, bridge concepts, and the
    6-month leading-indicator prediction for each strategy.
    """
    strategies = sorted(
        report.get("strategies", []),
        key=lambda s: (
            -s["composite_drift_score"],
            -s["frontier_ratio"],
            -s["bridge_ratio"],
            s["strategy_id"],
        ),
    )[:top_n]

    brief_strategies: list[dict[str, Any]] = []
    for s in strategies:
        brief_strategies.append(
            {
                "rank": len(brief_strategies) + 1,
                "strategy_id": s["strategy_id"],
                "tier": s.get("tier", ""),
                "card_count": s["card_count"],
                "composite_drift_score": s["composite_drift_score"],
                "leading_indicator": s["leading_indicator"],
                "prediction": s["prediction"],
                "frontier": {
                    "frontier_ratio": s["frontier_ratio"],
                    "emerging_ratio": s["emerging_ratio"],
                    "established_ratio": s["established_ratio"],
                    "frontier_entity_names": s["frontier_entity_names"][:20],
                    "emerging_entity_names": s["emerging_entity_names"][:20],
                },
                "bridge": {
                    "bridge_concepts": s["bridge_concepts"],
                    "bridge_ratio": s["bridge_ratio"],
                    "bridge_concept_names": s["bridge_concept_names"][:20],
                },
                "velocity": {
                    "drift_velocity": s["drift_velocity"],
                    "newly_emerged_ratio": s["newly_emerged_ratio"],
                    "deprecated_ratio": s["deprecated_ratio"],
                    "newly_emerged_entity_names": s["newly_emerged_entity_names"][:20],
                },
                "cross_domain": {
                    "cross_domain_ratio": s["cross_domain_ratio"],
                    "cross_domain_entity_names": s["cross_domain_entity_names"][:20],
                },
                "ego_vector": s["ego_vector"][:10],
            }
        )

    return {
        "schema": "rig.omniscout.pattern-drift-brief.v1",
        "computed_at": report.get("computed_at"),
        "top_n": top_n,
        "brief_strategies": brief_strategies,
        "bridge_concepts_highlight": report.get("bridge_concepts", [])[:10],
        "leading_indicator_count": sum(1 for b in brief_strategies if b["leading_indicator"]),
    }


def _print_status(report: dict[str, Any]) -> None:
    """Print a concise human-readable status summary."""
    print("Epistemic Drift Report")
    print(f"  computed_at: {report.get('computed_at')}")
    print(f"  cards:       {report.get('card_count')}")
    print(f"  strategies:  {report.get('strategy_count')}")
    print(f"  entities:    {report.get('unique_entities')}")
    print(f"  bridge concepts: {report.get('bridge_concept_count')}")
    print(f"  leading indicators: {report.get('leading_indicator_count')}")
    print()
    print("Ranked strategies by composite drift score:")
    for s in report.get("strategies", []):
        flag = "★" if s.get("leading_indicator") else " "
        print(
            f"  {flag} {s['strategy_id']:<34} "
            f"composite={s['composite_drift_score']:.4f}  "
            f"frontier={s['frontier_entities']}/{s['total_entities']}  "
            f"bridge={s['bridge_concepts']}  "
            f"velocity={s['drift_velocity']:.4f}  "
            f"cross={s['cross_domain_ratio']:.2f}"
        )
    print()
    print("Top novel entities:")
    for ent in report.get("top_novel_entities", []):
        print(f"  [{ent['type']}] {ent['name']}  ({ent['strategy_id']})")
    print()
    print("Bridge concepts (3+ strategies):")
    for ent in report.get("bridge_concepts", [])[:10]:
        print(
            f"  [{ent['type']}] {ent['name']}  "
            f"cards={ent['card_count']}  "
            f"strategies={ent['strategy_count']}  "
            f"in={', '.join(ent['strategies'])}"
        )


def _print_brief(brief: dict[str, Any]) -> None:
    """Print the daily drift brief in a readable format."""
    print("=" * 70)
    print("DAILY DRIFT BRIEF")
    print(f"  computed_at: {brief.get('computed_at')}")
    print(f"  top strategies: {brief.get('top_n')}")
    print(f"  leading indicators: {brief.get('leading_indicator_count')}")
    print("=" * 70)

    for s in brief.get("brief_strategies", []):
        print()
        print(
            f"#{s['rank']} {s['strategy_id']} "
            f"(composite={s['composite_drift_score']:.4f}) "
            f"{'★ LEADING INDICATOR' if s['leading_indicator'] else ''}"
        )
        print(f"  tier={s['tier']}  cards={s['card_count']}")
        print(f"  prediction: {s['prediction']}")
        print(
            f"  frontier={s['frontier']['frontier_ratio']:.2f} "
            f"emerging={s['frontier']['emerging_ratio']:.2f} "
            f"established={s['frontier']['established_ratio']:.2f}"
        )
        print(
            f"  bridge_concepts={s['bridge']['bridge_concepts']} "
            f"bridge_ratio={s['bridge']['bridge_ratio']:.2f}"
        )
        print(
            f"  drift_velocity={s['velocity']['drift_velocity']:.2f} "
            f"cross_domain={s['cross_domain']['cross_domain_ratio']:.2f}"
        )
        if s["frontier"]["frontier_entity_names"]:
            print(
                "  frontier entities: "
                + ", ".join(s["frontier"]["frontier_entity_names"][:10])
            )
        if s["bridge"]["bridge_concept_names"]:
            print(
                "  bridge concepts: "
                + ", ".join(s["bridge"]["bridge_concept_names"][:10])
            )
        if s["velocity"]["newly_emerged_entity_names"]:
            print(
                "  newly emerged: "
                + ", ".join(s["velocity"]["newly_emerged_entity_names"][:10])
            )

    print()
    print("Cross-strategy bridge concepts:")
    for ent in brief.get("bridge_concepts_highlight", []):
        print(
            f"  [{ent['type']}] {ent['name']} — "
            f"{ent['strategy_count']} strategies, {ent['card_count']} cards"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Epistemic Drift engine for V30 build cards"
    )
    parser.add_argument(
        "command",
        choices=["all", "brief", "status"],
        default="status",
        nargs="?",
        help="all: write full JSON report; brief: print daily drift brief; status: print concise summary",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override path for the JSON report (default: L2_ROOT/pattern-drift.json)",
    )
    parser.add_argument(
        "--brief-output",
        type=Path,
        default=None,
        help="Override path for the brief JSON (default: L2_ROOT/pattern-drift-brief.json)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Number of strategies in the brief (default: 5)",
    )
    args = parser.parse_args(argv)

    output_path = args.output or (L2_ROOT / "pattern-drift.json")
    brief_output_path = args.brief_output or (L2_ROOT / "pattern-drift-brief.json")

    report = compute_drift(output_path=output_path)
    brief = generate_brief(report, top_n=args.top_n)

    if args.command == "all":
        atomic_json(brief_output_path, brief)
        print(f"Wrote pattern drift report to {output_path}")
        print(f"Wrote drift brief to {brief_output_path}")
        print(f"Report SHA256: {report.get('report_sha256')}")
    elif args.command == "brief":
        _print_brief(brief)
        atomic_json(brief_output_path, brief)
        print(f"\nBrief written to: {brief_output_path}")
    else:
        _print_status(report)
        print(f"\nReport written to: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
