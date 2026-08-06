"""OmniScout L2 meta-synthesis engine.

Detects emergent patterns across the 130-card knowledge graph without LLM calls.
Exports detect_clusters, generate_meta_cards, identify_gaps, and a CLI main().
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rig_foundry.omniscout_build_cards import (
    DOCTRINE_DOMAINS,
    L2_CARDS,
    L2_ROOT,
    atomic_json,
    atomic_text,
    sha256_text,
    stable_json,
    utc_now,
)

SCHEMA_META = "rig.omniscout.meta-card.v1"
META_DIR = L2_ROOT / "meta-cards"

_STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "had", "her", "was",
    "one", "our", "out", "day", "get", "has", "him", "his", "how", "man", "new", "now",
    "old", "see", "two", "way", "who", "boy", "did", "its", "let", "put", "say", "she",
    "too", "use", "rig", "that", "with", "have", "this", "will", "your", "from", "they",
    "know", "want", "been", "good", "much", "some", "time", "very", "when", "come", "here",
    "just", "like", "long", "make", "many", "over", "such", "take", "than", "them", "well",
    "were", "what", "would", "there", "their", "said", "each", "which", "how", "about",
    "into", "only", "other", "these", "after", "back", "could", "first", "from", "more",
    "most", "never", "those", "while", "where", "being", "every", "great", "might", "shall",
    "still", "under", "without", "should", "through", "between", "before", "after", "above",
    "below", "during", "within", "across", "around", "because", "however", "therefore",
    "system", "agent", "agents", "model", "models", "data", "build", "card", "cards",
    "strategy", "strategies", "source", "sources", "using", "based", "based", "approach",
    # Corpus-generic terms that collapse the graph into a single component.
    "multi", "synthesis", "consensus", "mcp", "mechanism", "pattern", "tier", "pillar",
    "rank", "slice", "domain", "concept", "type", "python", "backend", "docker", "tool",
    "linkedin", "prefect", "sqlite", "content", "scheduler", "core", "store", "app",
    "engineering", "capability", "building", "omniscout", "business", "systems",
}


def _tokenize(text: str) -> set[str]:
    return {
        t for t in re.findall(r"[a-z0-9]{3,}", str(text).lower())
        if t not in _STOPWORDS
    }


def _text_blob(card: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "claim", "summary", "mechanism", "topic"):
        value = card.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
        elif value is not None:
            parts.append(str(value))
    pattern = card.get("pattern") or {}
    parts.extend([str(pattern.get("name", "")), str(pattern.get("description", ""))])
    for d in card.get("doctrine_domains", []):
        parts.append(str(d))
    entities = card.get("entities", {}) or {}
    for ent in entities.get("entities", []):
        parts.extend([str(ent.get("name", "")), str(ent.get("type", ""))])
    for t in card.get("tags", []):
        parts.append(str(t))
    return " ".join(parts)


def _semantic_fingerprint(card: dict[str, Any]) -> set[str]:
    """Focused semantic tokens for cross-strategy capability clustering."""
    parts: list[str] = []
    for key in ("title", "claim", "topic"):
        value = card.get(key)
        if isinstance(value, list):
            parts.extend(str(v) for v in value)
        elif value is not None:
            parts.append(str(value))
    pattern = card.get("pattern") or {}
    parts.append(str(pattern.get("name", "")))
    for d in card.get("doctrine_domains", []):
        parts.append(str(d))
    entities = card.get("entities", {}) or {}
    for ent in entities.get("entities", []):
        parts.append(str(ent.get("name", "")))
    bp = card.get("engineering_blueprint", {}) or {}
    for comp in bp.get("architecture_components", []):
        parts.append(str(comp))
    ts = bp.get("tech_stack", {}) or {}
    for bucket in ts.values():
        if isinstance(bucket, list):
            parts.extend(str(v) for v in bucket)
    gtm = card.get("gtm_strategy", {}) or {}
    for ch in gtm.get("channels", []):
        parts.append(str(ch))
    return _tokenize(" ".join(parts))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _load_cards() -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    if not L2_CARDS.exists():
        return cards
    for path in sorted(L2_CARDS.glob("l2-*.json")):
        try:
            cards.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return cards


def _strategy_id(card: dict[str, Any]) -> str:
    strat = card.get("strategy") or {}
    sid = strat.get("strategy_id")
    return str(sid) if sid else ""


def _topic_strategy_ids() -> set[str]:
    """All strategy ids declared in the topic strategy file."""
    try:
        from rig_foundry.omniscout_build_cards import load_topic_strategy
        ts = load_topic_strategy()
        return set((ts.get("strategies") or {}).keys())
    except Exception:
        return set(DOCTRINE_DOMAINS.keys())


def _parse_money_range(value: Any) -> tuple[float, float]:
    """Extract lower/upper bounds from strings like '$5K-20K/mo' or '$2-5B'."""
    s = str(value).lower().replace(",", "")
    nums: list[float] = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([kmb]?)", s):
        n = float(m.group(1))
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "": 1}.get(m.group(2), 1)
        nums.append(n * mult)
    if len(nums) >= 2:
        return nums[0], nums[-1]
    if len(nums) == 1:
        return nums[0], nums[0]
    return 0.0, 0.0


def _revenue_score(bi: dict[str, Any]) -> float:
    """Deterministic revenue potential score from business_intelligence fields."""
    ltv_lo, ltv_hi = _parse_money_range(bi.get("estimated_ltv"))
    cac_lo, cac_hi = _parse_money_range(bi.get("estimated_cac"))
    tam_lo, tam_hi = _parse_money_range(bi.get("tam"))
    gm = 0.0
    gm_str = str(bi.get("gross_margin", ""))
    if gm_str:
        m = re.search(r"(\d+(?:\.\d+)?)", gm_str)
        if m:
            gm = float(m.group(1)) / 100.0
    score = 0.0
    score += min((ltv_lo + ltv_hi) / 2 / 1e5, 10.0)  # LTV up to 10 pts
    score += min((tam_lo + tam_hi) / 2 / 1e9, 10.0)   # TAM up to 10 pts
    if cac_lo > 0 and ltv_lo > 0:
        score += min((ltv_lo / max(cac_hi, cac_lo, 1)) * 5, 10.0)  # LTV/CAC up to 10
    score += gm * 10.0
    return round(score, 2)


class _UnionFind:
    def __init__(self, items: list[int]) -> None:
        self.parent = {i: i for i in items}
        self.rank = {i: 0 for i in items}

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1


def detect_clusters(cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Group cards into product candidates and cross-strategy capability clusters."""
    cards = cards if cards is not None else _load_cards()
    if not cards:
        return {"product_candidates": [], "capability_clusters": [], "card_count": 0}

    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for card in cards:
        sid = _strategy_id(card)
        if sid:
            by_strategy[sid].append(card)

    product_candidates: list[dict[str, Any]] = []
    for sid, group in sorted(by_strategy.items(), key=lambda kv: -len(kv[1])):
        if len(group) < 5:
            continue
        scores = [c.get("score", {}).get("total", 0) for c in group]
        avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0
        rev_scores = [_revenue_score(c.get("business_intelligence", {}) or {}) for c in group]
        product_candidates.append({
            "strategy_id": sid,
            "card_count": len(group),
            "avg_score": avg_score,
            "combined_revenue_potential": round(sum(rev_scores), 2),
            "avg_revenue_potential": round(sum(rev_scores) / len(rev_scores), 2) if rev_scores else 0.0,
            "card_ids": [c.get("card_id") for c in group],
        })

    # Capability clusters: cliques of 3+ cards from >=2 different strategies where every
    # pairwise Jaccard on focused semantic fingerprints is > 0.15.
    tokens = [_semantic_fingerprint(c) for c in cards]
    strategies = [_strategy_id(c) for c in cards]
    n = len(cards)

    # Build adjacency for cross-strategy pairs above threshold.
    adj: dict[int, set[int]] = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            if not strategies[i] or not strategies[j]:
                continue
            if strategies[i] == strategies[j]:
                continue
            if _jaccard(tokens[i], tokens[j]) > 0.15:
                adj[i].add(j)
                adj[j].add(i)

    used: set[int] = set()
    capability_clusters: list[dict[str, Any]] = []
    # Greedy maximal clique expansion from highest-degree seeds.
    degree_order = sorted(range(n), key=lambda i: len(adj[i]), reverse=True)
    for seed in degree_order:
        if seed in used or len(adj[seed]) < 2:
            continue
        clique = [seed]
        candidates = sorted(adj[seed])
        for cand in candidates:
            if cand in used:
                continue
            if all(cand in adj[m] for m in clique):
                clique.append(cand)
        if len(clique) >= 3:
            sids_in_clique = {strategies[i] for i in clique if strategies[i]}
            if len(sids_in_clique) >= 2:
                shared_tokens: set[str] = tokens[clique[0]].copy()
                for i in clique[1:]:
                    shared_tokens &= tokens[i]
                all_entities: set[str] = set()
                for i in clique:
                    ents = cards[i].get("entities", {}) or {}
                    for ent in ents.get("entities", []):
                        if ent.get("name"):
                            all_entities.add(ent["name"])
                cross_patterns: list[str] = []
                for i in clique:
                    pat = cards[i].get("pattern", {}) or {}
                    if pat.get("name"):
                        cross_patterns.append(pat["name"])
                top_patterns = [p for p, _ in Counter(cross_patterns).most_common(3)]
                capability_clusters.append({
                    "cards": [cards[i].get("card_id") for i in clique],
                    "card_count": len(clique),
                    "strategies": sorted(sids_in_clique),
                    "shared_entities": sorted(all_entities)[:20],
                    "shared_concepts": sorted(shared_tokens)[:20],
                    "cross_strategy_pattern": " | ".join(top_patterns) if top_patterns else "cross-strategy synthesis",
                })
                used.update(clique)

    capability_clusters.sort(key=lambda c: -c["card_count"])

    return {
        "card_count": len(cards),
        "product_candidates": product_candidates,
        "capability_clusters": capability_clusters,
    }


def _top_claims(group: list[dict[str, Any]], limit: int = 7) -> list[dict[str, Any]]:
    ranked = sorted(
        group,
        key=lambda c: c.get("score", {}).get("total", 0),
        reverse=True,
    )
    claims = []
    for c in ranked[:limit]:
        claims.append({
            "card_id": c.get("card_id"),
            "title": c.get("title"),
            "claim": c.get("claim"),
            "score": c.get("score", {}).get("total"),
            "topic": c.get("topic"),
        })
    return claims


def _merge_agent_team(group: list[dict[str, Any]]) -> dict[str, Any]:
    agents_by_identity: dict[str, dict[str, Any]] = {}
    skills: set[str] = set()
    substrates: set[str] = set()
    for c in group:
        at = c.get("agent_team", {}) or {}
        for a in at.get("agents", []):
            if not isinstance(a, dict):
                continue
            ident = a.get("identity") or a.get("role", "agent")
            if ident not in agents_by_identity:
                agent_skills = a.get("skills", [])
                if not isinstance(agent_skills, list):
                    agent_skills = [str(agent_skills)]
                agents_by_identity[ident] = {
                    "role": a.get("role"),
                    "model": a.get("model"),
                    "identity": ident,
                    "skills": sorted(set(str(s) for s in agent_skills)),
                }
            skills.update(str(s) for s in a.get("skills", []) if isinstance(a.get("skills"), list))
        sr = at.get("skills_required", [])
        if isinstance(sr, list):
            skills.update(str(s) for s in sr)
        ps = at.get("pai_substrates", [])
        if isinstance(ps, list):
            for item in ps:
                if isinstance(item, dict):
                    substrates.add(item.get("agent_name") or stable_json(item))
                else:
                    substrates.add(str(item))
    return {
        "agent_count": len(agents_by_identity),
        "agents": sorted(agents_by_identity.values(), key=lambda a: a["role"] or ""),
        "skills_required": sorted(skills),
        "pai_substrates": sorted(substrates),
    }


def _merge_gtm(group: list[dict[str, Any]]) -> dict[str, Any]:
    channels: set[str] = set()
    content: list[dict[str, Any]] = []
    icps: list[str] = []
    motions: list[str] = []
    for c in group:
        gtm = c.get("gtm_strategy", {}) or {}
        channels.update(gtm.get("channels", []))
        icps.append(gtm.get("icp", ""))
        motions.append(gtm.get("sales_motion", ""))
        for item in gtm.get("content_plan", []):
            if isinstance(item, dict):
                content.append(item)
    counter_icp = Counter(i for i in icps if i)
    counter_motion = Counter(m for m in motions if m)
    # Deduplicate content plan by (type, topic)
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in content:
        key = stable_json({"type": item.get("type"), "topic": item.get("topic")})
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return {
        "sales_motion": counter_motion.most_common(1)[0][0] if counter_motion else "",
        "icp": counter_icp.most_common(1)[0][0] if counter_icp else "",
        "channels": sorted(channels),
        "content_plan": deduped[:10],
    }


def _combined_revenue_model(group: list[dict[str, Any]]) -> dict[str, Any]:
    models: set[str] = set()
    price_ranges: set[str] = set()
    ideas: set[str] = set()
    moats: set[str] = set()
    total_ltv = 0.0
    total_cac = 0.0
    count = 0
    for c in group:
        bi = c.get("business_intelligence", {}) or {}
        if bi.get("revenue_model"):
            models.add(bi["revenue_model"])
        if bi.get("price_range"):
            price_ranges.add(bi["price_range"])
        for idea in bi.get("revenue_ideas", []):
            ideas.add(idea)
        for moat in bi.get("competitive_moat", []):
            moats.add(moat)
        ltv_lo, ltv_hi = _parse_money_range(bi.get("estimated_ltv"))
        cac_lo, cac_hi = _parse_money_range(bi.get("estimated_cac"))
        if ltv_hi > 0:
            total_ltv += (ltv_lo + ltv_hi) / 2
        if cac_hi > 0:
            total_cac += (cac_lo + cac_hi) / 2
        count += 1
    ltv_cac = round(total_ltv / max(total_cac, 1), 2)
    return {
        "revenue_models": sorted(models),
        "price_ranges": sorted(price_ranges),
        "revenue_ideas": sorted(ideas)[:10],
        "competitive_moats": sorted(moats)[:10],
        "aggregate_ltv": round(total_ltv / max(count, 1), 2),
        "aggregate_cac": round(total_cac / max(count, 1), 2),
        "implied_ltv_cac_ratio": ltv_cac,
    }


def _shared_pattern(group: list[dict[str, Any]]) -> dict[str, Any]:
    names: list[str] = []
    descs: list[str] = []
    for c in group:
        pat = c.get("pattern", {}) or {}
        if pat.get("name"):
            names.append(pat["name"])
        if pat.get("description"):
            descs.append(pat["description"])
    top_names = Counter(names).most_common(3)
    # Extract recurring mechanism markers from titles/claims
    blob = " ".join(_text_blob(c) for c in group)
    top_concepts = [w for w, _ in Counter(_tokenize(blob)).most_common(10)]
    return {
        "pattern_names": [n for n, _ in top_names],
        "description": descs[0] if descs else "",
        "recurring_concepts": top_concepts,
    }


def _investment_thesis(sid: str, group: list[dict[str, Any]], avg_score: float, rev: dict[str, Any]) -> str:
    scores = [c.get("score", {}).get("total", 0) for c in group]
    strong = sum(1 for s in scores if s >= 85)
    promoted = sum(1 for c in group if c.get("score", {}).get("promote"))
    models = rev.get("revenue_models", [])
    model_str = models[0] if models else "consulting-led capability"
    return (
        f"Strategy '{sid}' aggregates {len(group)} build cards with average score "
        f"{avg_score} ({strong} STRONG/EXCELLENT, {promoted} promoted). "
        f"Primary monetization: {model_str}. "
        f"Implied LTV/CAC ratio {rev.get('implied_ltv_cac_ratio', 'N/A')}. "
        f"Sufficient signal density to treat as a product candidate."
    )


def _risk_portfolio(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    risks: list[str] = []
    for c in group:
        for r in c.get("risks", []):
            if r:
                risks.append(r)
    top = [r for r, _ in Counter(risks).most_common(10)]
    return [{"risk": r, "count": risks.count(r)} for r in top]


def generate_meta_cards(cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Synthesize meta-cards for every strategy with >=5 cards and persist them."""
    cards = cards if cards is not None else _load_cards()
    clusters = detect_clusters(cards)
    META_DIR.mkdir(parents=True, exist_ok=True)

    created: list[dict[str, Any]] = []
    for pc in clusters["product_candidates"]:
        sid = pc["strategy_id"]
        group = [c for c in cards if _strategy_id(c) == sid]
        rev = _combined_revenue_model(group)
        shared = _shared_pattern(group)
        meta: dict[str, Any] = {
            "schema": SCHEMA_META,
            "meta_card_id": f"meta-{sid}-{sha256_text(stable_json({'sid': sid, 'ts': utc_now()}))[:12]}",
            "created_at": utc_now(),
            "title": f"Meta-Synthesis: {sid}",
            "strategies_covered": [sid],
            "card_count": len(group),
            "aggregated_claims": _top_claims(group),
            "combined_revenue_model": rev,
            "merged_agent_team": _merge_agent_team(group),
            "shared_pattern": shared,
            "combined_gtm": _merge_gtm(group),
            "investment_thesis": _investment_thesis(sid, group, pc["avg_score"], rev),
            "risk_portfolio": _risk_portfolio(group),
            "avg_score": pc["avg_score"],
            "combined_revenue_potential": pc["combined_revenue_potential"],
        }
        path = META_DIR / f"{meta['meta_card_id']}.json"
        atomic_json(path, meta)
        created.append({
            "meta_card_id": meta["meta_card_id"],
            "strategy_id": sid,
            "path": str(path),
            "card_count": meta["card_count"],
        })

    return {
        "created": created,
        "meta_dir": str(META_DIR),
        "product_candidate_count": len(clusters["product_candidates"]),
    }


def identify_gaps(cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Scan the knowledge graph for missing connections and decay."""
    cards = cards if cards is not None else _load_cards()
    all_strategies = _topic_strategy_ids()

    present_strategies: set[str] = set()
    for c in cards:
        sid = _strategy_id(c)
        if sid:
            present_strategies.add(sid)
    empty_strategies = sorted(all_strategies - present_strategies)

    # Missing entity links: entity pairs co-occurring in >=2 cards but no explicit relationship.
    cooccurrence: Counter[tuple[str, str]] = Counter()
    actual_relationships: set[tuple[str, str]] = set()
    entity_cards: dict[str, set[str]] = defaultdict(set)
    for c in cards:
        cid = c.get("card_id", "")
        ents = c.get("entities", {}) or {}
        names = [(e.get("entity_id") or e.get("name") or "", e.get("name", "")) for e in ents.get("entities", [])]
        names = [(nid, n) for nid, n in names if nid and n]
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = sorted((names[i][0], names[j][0]))
                cooccurrence[(a, b)] += 1
                entity_cards[a].add(cid)
                entity_cards[b].add(cid)
        for rel in ents.get("relationships", []):
            a = rel.get("from") or ""
            b = rel.get("to") or ""
            if a and b:
                actual_relationships.add(tuple(sorted((a, b))))

    missing_entity_links: list[dict[str, Any]] = []
    for (a, b), count in cooccurrence.items():
        if count >= 2 and tuple(sorted((a, b))) not in actual_relationships:
            missing_entity_links.append({
                "entity_a": a,
                "entity_b": b,
                "cooccurrence_count": count,
                "cards_in_common": sorted(entity_cards[a] & entity_cards[b]),
                "suggested_relationship": "RELATED_TO",
            })
    missing_entity_links.sort(key=lambda x: -x["cooccurrence_count"])

    # Expiring cards: reverify_by within 90 days or estimated_expiry_months <= 6.
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=90)
    expiring_cards: list[dict[str, Any]] = []
    for c in cards:
        tv = c.get("temporal_validity", {}) or {}
        expiry = tv.get("estimated_expiry_months")
        reverify = tv.get("reverify_by")
        is_expiring = False
        reason = ""
        if isinstance(expiry, (int, float)) and expiry <= 6:
            is_expiring = True
            reason = f"estimated_expiry_months={expiry}"
        if reverify:
            try:
                # reverify_by often 'YYYY-MM'
                rv = datetime.strptime(str(reverify), "%Y-%m").replace(tzinfo=timezone.utc, day=1)
                if rv <= horizon:
                    is_expiring = True
                    reason = (reason + "; " if reason else "") + f"reverify_by={reverify}"
            except ValueError:
                pass
        if is_expiring:
            expiring_cards.append({
                "card_id": c.get("card_id"),
                "title": c.get("title"),
                "strategy_id": _strategy_id(c),
                "reason": reason,
                "freshness": tv.get("freshness"),
            })

    # Research frontiers: strategies with 0-2 cards, low-scoring non-empty strategies,
    # and doctrine domains with sparse coverage.
    by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in cards:
        sid = _strategy_id(c)
        if sid:
            by_strategy[sid].append(c)
    frontier_strategies: list[dict[str, Any]] = []
    for sid in sorted(all_strategies):
        group = by_strategy.get(sid, [])
        if len(group) <= 2:
            frontier_strategies.append({
                "strategy_id": sid,
                "card_count": len(group),
                "frontier_type": "low_volume",
            })
        else:
            scores = [c.get("score", {}).get("total", 0) for c in group]
            avg = sum(scores) / len(scores) if scores else 0
            if avg < 70:
                frontier_strategies.append({
                    "strategy_id": sid,
                    "card_count": len(group),
                    "avg_score": round(avg, 2),
                    "frontier_type": "low_quality",
                })

    # Doctrine domain coverage gaps
    domain_hits: dict[str, int] = defaultdict(int)
    for c in cards:
        for d in c.get("doctrine_domains", []):
            domain_hits[d] += 1
    sparse_domains = [
        {"domain": d, "card_count": domain_hits.get(d, 0), "keywords": kw}
        for d, kw in DOCTRINE_DOMAINS.items()
        if domain_hits.get(d, 0) <= 3
    ]
    sparse_domains.sort(key=lambda x: x["card_count"])

    return {
        "empty_strategies": empty_strategies,
        "missing_entity_links": missing_entity_links[:20],
        "expiring_cards": expiring_cards,
        "research_frontiers": {
            "low_volume_or_low_quality_strategies": frontier_strategies,
            "sparse_doctrine_domains": sparse_domains,
        },
    }


def _status(cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    cards = cards if cards is not None else _load_cards()
    clusters = detect_clusters(cards)
    gaps = identify_gaps(cards)
    meta_files = list(META_DIR.glob("meta-*.json")) if META_DIR.exists() else []
    return {
        "card_count": len(cards),
        "product_candidates": len(clusters["product_candidates"]),
        "capability_clusters": len(clusters["capability_clusters"]),
        "meta_cards_on_disk": len(meta_files),
        "empty_strategies": len(gaps["empty_strategies"]),
        "expiring_cards": len(gaps["expiring_cards"]),
        "missing_entity_links": len(gaps["missing_entity_links"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniScout L2 meta-synthesis engine")
    parser.add_argument(
        "command",
        choices=["clusters", "meta", "gaps", "all", "status"],
        help="clusters: detect product candidates and capability clusters; "
             "meta: generate meta-cards; gaps: identify knowledge gaps; "
             "all: run clusters, meta, gaps; status: summary",
    )
    parser.add_argument("--output", "-o", type=str, default=None, help="Optional JSON output file")
    args = parser.parse_args(argv)

    cards = _load_cards()
    result: dict[str, Any] = {"command": args.command, "card_count": len(cards)}

    if args.command == "clusters":
        result["clusters"] = detect_clusters(cards)
    elif args.command == "meta":
        result["meta"] = generate_meta_cards(cards)
    elif args.command == "gaps":
        result["gaps"] = identify_gaps(cards)
    elif args.command == "all":
        result["clusters"] = detect_clusters(cards)
        result["meta"] = generate_meta_cards(cards)
        result["gaps"] = identify_gaps(cards)
    elif args.command == "status":
        result.update(_status(cards))

    print(stable_json(result))

    if args.output:
        atomic_text(Path(args.output), stable_json(result) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
