"""Contradiction Arbitrage Rank (CAR) engine — 1000x upgrade.

Builds a proper contradiction graph, computes PageRank centrality over the
entity-contradiction network, enriches each contradiction with 7+ score
dimensions, predicts breakthrough probability / direction / time-to-resolution,
tracks resolution state across runs, and emits a daily breakthrough brief.

All deterministic; no LLM calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
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
except ImportError:  # pragma: no cover - supports direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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

SCHEMA_PAIR = "rig.foundry.pattern-contradiction.v2"
SCHEMA_ENTRIES = "rig.foundry.pattern-contradiction-entries.v2"
SCHEMA_BRIEF = "rig.foundry.pattern-contradiction-brief.v2"
OUTPUT_PATH = L2_ROOT / "pattern-contradiction.json"
STATE_PATH = L2_ROOT / "pattern-contradiction-state.json"

STABILITY_PENALTY = 0.5
AGE_CAP_DAYS = 3.0
VELOCITY_CAP = 1.0
PAGERANK_ITERATIONS = 40
PAGERANK_DAMPING = 0.85
CONFIDENCE_HIGH_EVIDENCE = 5
CONFIDENCE_MED_EVIDENCE = 3


def load_cards(cards_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load all L2 build cards from disk."""
    directory = cards_dir or L2_CARDS
    cards: list[dict[str, Any]] = []
    if not directory.exists():
        return cards
    for path in directory.glob("l2-*.json"):
        try:
            with path.open("r", encoding="utf-8") as fh:
                cards.append(json.load(fh))
        except (json.JSONDecodeError, OSError):
            continue
    return cards


def _card_age_days(card: dict[str, Any], now: datetime | None = None) -> float:
    """Days since card creation or last enrichment, floored at 0."""
    now = now or datetime.now(timezone.utc)
    for key in ("enriched_v30_at", "enriched_at", "created_at"):
        ts = card.get(key)
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = now - dt
            return max(0.0, delta.total_seconds() / 86400.0)
        except (ValueError, TypeError):
            continue
    return 0.0


def _parse_iso_age_days(ts: str, now: datetime | None = None) -> float:
    """Days since an ISO timestamp, floored at 0."""
    now = now or datetime.now(timezone.utc)
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 0.0


def _evidence(card: dict[str, Any]) -> float:
    """Evidence strength in [0, 1] from card score total."""
    score = card.get("score") or {}
    return (score.get("total") or 0) / 100.0


def _evidence_count(card: dict[str, Any]) -> int:
    """Count evidence-bearing URLs / items attached to a card."""
    sources = card.get("sources") or {}
    evidence = card.get("evidence") or []
    consensus = card.get("consensus") or {}
    return max(
        len(sources.get("urls") or []),
        len(evidence),
        len(consensus.get("results") or []),
    )


def _consensus_count(card: dict[str, Any]) -> int:
    """Number of consensus results attached to a card."""
    consensus = card.get("consensus") or {}
    return int(consensus.get("count") or 0)


def _strategy_id(card: dict[str, Any]) -> str:
    """Return the card's strategy_id, defaulting to 'unknown'."""
    strategy = card.get("strategy") or {}
    return strategy.get("strategy_id") or "unknown"


def _doctrine_domains(card: dict[str, Any]) -> set[str]:
    """Doctrine domains attached to a card."""
    return set(card.get("doctrine_domains") or [])


def _card_entities(card: dict[str, Any]) -> set[str]:
    """Return entity names attached to a card."""
    entities_block = card.get("entities") or {}
    return {
        e.get("name", "")
        for e in (entities_block.get("entities") or [])
        if e.get("name")
    }


def _parse_ltv(value: Any) -> float:
    """Parse estimated_ltv string like '$60K-240K' into a dollar midpoint."""
    if value is None:
        return 0.0
    s = str(value).lower().replace(",", "")
    nums: list[float] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([kmb]?)", s):
        n = float(m.group(1))
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "": 1}.get(m.group(2), 1)
        nums.append(n * mult)
    if not nums:
        return 0.0
    return sum(nums) / len(nums)


def _ltv(card: dict[str, Any]) -> float:
    """Estimated lifetime value in dollars for a card."""
    bi = card.get("business_intelligence") or {}
    return _parse_ltv(bi.get("estimated_ltv"))


def _newest_evidence_age_days(card: dict[str, Any], now: datetime | None = None) -> float:
    """Age of the newest piece of evidence (days); 0 if none found."""
    now = now or datetime.now(timezone.utc)
    ages: list[float] = []
    for ev in card.get("evidence") or []:
        if isinstance(ev, dict) and ev.get("captured_at"):
            ages.append(_parse_iso_age_days(ev["captured_at"], now))
        elif isinstance(ev, dict) and ev.get("year"):
            try:
                years = now.year - int(ev["year"])
                ages.append(max(0.0, years * 365.25))
            except (ValueError, TypeError):
                pass
    for res in (card.get("consensus") or {}).get("results") or []:
        if isinstance(res, dict) and res.get("year"):
            try:
                years = now.year - int(res["year"])
                ages.append(max(0.0, years * 365.25))
            except (ValueError, TypeError):
                pass
    if ages:
        return min(ages)
    return _card_age_days(card, now)


def _normalize_log(value: float, max_value: float) -> float:
    """Normalize using log1p against the observed maximum."""
    if max_value <= 0:
        return 0.0
    return math.log1p(value) / math.log1p(max_value)


def _normalize_linear(value: float, max_value: float) -> float:
    """Linear normalize capped at 1.0."""
    if max_value <= 0:
        return 0.0
    return min(1.0, value / max_value)


def _pair_id(card_a: str, card_b: str, reason: str = "") -> str:
    """Stable identifier for a contradiction pair / entry."""
    parts = sorted([card_a, card_b])
    payload = stable_json({"pair": parts, "reason": reason})
    return sha256_text(payload)[:16]


def _build_entity_graph(
    cards: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Build an undirected entity graph from contradiction relationships.

    Nodes are entity names. Edges connect entities that participate in the
    same contradiction relationship (card_a entities ↔ card_b entities).
    Edge weights are incremented per contradiction entry and scaled by the
    raw evidence scores of the two cards.
    """
    graph: dict[str, dict[str, Any]] = {"nodes": set(), "edges": defaultdict(float)}
    for card in cards:
        cid = card.get("card_id")
        if not cid:
            continue
        semantic = card.get("semantic_links") or {}
        entities_a = _card_entities(card)
        for contra in semantic.get("contradictions") or []:
            target_id = contra.get("card_id")
            if not target_id or target_id not in by_id:
                continue
            target_card = by_id[target_id]
            entities_b = _card_entities(target_card)
            weight = (_evidence(card) + _evidence(target_card)) / 2.0 + 0.05
            for ea in entities_a:
                graph["nodes"].add(ea)
                for eb in entities_b:
                    graph["nodes"].add(eb)
                    key = tuple(sorted((ea, eb)))
                    graph["edges"][key] += weight
    return graph


def _pagerank(
    graph: dict[str, dict[str, Any]],
    iterations: int = PAGERANK_ITERATIONS,
    damping: float = PAGERANK_DAMPING,
) -> dict[str, float]:
    """Compute PageRank centrality for every entity node in the graph."""
    nodes = list(graph.get("nodes") or [])
    if not nodes:
        return {}
    edges = graph.get("edges") or {}

    adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
    degrees: dict[str, float] = defaultdict(float)
    for (a, b), w in edges.items():
        adjacency[a].append((b, w))
        adjacency[b].append((a, w))
        degrees[a] += w
        degrees[b] += w

    n = len(nodes)
    rank: dict[str, float] = {node: 1.0 / n for node in nodes}
    base = (1.0 - damping) / n

    for _ in range(iterations):
        new_rank: dict[str, float] = {}
        for node in nodes:
            incoming = 0.0
            for neighbor, weight in adjacency.get(node, []):
                denom = degrees.get(neighbor, 1.0)
                if denom > 0:
                    incoming += damping * rank.get(neighbor, 0.0) * weight / denom
            new_rank[node] = base + incoming
        rank = new_rank

    return rank


def _contradiction_clusters(
    pairs: list[tuple[str, str]],
    by_id: dict[str, dict[str, Any]],
) -> dict[tuple[str, str], int]:
    """Assign each pair to a connected component cluster over entity graph."""
    if not pairs:
        return {}

    entity_index: dict[str, int] = {}
    uf = _UnionFind()

    def ensure(entity: str) -> None:
        if entity not in entity_index:
            entity_index[entity] = uf.add()

    for card_a, card_b in pairs:
        entities_a = _card_entities(by_id[card_a])
        entities_b = _card_entities(by_id[card_b])
        all_entities = list(entities_a | entities_b)
        if not all_entities:
            continue
        for e in all_entities:
            ensure(e)
        root = entity_index[all_entities[0]]
        for e in all_entities[1:]:
            uf.union(root, entity_index[e])

    pair_cluster: dict[tuple[str, str], int] = {}
    for pair in pairs:
        entities = list(_card_entities(by_id[pair[0]]) | _card_entities(by_id[pair[1]]))
        if entities:
            pair_cluster[pair] = uf.find(entity_index[entities[0]])
        else:
            pair_cluster[pair] = -1
    return pair_cluster


class _UnionFind:
    """Tiny union-find for clustering."""

    def __init__(self) -> None:
        self.parent: list[int] = []

    def add(self) -> int:
        idx = len(self.parent)
        self.parent.append(idx)
        return idx

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_contradiction_graph(
    cards: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build an undirected contradiction graph from card semantic_links.

    Returns a mapping from sorted (card_a, card_b) pair to metadata including
    touching neighborhood, inbound semantic-link centrality, entity overlap,
    and every directed reason recorded for the pair.
    """
    by_id = {c["card_id"]: c for c in cards if c.get("card_id")}

    outgoing: dict[str, set[str]] = defaultdict(set)
    incoming: dict[str, set[str]] = defaultdict(set)
    for card in cards:
        cid = card.get("card_id")
        if not cid:
            continue
        semantic = card.get("semantic_links") or {}
        for link in semantic.get("links") or []:
            target = link.get("card_id")
            if target and target in by_id:
                outgoing[cid].add(target)
                incoming[target].add(cid)

    graph: dict[tuple[str, str], dict[str, Any]] = {}
    for card in cards:
        cid = card.get("card_id")
        if not cid:
            continue
        semantic = card.get("semantic_links") or {}
        for contra in semantic.get("contradictions") or []:
            target_id = contra.get("card_id")
            if not target_id or target_id not in by_id:
                continue
            pair = tuple(sorted((cid, target_id)))
            if pair in graph:
                graph[pair]["reasons"].append(contra.get("reason", ""))
                graph[pair]["directed"].append(
                    {"source": cid, "target": target_id, "reason": contra.get("reason", "")}
                )
                continue

            touching: set[str] = set(pair)
            for pid in pair:
                touching.update(outgoing.get(pid, set()))
                touching.update(incoming.get(pid, set()))

            entities_a = _card_entities(card)
            entities_b = _card_entities(by_id[target_id])
            overlap = entities_a & entities_b

            graph[pair] = {
                "pair": pair,
                "touching": touching,
                "touching_count": len(touching),
                "inbound_links": len(incoming.get(pair[0], set()))
                + len(incoming.get(pair[1], set())),
                "reasons": [contra.get("reason", "")],
                "directed": [
                    {"source": cid, "target": target_id, "reason": contra.get("reason", "")}
                ],
                "overlap": overlap,
                "overlap_count": len(overlap),
                "entities_a": entities_a,
                "entities_b": entities_b,
            }

    return graph


def _resolve_state(car: float, evidence_a: float, evidence_b: float) -> str:
    """Map CAR + evidence tilt to resolution state machine state."""
    gap = abs(evidence_a - evidence_b)
    if car >= 0.85 or gap >= 0.5:
        return "resolved"
    if car >= 0.65:
        if evidence_a > evidence_b + 0.08:
            return "tilting_a"
        if evidence_b > evidence_a + 0.08:
            return "tilting_b"
        return "active"
    if car >= 0.35:
        return "active"
    return "dormant"


def _next_state(current: str, car: float, evidence_a: float, evidence_b: float) -> str:
    """State machine: active → tilting_a/b → resolved → synthesized."""
    base = _resolve_state(car, evidence_a, evidence_b)
    if current == "synthesized":
        return "synthesized"
    if base == "resolved" and current in ("resolved", "tilting_a", "tilting_b"):
        return "synthesized"
    return base


def _cross_domain_factor(card_a: dict[str, Any], card_b: dict[str, Any]) -> float:
    """Return multiplier >1 when contradicting cards span strategy/domain."""
    sid_a = _strategy_id(card_a)
    sid_b = _strategy_id(card_b)
    if sid_a != "unknown" and sid_b != "unknown" and sid_a != sid_b:
        return 1.4
    domains_a = _doctrine_domains(card_a)
    domains_b = _doctrine_domains(card_b)
    if domains_a and domains_b and not domains_a.intersection(domains_b):
        return 1.25
    return 1.0


def _confidence_level(
    car: float, evidence_a: int, evidence_b: int, pagerank: float
) -> str:
    """Low / medium / high certainty for the prediction."""
    total_evidence = evidence_a + evidence_b
    if car > 0.6 and total_evidence >= CONFIDENCE_HIGH_EVIDENCE and pagerank > 0.5:
        return "high"
    if car > 0.4 and total_evidence >= CONFIDENCE_MED_EVIDENCE:
        return "medium"
    return "low"


def _time_to_resolution_weeks(
    car: float, velocity: float, evidence_gap: float, state: str
) -> int:
    """Estimated weeks until one side wins."""
    if state in ("resolved", "synthesized"):
        return 0
    base = (1.0 - min(1.0, car)) * 16.0
    speed = 1.0 + min(velocity, VELOCITY_CAP)
    gap_boost = 1.0 + evidence_gap
    weeks = max(1.0, base / (speed * gap_boost))
    return int(math.ceil(weeks))


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def compute_car(
    cards: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute enriched CAR for each unique contradiction pair.

    Builds a real entity-contradiction graph, computes PageRank centrality,
    and scores each pair across 7+ dimensions: Density, Evidence tension,
    Centrality, Paper velocity, Revenue potential, Temporal pressure, and
    Cross-domain multiplier.
    """
    cards = cards if cards is not None else load_cards()
    now = now or datetime.now(timezone.utc)
    by_id = {c["card_id"]: c for c in cards if c.get("card_id")}

    graph = build_contradiction_graph(cards)
    if not graph:
        return {
            "schema": SCHEMA_PAIR,
            "pairs": [],
            "entity_graph": {"node_count": 0, "edge_count": 0, "pagerank": {}},
            "clusters": {},
            "metadata": {
                "count": 0,
                "max_car": 0.0,
                "breakthrough_count": 0,
                "tilting_count": 0,
                "active_count": 0,
                "dormant_count": 0,
                "resolved_count": 0,
                "synthesized_count": 0,
                "computed_at": utc_now(),
            },
        }

    entity_graph = _build_entity_graph(cards, by_id)
    pagerank = _pagerank(entity_graph)
    pair_clusters = _contradiction_clusters(list(graph.keys()), by_id)

    max_density = max(len(data["touching"]) for data in graph.values())
    max_centrality = max(data["inbound_links"] for data in graph.values())
    max_ltv = max(
        (_ltv(by_id[pair[0]]) + _ltv(by_id[pair[1]])) for pair in graph.keys()
    ) or 1.0

    raw_pairs: list[dict[str, Any]] = []
    for pair, data in graph.items():
        card_a = by_id[pair[0]]
        card_b = by_id[pair[1]]

        evidence_a = _evidence(card_a)
        evidence_b = _evidence(card_b)
        evidence_tension = min(evidence_a, evidence_b) * abs(evidence_a - evidence_b)

        density = _normalize_log(len(data["touching"]), max_density)
        centrality = data["inbound_links"] / max_centrality if max_centrality > 0 else 0.0

        entities_all = data["entities_a"] | data["entities_b"]
        entity_pagerank_values = [pagerank.get(e, 0.0) for e in entities_all]
        mean_pagerank = (
            sum(entity_pagerank_values) / len(entity_pagerank_values)
            if entity_pagerank_values
            else 0.0
        )

        velocity = (
            _consensus_count(card_a)
            + _consensus_count(card_b)
            + _evidence_count(card_a)
            + _evidence_count(card_b)
        ) / 20.0
        velocity = min(velocity, VELOCITY_CAP)

        revenue = _ltv(card_a) + _ltv(card_b)
        revenue_norm = _normalize_linear(revenue, max_ltv)

        age_a = _newest_evidence_age_days(card_a, now)
        age_b = _newest_evidence_age_days(card_b, now)
        newest_age = min(age_a, age_b)
        temporal_pressure = math.exp(-newest_age / 30.0)

        cross_domain = _cross_domain_factor(card_a, card_b)

        denominator = (newest_age / 90.0) + STABILITY_PENALTY
        if denominator <= 0:
            raw_car = 0.0
        else:
            raw_car = (
                density
                * (evidence_tension + 0.05)
                * (centrality + 0.05)
                * (mean_pagerank + 0.05)
                * (velocity + 0.05)
                * (revenue_norm + 0.05)
                * temporal_pressure
                * cross_domain
            ) / denominator

        cluster_id = pair_clusters.get(pair, -1)

        raw_pairs.append(
            {
                "pair": list(pair),
                "card_a": pair[0],
                "card_b": pair[1],
                "raw_car": float(raw_car),
                "components": {
                    "D": round(density, 6),
                    "E": round(evidence_tension, 6),
                    "C_link": round(centrality, 6),
                    "C_page": round(mean_pagerank, 6),
                    "V": round(velocity, 6),
                    "R": round(revenue_norm, 6),
                    "T": round(temporal_pressure, 6),
                    "X": round(cross_domain, 6),
                },
                "touching_count": len(data["touching"]),
                "inbound_links": data["inbound_links"],
                "entity_count": len(entities_all),
                "overlap_count": data["overlap_count"],
                "cluster_id": cluster_id,
                "reasons": [r for r in data["reasons"] if r],
                "directed": data["directed"],
                "entities": sorted(entities_all),
                "strategy_ids": sorted({_strategy_id(card_a), _strategy_id(card_b)}),
                "evidence": {
                    "a": round(evidence_a, 4),
                    "b": round(evidence_b, 4),
                    "a_count": _evidence_count(card_a),
                    "b_count": _evidence_count(card_b),
                },
            }
        )

    raw_values = [p["raw_car"] for p in raw_pairs]
    raw_min = min(raw_values)
    raw_max = max(raw_values)
    raw_range = raw_max - raw_min

    pairs: list[dict[str, Any]] = []
    for p in raw_pairs:
        if raw_range > 0:
            car = (p["raw_car"] - raw_min) / raw_range
        else:
            car = 0.0
        car = max(0.0, min(1.0, car))

        evidence_a = p["evidence"]["a"]
        evidence_b = p["evidence"]["b"]
        state = _resolve_state(car, evidence_a, evidence_b)
        evidence_gap = abs(evidence_a - evidence_b)
        breakthrough_prob = _sigmoid(
            car * p["components"]["X"] * p["components"]["T"] * 6.0
        )

        if evidence_a > evidence_b + 0.05:
            predicted_direction = pair[0]
            direction_label = f"{pair[0]} gaining"
        elif evidence_b > evidence_a + 0.05:
            predicted_direction = pair[1]
            direction_label = f"{pair[1]} gaining"
        else:
            predicted_direction = None
            direction_label = "stalemate"

        mean_pagerank = p["components"]["C_page"]
        confidence = _confidence_level(
            car,
            p["evidence"]["a_count"],
            p["evidence"]["b_count"],
            mean_pagerank,
        )
        weeks = _time_to_resolution_weeks(car, p["components"]["V"], evidence_gap, state)

        pairs.append(
            {
                **p,
                "car": round(car, 6),
                "state": state,
                "breakthrough": {
                    "probability": round(breakthrough_prob, 6),
                    "predicted_direction": predicted_direction,
                    "direction_label": direction_label,
                    "confidence": confidence,
                    "time_to_resolution_weeks": weeks,
                },
            }
        )

    pairs.sort(key=lambda item: item["car"], reverse=True)

    clusters: dict[int, dict[str, Any]] = defaultdict(lambda: {"pairs": [], "entities": set()})
    for p in pairs:
        cid = p["cluster_id"]
        if cid < 0:
            continue
        clusters[cid]["pairs"].append(p["pair"])
        clusters[cid]["entities"].update(p["entities"])

    clusters_out = {
        str(k): {
            "pair_count": len(v["pairs"]),
            "entity_count": len(v["entities"]),
            "pairs": v["pairs"],
            "entities": sorted(v["entities"]),
        }
        for k, v in clusters.items()
    }

    return {
        "schema": SCHEMA_PAIR,
        "pairs": pairs,
        "entity_graph": {
            "node_count": len(entity_graph["nodes"]),
            "edge_count": len(entity_graph["edges"]),
            "pagerank": {k: round(v, 8) for k, v in sorted(pagerank.items())},
            "top_entities": sorted(
                pagerank.items(), key=lambda kv: kv[1], reverse=True
            )[:20],
        },
        "clusters": clusters_out,
        "metadata": {
            "count": len(pairs),
            "raw_min": round(raw_min, 6),
            "raw_max": round(raw_max, 6),
            "max_car": round(pairs[0]["car"], 6) if pairs else 0.0,
            "breakthrough_count": sum(1 for p in pairs if p["car"] > 0.7),
            "tilting_count": sum(
                1 for p in pairs if p["state"] in ("tilting_a", "tilting_b")
            ),
            "active_count": sum(1 for p in pairs if p["state"] == "active"),
            "dormant_count": sum(1 for p in pairs if p["state"] == "dormant"),
            "resolved_count": sum(1 for p in pairs if p["state"] == "resolved"),
            "synthesized_count": sum(1 for p in pairs if p["state"] == "synthesized"),
            "computed_at": utc_now(),
        },
    }


def score_all_contradictions(
    cards: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Score every directed contradiction entry, attaching the unique-pair CAR.

    Returns one record per ``semantic_links.contradictions`` item, preserving
    the original direction so downstream callers see every contradiction scored.
    """
    cards = cards if cards is not None else load_cards()
    now = now or datetime.now(timezone.utc)
    by_id = {c["card_id"]: c for c in cards if c.get("card_id")}

    car_result = compute_car(cards, now=now)
    car_by_pair = {tuple(p["pair"]): p for p in car_result["pairs"]}

    scored: list[dict[str, Any]] = []
    for card in cards:
        cid = card.get("card_id")
        if not cid:
            continue
        semantic = card.get("semantic_links") or {}
        for contra in semantic.get("contradictions") or []:
            target_id = contra.get("card_id")
            if not target_id or target_id not in by_id:
                continue
            pair = tuple(sorted((cid, target_id)))
            base = car_by_pair.get(pair)
            if not base:
                continue

            target_card = by_id[target_id]
            pid = _pair_id(cid, target_id, contra.get("reason", ""))
            scored.append(
                {
                    "entry_id": pid,
                    "source_card": cid,
                    "target_card": target_id,
                    "pair": list(pair),
                    "car": base["car"],
                    "state": base["state"],
                    "components": base["components"],
                    "breakthrough": base["breakthrough"],
                    "reason": contra.get("reason", ""),
                    "entities": sorted(_card_entities(card) | _card_entities(target_card)),
                    "strategy_ids": sorted(
                        {_strategy_id(card), _strategy_id(target_card)}
                    ),
                    "cluster_id": base.get("cluster_id"),
                }
            )

    scored.sort(key=lambda item: item["car"], reverse=True)

    return {
        "schema": SCHEMA_ENTRIES,
        "contradictions": scored,
        "metadata": {
            "count": len(scored),
            "unique_pair_count": len(car_by_pair),
            "max_car": round(scored[0]["car"], 6) if scored else 0.0,
            "breakthrough_count": sum(
                1 for s in scored if s["breakthrough"]["probability"] > 0.7
            ),
            "tilting_count": sum(
                1 for s in scored if s["state"] in ("tilting_a", "tilting_b")
            ),
            "active_count": sum(1 for s in scored if s["state"] == "active"),
            "dormant_count": sum(1 for s in scored if s["state"] == "dormant"),
            "resolved_count": sum(1 for s in scored if s["state"] == "resolved"),
            "synthesized_count": sum(1 for s in scored if s["state"] == "synthesized"),
            "computed_at": utc_now(),
        },
    }


def _load_previous_state(path: Path = STATE_PATH) -> dict[str, Any]:
    """Load the previous contradiction snapshot for shift tracking."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(
    car_result: dict[str, Any], path: Path = STATE_PATH
) -> None:
    """Persist a minimal snapshot of current contradictions for next run."""
    snapshot: dict[str, Any] = {
        "schema": "rig.foundry.pattern-contradiction-state.v1",
        "computed_at": utc_now(),
        "pairs": {},
    }
    for p in car_result.get("pairs", []):
        key = stable_json(sorted(p["pair"]))
        snapshot["pairs"][sha256_text(key)[:16]] = {
            "pair": sorted(p["pair"]),
            "car": p["car"],
            "state": p["state"],
            "evidence_a": p["evidence"]["a"],
            "evidence_b": p["evidence"]["b"],
            "breakthrough_probability": p["breakthrough"]["probability"],
        }
    atomic_json(path, snapshot)


def _track_resolution_shifts(
    current: dict[str, Any], previous: dict[str, Any]
) -> dict[str, Any]:
    """Detect new, resolved, and shifted contradictions between runs."""
    prev_pairs = previous.get("pairs") or {}
    prev_keys = set(prev_pairs.keys())

    curr: dict[str, Any] = {}
    for p in current.get("pairs", []):
        key = sha256_text(stable_json(sorted(p["pair"])))[:16]
        curr[key] = p
    curr_keys = set(curr.keys())

    new_keys = curr_keys - prev_keys
    resolved_keys = prev_keys - curr_keys

    shifts: list[dict[str, Any]] = []
    for key in curr_keys & prev_keys:
        c = curr[key]
        pr = prev_pairs[key]
        car_delta = round(c["car"] - pr.get("car", 0.0), 6)
        if abs(car_delta) >= 0.05 or c["state"] != pr.get("state"):
            shifts.append(
                {
                    "pair": sorted(c["pair"]),
                    "car_delta": car_delta,
                    "old_state": pr.get("state"),
                    "new_state": c["state"],
                    "old_breakthrough_probability": pr.get("breakthrough_probability"),
                    "new_breakthrough_probability": c["breakthrough"]["probability"],
                }
            )

    return {
        "new": [curr[k]["pair"] for k in new_keys],
        "resolved": [prev_pairs[k]["pair"] for k in resolved_keys],
        "shifts": shifts,
        "new_count": len(new_keys),
        "resolved_count": len(resolved_keys),
        "shift_count": len(shifts),
    }


def generate_brief(
    car_result: dict[str, Any] | None = None,
    entries_result: dict[str, Any] | None = None,
    *,
    top_n: int = 5,
) -> dict[str, Any]:
    """Generate the daily breakthrough brief from CAR results."""
    if car_result is None:
        car_result = compute_car()
    if entries_result is None:
        entries_result = score_all_contradictions()

    top_pairs = car_result["pairs"][:top_n]
    brief_items: list[dict[str, Any]] = []
    for p in top_pairs:
        brief_items.append(
            {
                "rank": len(brief_items) + 1,
                "pair": sorted(p["pair"]),
                "car": p["car"],
                "state": p["state"],
                "breakthrough_probability": p["breakthrough"]["probability"],
                "predicted_direction": p["breakthrough"]["predicted_direction"],
                "direction_label": p["breakthrough"]["direction_label"],
                "confidence": p["breakthrough"]["confidence"],
                "time_to_resolution_weeks": p["breakthrough"][
                    "time_to_resolution_weeks"
                ],
                "score_components": p["components"],
                "entities": p["entities"][:10],
                "strategies": p["strategy_ids"],
                "reasons": p["reasons"][:3],
            }
        )

    previous = _load_previous_state()
    shifts = _track_resolution_shifts(car_result, previous)
    _save_state(car_result)

    return {
        "schema": SCHEMA_BRIEF,
        "date": utc_now(),
        "top_n": top_n,
        "summary": {
            "total_pairs": car_result["metadata"]["count"],
            "breakthrough_candidates": car_result["metadata"]["breakthrough_count"],
            "active": car_result["metadata"]["active_count"],
            "tilting": car_result["metadata"]["tilting_count"],
            "resolved": car_result["metadata"]["resolved_count"],
            "new_contradictions": shifts["new_count"],
            "resolved_contradictions": shifts["resolved_count"],
            "shifts": shifts["shift_count"],
        },
        "shifts": shifts,
        "top": brief_items,
    }


def write_results(result: dict[str, Any], path: Path | None = None) -> Path:
    """Persist CAR results to L2_ROOT/pattern-contradiction.json."""
    output_path = path or OUTPUT_PATH
    atomic_json(output_path, result)
    return output_path


def _format_pair(pair: dict[str, Any]) -> str:
    b = pair["breakthrough"]
    return (
        f"{pair['car']:.4f} {pair['state']:<12} "
        f"P={b['probability']:.2f} {b['confidence']:<6} "
        f"{b['direction_label']:<28} ~{b['time_to_resolution_weeks']:>2}w  "
        f"{pair['card_a']} <-> {pair['card_b']}"
    )


def _format_brief(brief: dict[str, Any]) -> str:
    lines = [
        "Daily Breakthrough Brief — Contradictions",
        f"Generated: {brief['date']}",
        "",
        "Summary:",
        f"  total pairs:        {brief['summary']['total_pairs']}",
        f"  breakthrough cand.: {brief['summary']['breakthrough_candidates']}",
        f"  active:             {brief['summary']['active']}",
        f"  tilting:            {brief['summary']['tilting']}",
        f"  resolved:           {brief['summary']['resolved']}",
        f"  new this run:       {brief['summary']['new_contradictions']}",
        f"  resolved this run:  {brief['summary']['resolved_contradictions']}",
        "",
        "Top 5 contradictions:",
    ]
    for item in brief["top"]:
        lines.append(
            f"  #{item['rank']} CAR={item['car']:.4f} state={item['state']} "
            f"Pbreak={item['breakthrough_probability']:.2f} "
            f"conf={item['confidence']} direction={item['direction_label']} "
            f"~{item['time_to_resolution_weeks']}w"
        )
        lines.append(f"      pair: {item['pair']}")
        lines.append(f"      strategies: {item['strategies']}")
        lines.append(f"      entities: {item['entities']}")
        if item["reasons"]:
            lines.append(f"      reasons: {item['reasons'][0]}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Contradiction Arbitrage Rank engine (1000x upgrade)"
    )
    parser.add_argument(
        "command",
        choices=("all", "top", "brief", "status"),
        default="status",
        nargs="?",
        help="Output mode",
    )
    parser.add_argument(
        "--cards-dir",
        type=Path,
        help="Override directory containing l2-*.json cards",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Override output JSON path",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=5,
        help="Number of items in brief/top output",
    )
    args = parser.parse_args(argv)

    cards = load_cards(args.cards_dir)
    car_result = compute_car(cards)
    entries_result = score_all_contradictions(cards)

    combined: dict[str, Any] = {
        "schema": SCHEMA_PAIR,
        "pairs": car_result["pairs"],
        "contradictions": entries_result["contradictions"],
        "entity_graph": car_result["entity_graph"],
        "clusters": car_result["clusters"],
        "metadata": {
            **car_result["metadata"],
            "unique_pair_count": car_result["metadata"]["count"],
            "contradiction_entry_count": entries_result["metadata"]["count"],
        },
    }
    write_results(combined, args.output)

    if args.command == "status":
        meta = combined["metadata"]
        print(f"Contradiction entries scored: {meta['contradiction_entry_count']}")
        print(f"Unique pairs scored:          {meta['unique_pair_count']}")
        print(f"Entity graph nodes:           {combined['entity_graph']['node_count']}")
        print(f"Entity graph edges:           {combined['entity_graph']['edge_count']}")
        print(f"Breakthrough (>0.7): {meta['breakthrough_count']}")
        print(f"Tilting:             {meta['tilting_count']}")
        print(f"Active (>0.3):       {meta['active_count']}")
        print(f"Dormant:             {meta['dormant_count']}")
        print(f"Resolved:            {meta['resolved_count']}")
        print(f"Synthesized:         {meta['synthesized_count']}")
        print(f"Max CAR:             {meta['max_car']}")
    elif args.command == "top":
        candidates = car_result["pairs"][: args.top_n]
        print(f"Top {args.top_n} contradiction candidates:")
        for pair in candidates:
            print("  " + _format_pair(pair))
    elif args.command == "brief":
        brief = generate_brief(car_result, entries_result, top_n=args.top_n)
        print(_format_brief(brief))
    elif args.command == "all":
        for pair in car_result["pairs"]:
            print(_format_pair(pair))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
