"""OmniScout L2 v3 enrichment — Memory OS patterns.

Upgrades v2 cards into a living knowledge graph:
- entities: named entity extraction (persons, companies, tools, concepts) + typed relationships
- memory_layer: assignment to one of 8 governed layers (corrections → evidence)
- semantic_links: cross-card relationships (supports/contradicts/extends/caused-by/part-of)
- contradiction_scan: detect conflicts against existing card corpus
- temporal_validity: evidence freshness window + expiry estimate
- predictive_context: top-5 related cards an agent should load alongside this one
- promotion_state: STAGED with evidence gates (doctrine promotion readiness)
- proof_seal: hash-chained proof binding the enriched artifact
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
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

SCHEMA_V3 = "rig.omniscout.build-card.v3"

# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------

ENTITY_PATTERNS: dict[str, list[str]] = {
    "TOOL": [
        r"\b(Ollama|MLX|GGUF|Prefect|Temporal|Playwright|ChromaDB|pgvector|Supabase|Vercel|Cloudflare|n8n|LangChain|LangGraph|Pydantic|FastAPI|Docker|Kubernetes)\b",
        r"\b(qwen[0-9]?|llama|claude|gpt-?\d|gemini|mistral|phi-?\d)\b",
    ],
    "COMPANY": [
        r"\b(Google|OpenAI|Anthropic|Meta|Microsoft|Apple|Amazon|NVIDIA|Hugging\s*Face)\b",
        r"\b(NeurIPS|ICML|ICLR|ACL|CVPR|arXiv)\b",
    ],
    "CONCEPT": [
        r"\b(RAG|retrieval.augmented.generation|fine.tuning|RLHF|PPO|DPO|chain.of.thought|CoT)\b",
        r"\b(multi.agent|tool.use|function.calling|agent.harness|proof.packet|GEV|done.test)\b",
        r"\b(Brier.score|calibration|active.inference|free.energy|Markov.blanket)\b",
        r"\b(saga.pattern|exactly.once|idempotency|circuit.breaker|retry|backoff)\b",
    ],
    "METHOD": [
        r"\b(meta.analysis|systematic.review|randomized.controlled|RCT|cohort.study|case.study)\b",
        r"\b(benchmark|ablation|eval.harness|regression.test|unit.test)\b",
    ],
}

RELATION_KEYWORDS: dict[str, list[str]] = {
    "uses_tool": [r"\busing\b", r"\bwith\b", r"\bvia\b", r"\bthrough\b", r"\bpowered.by\b"],
    "improves_on": [r"\boutperforms?\b", r"\bsuperior.to\b", r"\bbetter.than\b", r"\bvs?\.\b"],
    "contradicts": [r"\bhowever\b", r"\bcontrary\b", r"\bbut\b", r"\bdespite\b", r"\bnot\b.*?\bbut\b"],
    "extends": [r"\bextends?\b", r"\bbuilds.on\b", r"\bfollowing\b", r"\bbased.on\b"],
    "caused_by": [r"\bbecause\b", r"\bcaused.by\b", r"\bdue.to\b", r"\bresults.from\b"],
}


def extract_entities(card: dict[str, Any]) -> dict[str, Any]:
    """Extract named entities with types and inter-entity relationships."""
    blob = " ".join([
        str(card.get("title") or ""),
        str(card.get("claim") or ""),
        str(card.get("summary") or ""),
        str(card.get("mechanism") or ""),
        str(card.get("mechanism") or ""),
        " ".join(str(e.get("quote_or_fact") or "") for e in card.get("evidence") or []),
    ])

    entities: list[dict[str, str]] = []
    seen_names: set[str] = set()

    for etype, patterns in ENTITY_PATTERNS.items():
        for pat in patterns:
            for m in re.finditer(pat, blob, re.I):
                name = m.group(0).strip()
                key = name.lower()
                if key in seen_names:
                    continue
                seen_names.add(key)
                entities.append({
                    "name": name,
                    "type": etype,
                    "entity_id": "ent-" + sha256_text(name.lower())[:12],
                })

    # extract domain concepts from doctrine mapping
    for domain, kws in DOCTRINE_DOMAINS.items():
        for kw in kws:
            if kw.lower() in blob.lower() and kw.lower() not in seen_names:
                seen_names.add(kw.lower())
                entities.append({
                    "name": kw,
                    "type": "CONCEPT",
                    "entity_id": "ent-" + sha256_text(kw.lower())[:12],
                    "domain": domain,
                })

    # extract numbers/metrics as measurement entities
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(%|x|tokens?/s|ms|GB|TiB|hours?|days?|weeks?)", blob):
        val = m.group(0)
        if val.lower() not in seen_names:
            seen_names.add(val.lower())
            entities.append({
                "name": val,
                "type": "METRIC",
                "entity_id": "ent-" + sha256_text(val)[:12],
            })

    # detect relationships between entities from same card
    relationships: list[dict[str, str]] = []
    sentences = re.split(r"[.!?]+", blob)
    for sent in sentences:
        sent_lower = sent.lower()
        found_ents = [e for e in entities if e["name"].lower() in sent_lower]
        if len(found_ents) < 2:
            continue
        for rel_type, kws in RELATION_KEYWORDS.items():
            if any(re.search(kw, sent_lower) for kw in kws):
                # link first two found entities
                relationships.append({
                    "from": found_ents[0]["entity_id"],
                    "from_name": found_ents[0]["name"],
                    "to": found_ents[1]["entity_id"],
                    "to_name": found_ents[1]["name"],
                    "type": rel_type,
                    "evidence": sent.strip()[:200],
                })
                break

    return {
        "entities": entities[:40],
        "entity_count": len(entities),
        "relationships": relationships[:15],
        "relationship_count": len(relationships),
        "entity_types": dict(Counter(e["type"] for e in entities)),
    }


# ---------------------------------------------------------------------------
# Memory layer assignment
# ---------------------------------------------------------------------------

MEMORY_LAYERS = {
    1: ("CORRECTIONS", "Fixes wrong beliefs; overrides everything below"),
    2: ("IDENTITY_PREFERENCES", "Operator preferences, identity, style"),
    3: ("ACTIVE_GOALS", "Current missions and objectives"),
    4: ("SAFETY_AUTHORITY", "Safety constraints, governance, Gate-D"),
    5: ("WORKSPACE_REPO_FACTS", "Repository/workspace facts, architecture"),
    6: ("EPISODIC_RUN_STATE", "What happened in this run/session"),
    7: ("VERIFIED_PROCEDURES", "Tested, repeatable procedures"),
    8: ("EVIDENCE_DECISION_PROVENANCE", "Evidence trail for decisions"),
}


def assign_memory_layer(card: dict[str, Any]) -> dict[str, Any]:
    """Assign this card to the correct governed memory layer."""
    blob = " ".join([
        str(card.get("title") or ""),
        str(card.get("claim") or ""),
        str(card.get("summary") or ""),
        str(card.get("mechanism") or ""),
        str((card.get("strategy") or {}).get("strategy_id") or ""),
    ]).lower()

    # layer 1: corrections
    if any(w in blob for w in ["wrong", "incorrect", "false", "myth", "debunk", "contrary"]):
        layer = 1
    # layer 4: safety/governance
    elif any(w in blob for w in ["gate", "proof", "verify", "audit", "security", "compliance", "safety"]):
        layer = 4
    # layer 7: verified procedures
    elif any(w in blob for w in ["step", "procedure", "how to", "implement", "pipeline", "workflow"]):
        layer = 7
    # layer 5: repo facts
    elif any(w in blob for w in ["architecture", "system", "infrastructure", "framework", "design"]):
        layer = 5
    # layer 8: evidence/decision
    elif (card.get("consensus") or {}).get("used") or (card.get("sources") or {}).get("count", 0) >= 3:
        layer = 8
    # layer 3: active goals
    elif any(w in blob for w in ["strategy", "objective", "goal", "target", "plan"]):
        layer = 3
    # layer 6: episodic
    elif any(w in blob for w in ["observed", "measured", "experiment", "evaluated", "tested"]):
        layer = 6
    else:
        layer = 8  # default to evidence

    layer_name, layer_desc = MEMORY_LAYERS[layer]
    return {
        "layer": layer,
        "layer_name": layer_name,
        "layer_description": layer_desc,
        "authority_rank": f"L{layer}/8 (higher = more authoritative within its domain)",
        "retention": "permanent" if layer <= 5 else ("promoted_on_review" if layer == 7 else "expires_with_evidence"),
    }


# ---------------------------------------------------------------------------
# Semantic cross-links + contradiction detection
# ---------------------------------------------------------------------------

def _card_text(card: dict[str, Any]) -> str:
    return " ".join([
        str(card.get("title") or ""),
        str(card.get("claim") or ""),
        str(card.get("summary") or "")[:500],
        str((card.get("strategy") or {}).get("strategy_id") or ""),
        str(card.get("topic") or ""),
    ]).lower()


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{4,}", text.lower())}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def compute_semantic_links(card: dict[str, Any], all_cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Find cross-card relationships: supports, contradicts, extends, similar."""
    my_text = _card_text(card)
    my_tokens = _tokenize(my_text)
    my_sid = (card.get("strategy") or {}).get("strategy_id") or ""
    my_id = card.get("card_id") or ""
    my_claim = str(card.get("claim") or "").lower()

    links: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []

    for other in all_cards:
        other_id = other.get("card_id") or ""
        if other_id == my_id:
            continue
        other_text = _card_text(other)
        other_tokens = _tokenize(other_text)
        sim = _jaccard(my_tokens, other_tokens)
        if sim < 0.08:
            continue

        other_sid = (other.get("strategy") or {}).get("strategy_id") or ""
        other_claim = str(other.get("claim") or "").lower()

        # classify relationship
        rel_type = "RELATED_TO"
        reason = f"token_overlap={sim:.2f}"

        # same strategy = extends
        if my_sid == other_sid and sim > 0.15:
            rel_type = "EXTENDS"
            reason = f"same strategy `{my_sid}`, token_overlap={sim:.2f}"

        # contradiction heuristic: similar topic but different claim keywords
        neg_words = {"not", "no", "cannot", "fail", "wrong", "inferior", "worse", "contrary", "however", "despite"}
        my_neg = neg_words & my_tokens
        other_neg = neg_words & other_tokens
        if (my_neg or other_neg) and sim > 0.12 and my_sid == other_sid:
            # possible contradiction
            rel_type = "CONTRADICTS"
            reason = f"negation overlap + same strategy, token_overlap={sim:.2f}"
            contradictions.append({
                "card_id": other_id,
                "title": other.get("title") or "",
                "similarity": round(sim, 3),
                "reason": reason,
            })

        # supports: one cites evidence the other uses
        my_urls = set((card.get("sources") or {}).get("urls") or [])
        other_urls = set((other.get("sources") or {}).get("urls") or [])
        shared_urls = my_urls & other_urls
        if shared_urls:
            rel_type = "SUPPORTS"
            reason = f"shares {len(shared_urls)} source URL(s), token_overlap={sim:.2f}"

        links.append({
            "card_id": other_id,
            "title": (other.get("title") or "")[:80],
            "strategy_id": other_sid,
            "relationship": rel_type,
            "similarity": round(sim, 3),
            "reason": reason,
        })

    # sort by similarity
    links.sort(key=lambda x: -x["similarity"])
    contradictions.sort(key=lambda x: -x["similarity"])

    return {
        "links": links[:20],
        "link_count": len(links),
        "contradictions": contradictions[:5],
        "contradiction_count": len(contradictions),
        "supports_count": sum(1 for l in links if l["relationship"] == "SUPPORTS"),
        "extends_count": sum(1 for l in links if l["relationship"] == "EXTENDS"),
        "graph_density": round(len(links) / max(1, len(all_cards) - 1), 3),
    }


# ---------------------------------------------------------------------------
# Temporal validity
# ---------------------------------------------------------------------------

def compute_temporal_validity(card: dict[str, Any]) -> dict[str, Any]:
    """Estimate evidence freshness and decay."""
    created = card.get("created_at") or utc_now()
    consensus = card.get("consensus") or {}
    sources = card.get("sources") or {}
    score = card.get("score") or {}
    rank = score.get("rank") or "?"
    total = int(score.get("total") or 0)

    # extract years from evidence
    years: list[int] = []
    for e in card.get("evidence") or []:
        if isinstance(e, dict):
            for m in re.findall(r"\b(19|20)(\d{2})\b", str(e.get("quote_or_fact") or "")):
                years.append(int(m[0] + m[1]))
    for r in consensus.get("results") or []:
        if isinstance(r, dict) and r.get("year"):
            try:
                years.append(int(r["year"]))
            except (ValueError, TypeError):
                pass

    now_year = datetime.now(timezone.utc).year
    if years:
        max_year = max(years)
        min_year = min(years)
        median_year = sorted(years)[len(years) // 2]
        age_years = now_year - max_year
    else:
        max_year = min_year = median_year = now_year
        age_years = 0

    # freshness classification
    if age_years <= 1:
        freshness = "FRESH"
        decay_window = "2-3 years before re-verification needed"
    elif age_years <= 3:
        freshness = "RECENT"
        decay_window = "1-2 years — re-verify against newer evidence"
    elif age_years <= 5:
        freshness = "AGING"
        decay_window = "Re-verify now — evidence may be superseded"
    else:
        freshness = "STALE"
        decay_window = "Evidence is old — flag for re-research before doctrine use"

    # expiry estimate
    expiry_months = max(3, 36 - age_years * 6)
    if rank == "EXCELLENT":
        expiry_months = int(expiry_months * 1.5)
    elif rank in {"WEAK", "REJECT"}:
        expiry_months = max(3, expiry_months // 2)

    return {
        "freshness": freshness,
        "evidence_year_range": [min_year, max_year] if years else None,
        "evidence_median_year": median_year if years else None,
        "age_years": age_years,
        "decay_window": decay_window,
        "estimated_expiry_months": expiry_months,
        "reverify_by": (
            datetime.now(timezone.utc).replace(month=((datetime.now(timezone.utc).month - 1 + expiry_months) % 12) + 1)
            .strftime("%Y-%m")
        ),
        "evidence_year_count": len(years),
    }


# ---------------------------------------------------------------------------
# Predictive context (what to load alongside)
# ---------------------------------------------------------------------------

def compute_predictive_context(
    card: dict[str, Any],
    semantic_links: dict[str, Any],
    all_cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Predict what an agent should load alongside this card."""
    sid = (card.get("strategy") or {}).get("strategy_id") or ""
    tier = (card.get("strategy") or {}).get("tier") or "na"

    # top 5 related cards by combined score: similarity + rank weight
    rank_weight = {"EXCELLENT": 3, "STRONG": 2, "GOOD": 1, "WEAK": 0.5, "REJECT": 0}
    card_rank_map = {c.get("card_id"): c for c in all_cards}

    scored: list[tuple[float, dict[str, Any]]] = []
    for link in semantic_links.get("links") or []:
        other = card_rank_map.get(link["card_id"])
        if not other:
            continue
        weight = rank_weight.get((other.get("score") or {}).get("rank", "?"), 0)
        combined = link["similarity"] * 2 + weight
        scored.append((combined, link))
    scored.sort(key=lambda x: -x[0])

    prefetch = [
        {
            "card_id": link["card_id"],
            "title": link["title"],
            "strategy_id": link["strategy_id"],
            "relationship": link["relationship"],
            "similarity": link["similarity"],
            "why": f"Load alongside because: {link['reason']}",
        }
        for _, link in scored[:5]
    ]

    # strategy cluster: other cards in same strategy
    same_strategy = [
        {
            "card_id": c.get("card_id"),
            "title": (c.get("title") or "")[:60],
            "rank": (c.get("score") or {}).get("rank"),
        }
        for c in all_cards
        if (c.get("strategy") or {}).get("strategy_id") == sid
        and c.get("card_id") != card.get("card_id")
    ][:5]

    # doctrine context: what doctrine files should be loaded
    doctrine_files: list[str] = []
    for domain in card.get("doctrine_domains") or []:
        doctrine_files.append(f"~/.rig/agent-doctrine/RIG_{domain.upper()}_DOCTRINE.md")
    if tier == "T0":
        doctrine_files.append("~/.rig/agent-doctrine/HONEST_COMPLETION_DOCTRINE.md")
        doctrine_files.append("~/.rig/agent-doctrine/RIG_AGENT_STANDARDS_DOCTRINE.md")

    return {
        "prefetch_cards": prefetch,
        "same_strategy_cluster": same_strategy,
        "doctrine_context": doctrine_files[:3],
        "agent_briefing": (
            f"When loading this card, also load: "
            f"{len(prefetch)} related cards from {len(set(l['strategy_id'] for l in prefetch))} strategies, "
            f"{len(same_strategy)} same-strategy siblings, "
            f"and {len(doctrine_files)} doctrine files."
        ),
    }


# ---------------------------------------------------------------------------
# Promotion state (governed doctrine pipeline)
# ---------------------------------------------------------------------------

def compute_promotion_state(card: dict[str, Any], analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate readiness for doctrine promotion through governed pipeline."""
    score = card.get("score") or {}
    rank = score.get("rank") or "?"
    total = int(score.get("total") or 0)
    sources = card.get("sources") or {}
    source_count = int(sources.get("count") or 0)
    consensus = card.get("consensus") or {}
    consensus_count = int(consensus.get("count") or 0)

    # doctrine promotion gates (from semantic_memory.py patterns)
    gates: dict[str, bool] = {
        "evidence_density": source_count >= 3,
        "consensus_backed": consensus_count >= 1,
        "mechanism_present": len(str(card.get("mechanism") or "")) >= 100,
        "done_test_executable": bool((card.get("idea") or {}).get("done_test")),
        "score_threshold": total >= 70,
        "independent_sources": source_count >= 3,
        "no_hard_blocks": not (score.get("hard_blocks") or []),
    }

    all_pass = all(gates.values())
    failing = [k for k, v in gates.items() if not v]

    # candidate type (from semantic_memory CandidateType)
    sid = (card.get("strategy") or {}).get("strategy_id") or ""
    if sid in {"doctrine-control-plane", "proof-false-done"}:
        candidate_type = "doctrine"
    elif (card.get("pattern") or {}).get("name"):
        candidate_type = "procedure"
    elif rank in {"STRONG", "EXCELLENT"}:
        candidate_type = "claim"
    else:
        candidate_type = "preference"

    # state
    if all_pass and rank in {"STRONG", "EXCELLENT"}:
        state = "READY_FOR_PROMOTION"
        next_action = "Submit to semantic_memory.promote_doctrine_candidate() with 3+ independent source families"
    elif gates["score_threshold"] and gates["evidence_density"]:
        state = "STAGED"
        next_action = f"Strengthen: {', '.join(failing)} then resubmit"
    else:
        state = "DRAFT"
        next_action = "Improve evidence density and mechanism depth before staging"

    return {
        "candidate_type": candidate_type,
        "state": state,
        "next_action": next_action,
        "gates": gates,
        "all_gates_pass": all_pass,
        "failing_gates": failing,
        "promotion_path": "DRAFT → STAGED → READY_FOR_PROMOTION → PROMOTED (via Obsidian + gBrain)",
        "sensitivity": "internal",
        "retention_class": "candidate",
    }


# ---------------------------------------------------------------------------
# Proof seal
# ---------------------------------------------------------------------------

def seal_proof(card: dict[str, Any]) -> dict[str, Any]:
    """Hash-chain proof binding the enriched artifact."""
    # strip existing proof + score for canonical hash
    clean = {k: v for k, v in card.items() if k not in {"proof_seal", "artifact_sha256"}}
    canonical = stable_json(clean)
    content_hash = sha256_text(canonical)

    # chain to previous (use latest ledger hash if available)
    ledger_path = L2_ROOT / "ledger.jsonl"
    prev_hash = ""
    if ledger_path.exists():
        lines = ledger_path.read_text(encoding="utf-8").strip().split("\n")
        if lines:
            try:
                prev = json.loads(lines[-1])
                prev_hash = prev.get("artifact_sha256") or prev.get("card_sha256") or ""
            except json.JSONDecodeError:
                pass

    this_hash = sha256_text(prev_hash + content_hash)

    return {
        "schema": "rig.omniscout.proof-seal.v1",
        "sealed_at": utc_now(),
        "content_hash": content_hash,
        "prev_hash": prev_hash[:16] if prev_hash else "genesis",
        "this_hash": this_hash,
        "sealer": "omniscout-l2-enrichment-v3",
        "chain_depth": sum(1 for _ in ledger_path.open()) if ledger_path.exists() else 0,
        "verifiable": True,
        "verify_command": f"python -c \"import hashlib; print(hashlib.sha256(open('{card.get('card_id')}.json','rb').read()).hexdigest())\"",
    }


# ---------------------------------------------------------------------------
# Enrich single card to v3
# ---------------------------------------------------------------------------


def enrich_card_v3(card_path: Path, all_cards_data: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Upgrade a v2 card to v3 with Memory OS patterns."""
    card = json.loads(card_path.read_text(encoding="utf-8"))

    if card.get("schema") == SCHEMA_V3:
        return {"ok": True, "card_id": card.get("card_id"), "status": "already_v3"}

    # load all cards for cross-referencing if not provided
    if all_cards_data is None:
        all_cards_data = []
        for p in L2_CARDS.glob("l2-*.json"):
            try:
                all_cards_data.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue

    # compute all v3 enrichments
    entities = extract_entities(card)
    memory_layer = assign_memory_layer(card)
    semantic_links = compute_semantic_links(card, all_cards_data)
    temporal = compute_temporal_validity(card)
    predictive = compute_predictive_context(card, semantic_links, all_cards_data)
    promotion = compute_promotion_state(card, card.get("analysis"))

    # upgrade card
    card["schema"] = SCHEMA_V3
    card["entities"] = entities
    card["memory_layer"] = memory_layer
    card["semantic_links"] = semantic_links
    card["temporal_validity"] = temporal
    card["predictive_context"] = predictive
    card["promotion_state"] = promotion
    card["enriched_v3_at"] = utc_now()

    # proof seal
    card["proof_seal"] = seal_proof(card)

    # re-score with new fields
    new_score = score_build_card(card)
    card["score"] = new_score
    card["artifact_sha256"] = sha256_text(stable_json({k: v for k, v in card.items() if k != "artifact_sha256"}))

    # write back
    atomic_json(card_path, card)

    # regenerate markdown
    md = _card_v3_to_markdown(card)
    atomic_text(card_path.with_suffix(".md"), md)

    return {
        "ok": True,
        "card_id": card.get("card_id"),
        "status": "enriched_to_v3",
        "entities": entities["entity_count"],
        "relationships": entities["relationship_count"],
        "memory_layer": memory_layer["layer_name"],
        "semantic_links": semantic_links["link_count"],
        "contradictions": semantic_links["contradiction_count"],
        "freshness": temporal["freshness"],
        "promotion_state": promotion["state"],
        "score": new_score.get("total"),
        "rank": new_score.get("rank"),
    }


def _card_v3_to_markdown(card: dict[str, Any]) -> str:
    """Full v3 markdown with Memory OS sections."""
    # start with v2 content
    from rig_foundry.omniscout_l2_enrich import _card_v2_to_markdown

    v2_md = _card_v2_to_markdown(card)
    entities = card.get("entities") or {}
    ml = card.get("memory_layer") or {}
    sl = card.get("semantic_links") or {}
    tv = card.get("temporal_validity") or {}
    pc = card.get("predictive_context") or {}
    ps = card.get("promotion_state") or {}
    proof = card.get("proof_seal") or {}

    v3_sections = [
        "\n---\n\n## Memory OS Layer",
        "",
        f"**Layer:** L{ml.get('layer',8)}/8 — {ml.get('layer_name','EVIDENCE')}",
        f"*{ml.get('layer_description','')}*",
        f"**Retention:** {ml.get('retention','')}",
        "",
        "## Entity Graph",
        "",
        f"Extracted **{entities.get('entity_count',0)}** entities ({entities.get('entity_types',{})}).",
        "",
    ]

    # entity table
    for e in (entities.get("entities") or [])[:15]:
        v3_sections.append(f"- `{e['type']}`: **{e['name']}**")
    v3_sections.append("")

    # relationships
    if entities.get("relationships"):
        v3_sections += ["### Entity Relationships", ""]
        for r in entities["relationships"][:8]:
            v3_sections.append(
                f"- `{r['from_name']}` → **{r['type']}** → `{r['to_name']}`"
            )
        v3_sections.append("")

    # semantic links
    v3_sections += [
        "## Semantic Cross-Links",
        "",
        f"- Links: **{sl.get('link_count',0)}** (supports: {sl.get('supports_count',0)}, extends: {sl.get('extends_count',0)})",
        f"- Graph density: {sl.get('graph_density',0)}",
        f"- Contradictions detected: **{sl.get('contradiction_count',0)}**",
        "",
    ]
    for link in (sl.get("links") or [])[:8]:
        v3_sections.append(
            f"- [{link['relationship']}]({link['card_id']}) — {link['title']} "
            f"(sim={link['similarity']}, {link['reason']})"
        )
    v3_sections.append("")

    # contradictions
    if sl.get("contradictions"):
        v3_sections += ["### ⚠️ Contradictions", ""]
        for c in sl["contradictions"]:
            v3_sections.append(
                f"- **CONTRADICTS** [{c['title']}]({c['card_id']}) (sim={c['similarity']})"
            )
        v3_sections.append("")

    # temporal validity
    v3_sections += [
        "## Temporal Validity",
        "",
        f"**Freshness:** {tv.get('freshness','?')}",
        f"**Evidence years:** {tv.get('evidence_year_range','?')}",
        f"**Age:** {tv.get('age_years',0)} years",
        f"**Decay window:** {tv.get('decay_window','')}",
        f"**Re-verify by:** {tv.get('reverify_by','?')}",
        "",
    ]

    # predictive context
    v3_sections += [
        "## Predictive Context",
        "",
        f"{pc.get('agent_briefing','')}",
        "",
        "### Prefetch Cards",
    ]
    for p in (pc.get("prefetch_cards") or [])[:5]:
        v3_sections.append(f"- [{p['title']}]({p['card_id']}) — {p['relationship']} (sim={p['similarity']})")
    v3_sections += [
        "",
        "### Doctrine Context",
        *[f"- `{f}`" for f in (pc.get("doctrine_context") or [])],
        "",
    ]

    # promotion state
    v3_sections += [
        "## Doctrine Promotion State",
        "",
        f"**State:** {ps.get('state','?')}",
        f"**Type:** {ps.get('candidate_type','?')}",
        f"**All gates pass:** {ps.get('all_gates_pass',False)}",
        f"**Next:** {ps.get('next_action','')}",
        "",
        "### Gates",
    ]
    for gate, passed in (ps.get("gates") or {}).items():
        icon = "✅" if passed else "❌"
        v3_sections.append(f"- {icon} `{gate}`")
    v3_sections += [
        "",
        f"**Path:** {ps.get('promotion_path','')}",
        "",
    ]

    # proof seal
    v3_sections += [
        "## Proof Seal",
        "",
        f"- **Hash:** `{proof.get('this_hash','')[:32]}...`",
        f"- **Prev:** `{proof.get('prev_hash','')[:16]}`",
        f"- **Chain depth:** {proof.get('chain_depth',0)}",
        f"- **Sealed at:** {proof.get('sealed_at','')}",
        f"- **Sealer:** {proof.get('sealer','')}",
        "",
    ]

    return v2_md + "\n".join(v3_sections) + "\n"


# ---------------------------------------------------------------------------
# Batch enrichment
# ---------------------------------------------------------------------------


def enrich_all_v3() -> dict[str, Any]:
    """Enrich every card to v3 with Memory OS patterns."""
    L2_CARDS.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    t0 = time.time()

    # load all cards ONCE for cross-referencing
    all_cards_data: list[dict[str, Any]] = []
    card_paths: list[Path] = []
    for p in sorted(L2_CARDS.glob("l2-*.json")):
        try:
            all_cards_data.append(json.loads(p.read_text(encoding="utf-8")))
            card_paths.append(p)
        except (OSError, json.JSONDecodeError):
            continue

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for path in card_paths:
        try:
            # reload card data (may have been enriched by previous iteration)
            all_cards_data = []
            for p2 in card_paths:
                try:
                    all_cards_data.append(json.loads(p2.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
            results.append(enrich_card_v3(path, all_cards_data))
        except Exception as exc:  # noqa: BLE001
            errors.append({"card_id": path.stem, "error": str(exc)[:300]})

    # build knowledge graph index
    graph = _build_graph_index(all_cards_data)

    summary = {
        "schema": "rig.omniscout.l2-enrichment-v3.v1",
        "ok": len(errors) == 0,
        "started_at": started,
        "finished_at": utc_now(),
        "elapsed_s": round(time.time() - t0, 2),
        "total_cards": len(card_paths),
        "enriched_v3": sum(1 for r in results if r.get("status") == "enriched_to_v3"),
        "already_v3": sum(1 for r in results if r.get("status") == "already_v3"),
        "errors": errors[:10],
        "graph_stats": graph,
        "at": utc_now(),
    }
    (L2_ROOT / "latest-enrichment-v3.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def _build_graph_index(all_cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a knowledge graph summary across all enriched cards."""
    total_entities: set[str] = set()
    total_links = 0
    total_contradictions = 0
    layer_dist: Counter = Counter()
    freshness_dist: Counter = Counter()
    promotion_states: Counter = Counter()
    entity_type_dist: Counter = Counter()

    for card in all_cards:
        for e in (card.get("entities") or {}).get("entities") or []:
            total_entities.add(e.get("entity_id") or e.get("name"))
            entity_type_dist[e.get("type")] += 1
        sl = card.get("semantic_links") or {}
        total_links += int(sl.get("link_count") or 0)
        total_contradictions += int(sl.get("contradiction_count") or 0)
        ml = card.get("memory_layer") or {}
        layer_dist[ml.get("layer_name")] += 1
        tv = card.get("temporal_validity") or {}
        freshness_dist[tv.get("freshness")] += 1
        ps = card.get("promotion_state") or {}
        promotion_states[ps.get("state")] += 1

    return {
        "unique_entities": len(total_entities),
        "total_links": total_links,
        "total_contradictions": total_contradictions,
        "layer_distribution": dict(layer_dist),
        "freshness_distribution": dict(freshness_dist),
        "promotion_states": dict(promotion_states),
        "entity_type_distribution": dict(entity_type_dist),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Enrich L2 cards to v3 (Memory OS)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("all", help="enrich all cards to v3")
    p_one = sub.add_parser("one", help="enrich one card")
    p_one.add_argument("path")
    sub.add_parser("status", help="show v3 status")
    sub.add_parser("graph", help="show knowledge graph summary")
    args = parser.parse_args(argv)

    if args.cmd == "all":
        out = enrich_all_v3()
    elif args.cmd == "one":
        out = enrich_card_v3(Path(args.path))
    elif args.cmd == "status":
        v3 = sum(1 for p in L2_CARDS.glob("l2-*.json") if json.loads(p.read_text()).get("schema") == SCHEMA_V3)
        v2 = sum(1 for p in L2_CARDS.glob("l2-*.json") if json.loads(p.read_text()).get("schema", "").endswith("v2"))
        out = {"v3": v3, "v2": v2, "total": v3 + v2}
    elif args.cmd == "graph":
        all_cards = [json.loads(p.read_text()) for p in L2_CARDS.glob("l2-*.json")]
        out = _build_graph_index(all_cards)
    else:
        parser.error("unknown")
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
