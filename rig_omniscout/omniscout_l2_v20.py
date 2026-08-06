"""OmniScout L2 V20 Council Enrichment Engine — multi-perspective deterministic review.

Each V10 build card is reviewed by 6 domain experts (no LLM calls):
- Business Strategist
- Marketing Director
- Competitive Intelligence
- Product Developer
- AI Architect
- Potential Client

A council synthesis then produces a consensus verdict, top actions, and confidence score.
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
    L2_CARDS, L2_ROOT, atomic_json, atomic_text, sha256_text, slugify,
    stable_json, utc_now, DOCTRINE_DOMAINS, score_build_card,
)
from rig_foundry.omniscout_l2_v10 import (  # noqa: E402
    _card_v10_to_markdown, SCHEMA_V10,
)

SCHEMA_V20 = "rig.omniscout.build-card.v20"

VOTE_GO = "GO"
VOTE_NO_GO = "NO-GO"
VOTE_CONDITIONAL = "CONDITIONAL"
VOTES = {VOTE_GO, VOTE_NO_GO, VOTE_CONDITIONAL}

# ---------------------------------------------------------------------------
# Strategy-aware templates (deterministic lenses)
# ---------------------------------------------------------------------------

STRATEGY_TEMPLATES: dict[str, dict[str, Any]] = {
    "agent-engineering": {
        "business_moat": "fractional CAIO + agent-build retainer",
        "marketing_hook": "RIG ships agent systems with deterministic done-tests",
        "competitor_set": ["Cognition", "Anthropic Consulting", "Sierra", "AutoGen"],
        "mvp_core": "closed-loop agent harness with builder/verifier split",
        "ai_pattern": "tool-calling loop with ProofPacket sealing",
        "buyer_pain": "AI prototypes never ship because nobody verifies them",
    },
    "proof-false-done": {
        "business_moat": "verification SaaS + audit licensing",
        "marketing_hook": "Every 'done' claim backed by a planted-failure regression",
        "competitor_set": ["SLSA", "Sigstore", "OpenSSF", "in-toto"],
        "mvp_core": "non-vacuity gate runner + artifact hash chain",
        "ai_pattern": "deterministic verifier agent + evidence receipt",
        "buyer_pain": "Teams ship broken code because tests pass by accident",
    },
    "local-inference-fleet": {
        "business_moat": "fleet management platform + consulting",
        "marketing_hook": "Run private LLM fleets on Apple Silicon + QNAP",
        "competitor_set": ["Ollama", "vLLM", "TGI", "Baseten"],
        "mvp_core": "multi-node model scheduler with health watchdog",
        "ai_pattern": "local model router with cost/latency fallbacks",
        "buyer_pain": "Cloud AI bills and data-sovereignty headaches",
    },
    "gtm-sales": {
        "business_moat": "GTM automation retainer + pipeline rev-share",
        "marketing_hook": "AI outbound that reads signal before it sends",
        "competitor_set": ["Clay", "Apollo", "Regie.ai", "Outreach"],
        "mvp_core": "signal-scored prospect queue + personalized sequencer",
        "ai_pattern": "research-to-draft agent with human Gate-D",
        "buyer_pain": "SDRs spray and pray; pipeline quality collapses",
    },
    "healthcare-ai-ops": {
        "business_moat": "healthcare AI ops retainer + RCM rev-share",
        "marketing_hook": "AI ops for DSOs that keeps PHI local",
        "competitor_set": ["Notable Health", "Olive", "Twill", "Hyro"],
        "mvp_core": "HIPAA-audited agent workflow with audit trail",
        "ai_pattern": " constrained retrieval agent with citation tracing",
        "buyer_pain": "EHR bloat and denied claims drain margin",
    },
    "knowledge-memory": {
        "business_moat": "knowledge platform + integration consulting",
        "marketing_hook": "A second brain that writes itself from agent work",
        "competitor_set": ["Mem.ai", "Notion AI", "Obsidian", "Glean"],
        "mvp_core": "entity extractor + graph linker + recall API",
        "ai_pattern": "embedding + keyword hybrid retrieval with provenance",
        "buyer_pain": "Institutional knowledge lives in scattered tools",
    },
    "scraping-intelligence": {
        "business_moat": "data pipeline SaaS + custom scraping",
        "marketing_hook": "Turn any public signal into structured intel",
        "competitor_set": ["Bright Data", "Apify", "ScrapingBee", "Octoparse"],
        "mvp_core": "resilient scrape runner with schema extraction",
        "ai_pattern": "browser-to-structured-data agent with retry escalation",
        "buyer_pain": "Market intelligence is manual and already stale",
    },
    "automation-runtime": {
        "business_moat": "workflow orchestration platform + managed service",
        "marketing_hook": "Long-running agent workflows that survive crashes",
        "competitor_set": ["Temporal", "Prefect", "n8n", "Make"],
        "mvp_core": "durable job graph with checkpoint/restore",
        "ai_pattern": "state-machine agent with idempotent steps",
        "buyer_pain": "Ad-hoc scripts fail silently in production",
    },
    "determinism-gates": {
        "business_moat": "CI/verification tooling + enterprise licensing",
        "marketing_hook": "Deterministic gates that refuse to lie about done",
        "competitor_set": ["GitHub Actions", "CircleCI", "Buildkite", "SLSA"],
        "mvp_core": "deterministic verifier harness with planted failures",
        "ai_pattern": "verifier agent with structured pass/fail receipts",
        "buyer_pain": "Agent builds claim success but can't prove it",
    },
    "doctrine-control-plane": {
        "business_moat": "doctrine governance platform + consulting",
        "marketing_hook": "Keep every agent aligned as doctrine evolves",
        "competitor_set": ["OpenAI custom instructions", "Cursor rules", "GitHub Copilot"],
        "mvp_core": "versioned rule compiler + compliance checker",
        "ai_pattern": "policy-anchored retrieval with contradiction detection",
        "buyer_pain": "Agent behavior drifts as teams and prompts multiply",
    },
}

DEFAULT_TEMPLATE: dict[str, Any] = {
    "business_moat": "consulting + custom build",
    "marketing_hook": "RIG capability built from Consensus-backed research",
    "competitor_set": ["generic AI consultancies", "offshore dev shops"],
    "mvp_core": "harness with deterministic done-test and ProofPacket sealing",
    "ai_pattern": "closed-loop agent with independent verifier",
    "buyer_pain": "Leadership wants AI outcomes but lacks verifiable delivery",
}

TIER_GO_BIAS = {"T0": 2, "T1": 1, "T2": 0, "na": 0}
TIER_NAMES = {"T0": "core", "T1": "revenue", "T2": "moat", "na": "unmapped"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _card_text_blob(card: dict[str, Any]) -> str:
    parts = [
        str(card.get("title") or ""),
        str(card.get("claim") or ""),
        str(card.get("summary") or ""),
        str(card.get("mechanism") or ""),
        str(card.get("why_not_median") or ""),
    ]
    return "\n".join(parts).lower()


def _strategy(card: dict[str, Any]) -> dict[str, Any]:
    return card.get("strategy") or {}


def _strategy_id(card: dict[str, Any]) -> str:
    return str((_strategy(card).get("strategy_id") or "unmapped")).lower()


def _tier(card: dict[str, Any]) -> str:
    return str(_strategy(card).get("tier") or "na").upper()


def _template(card: dict[str, Any]) -> dict[str, Any]:
    return STRATEGY_TEMPLATES.get(_strategy_id(card), DEFAULT_TEMPLATE)


def _score_total(card: dict[str, Any]) -> int:
    return int((card.get("score") or {}).get("total") or 0)


def _rank(card: dict[str, Any]) -> str:
    return str((card.get("score") or {}).get("rank") or "UNKNOWN")


def _promote(card: dict[str, Any]) -> bool:
    return bool((card.get("score") or {}).get("promote"))


def _source_count(card: dict[str, Any]) -> int:
    s = card.get("sources") or {}
    return int(s.get("count") or len(s.get("urls") or []) or 0)


def _entity_names(card: dict[str, Any]) -> list[str]:
    return [str(e.get("name") or "") for e in (card.get("entities") or {}).get("entities", []) if e.get("name")]


def _tool_entities(card: dict[str, Any]) -> list[str]:
    return [str(e.get("name") or "") for e in (card.get("entities") or {}).get("entities", []) if e.get("type") == "TOOL"]


def _domain_alignment(card: dict[str, Any]) -> list[str]:
    """Return doctrine domains that match card entities/text."""
    blob = _card_text_blob(card)
    entities = set(_entity_names(card))
    aligned: list[str] = []
    for domain, markers in DOCTRINE_DOMAINS.items():
        score = sum(1 for m in markers if m in blob or any(m in e for e in entities))
        if score >= 2:
            aligned.append(domain)
    return aligned or list((card.get("doctrine_domains") or [])[:2]) or ["engineering-capability"]


def _detected_competitors(card: dict[str, Any]) -> list[str]:
    """Look for known competitors in entities, OSS list, and text."""
    text = _card_text_blob(card)
    known = set(_template(card).get("competitor_set", []))
    for p in (card.get("oss_integration") or {}).get("relevant_projects", []):
        known.add(str(p.get("project") or ""))
    for e in _entity_names(card):
        known.add(e)
    found = sorted({c for c in known if c and len(c) > 2 and c.lower() in text})
    return found or list(_template(card).get("competitor_set", []))[:3]


def _vote_from_score(card: dict[str, Any], bias: int = 0) -> str:
    """Map score/rank/promote to a council vote with optional tier bias."""
    total = _score_total(card) + bias * 3
    if _promote(card) and total >= 70:
        return VOTE_GO
    if total >= 60:
        return VOTE_CONDITIONAL
    return VOTE_NO_GO


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


# ---------------------------------------------------------------------------
# Six council perspectives (deterministic)
# ---------------------------------------------------------------------------


def _business_strategist(card: dict[str, Any]) -> dict[str, Any]:
    sid = _strategy_id(card)
    tier = _tier(card)
    biz = card.get("business_intelligence") or {}
    score_total = _score_total(card)
    rank = _rank(card)
    template = _template(card)

    price = str(biz.get("price_range") or "unknown")
    margin = str(biz.get("gross_margin") or "unknown")
    ltv_cac = str(biz.get("ltv_cac_ratio") or "unknown")
    tam = str(biz.get("tam") or "unknown")

    analysis = (
        f"Strategy `{sid}` ({TIER_NAMES.get(tier, tier)} tier) proposes a {template['business_moat']} model. "
        f"Price band {price} at {margin} gross margin; LTV/CAC {ltv_cac}. "
        f"Addressable market {tam}. Card scores {score_total}/100 ({rank}). "
        f"{'Promote flag is set' if _promote(card) else 'Promote flag is not set'}; "
        f"{_source_count(card)} independent sources back the claim."
    )

    recs: list[str] = []
    recs.append(f"Validate pricing power for `{sid}` with 3 prospect conversations")
    if "N/A" in ltv_cac or "unknown" in ltv_cac:
        recs.append("Fix LTV/CAC math: model actual sales cycle and churn")
    if tier == "T0":
        recs.append("Open-source a narrow slice to drive top-of-funnel enterprise leads")
    if score_total >= 80:
        recs.append("Package as a fixed-scope pilot offering to shorten sales cycle")

    if tier in {"T0", "T1"} and _promote(card):
        risk = "Execution risk: high-margin retainer depends on delivery velocity and client education"
    elif score_total >= 70:
        risk = "Market timing risk: buyers may not yet budget for this capability"
    else:
        risk = "Unit-economics risk: unclear pricing power and unproven willingness-to-pay"

    question = (
        "Product Developer: can the MVP ship in under 4 weeks with a real done-test, "
        "or does scope blow up the initial price point?"
    )

    vote = _vote_from_score(card, bias=TIER_GO_BIAS.get(tier, 0))
    if tier == "T0" and score_total >= 75:
        vote = VOTE_GO

    return {
        "role": "business_strategist",
        "analysis": analysis,
        "recommendation": recs[:4],
        "risk": risk,
        "question_to_council": question,
        "vote": vote,
    }


def _marketing_director(card: dict[str, Any]) -> dict[str, Any]:
    sid = _strategy_id(card)
    tier = _tier(card)
    gtm = card.get("gtm_strategy") or {}
    template = _template(card)
    score_total = _score_total(card)

    icp = str(gtm.get("icp") or "mid-market operators")
    channels = ", ".join(gtm.get("channels") or ["LinkedIn", "Content"])
    positioning = str(gtm.get("positioning") or f"RIG {sid} capability")
    hook = template["marketing_hook"]

    entity_count = (card.get("entities") or {}).get("entity_count", 0)
    link_count = (card.get("semantic_links") or {}).get("link_count", 0)

    analysis = (
        f"Positioning angle: '{hook}'. ICP is {icp} via {channels}. "
        f"Entity graph has {entity_count} concepts and {link_count} semantic links, "
        f"giving content creators concrete anchors. Score {score_total}/100. "
        f"Tier {tier} signals priority {'T0 → flagship narrative' if tier == 'T0' else ('T1 → revenue narrative' if tier == 'T1' else 'T2 → depth narrative')}."
    )

    recs = [
        f"Lead with the '{hook}' hook in the next LinkedIn post",
        f"Create a 'before/after' case frame for {icp}",
        "Turn the card's Consensus evidence into a credibility slide",
    ]
    if tier in {"T0", "T1"}:
        recs.append("Run a 2-week demand-gen sprint: one post + one video + one essay")
    if gtm.get("content_plan"):
        recs.append(f"Execute the first content item: {gtm['content_plan'][0]['type']} on {gtm['content_plan'][0]['topic'][:60]}")

    if score_total >= 75 and tier in {"T0", "T1"}:
        risk = "Channel saturation risk: many AI consultancies compete for the same LinkedIn attention"
    elif score_total >= 60:
        risk = "Message clarity risk: abstract mechanism may not convert to buyer language"
    else:
        risk = "ICP-fit risk: unclear who pays first and why now"

    question = (
        "Potential Client: what specific job-to-be-done would make you budget for this in 30 days?"
    )

    vote = _vote_from_score(card, bias=TIER_GO_BIAS.get(tier, 0))
    if tier in {"T0", "T1"} and score_total >= 68:
        vote = VOTE_GO

    return {
        "role": "marketing_director",
        "analysis": analysis,
        "recommendation": recs[:4],
        "risk": risk,
        "question_to_council": question,
        "vote": vote,
    }


def _competitive_intelligence(card: dict[str, Any]) -> dict[str, Any]:
    sid = _strategy_id(card)
    tier = _tier(card)
    template = _template(card)
    competitors = _detected_competitors(card)
    moat = (card.get("business_intelligence") or {}).get("competitive_moat", [])
    oss = (card.get("oss_integration") or {}).get("relevant_projects", [])
    score_total = _score_total(card)

    diff_score = min(10, len(moat) * 2 + len(oss))
    differentiation = "strong" if diff_score >= 7 else ("moderate" if diff_score >= 4 else "weak")

    analysis = (
        f"Known competitor set for `{sid}`: {', '.join(competitors[:5]) or 'none detected'}. "
        f"Differentiation score {diff_score}/10 ({differentiation}) based on {len(moat)} moat claims and {len(oss)} OSS integrations. "
        f"RIG's proof-chain + local-fleet + Consensus backing create defensibility if execution keeps pace."
    )

    recs = [
        f"Publish a comparison page: RIG {sid} vs {competitors[0] if competitors else 'incumbents'}",
        "Open-source one non-core module to shape the category conversation",
    ]
    if moat:
        recs.append(f"Turn the top moat into a proof point: {moat[0]}")
    if len(oss) >= 3:
        recs.append("Contribute a small integration PR to the most aligned OSS project")

    if differentiation == "strong":
        risk = "Threat: incumbents may copy the narrative before RIG ships the product"
    elif differentiation == "moderate":
        risk = "Threat: feature parity arms race with better-funded tools"
    else:
        risk = "Threat: commodity positioning; no durable moat visible"

    question = (
        "Business Strategist: which moat element is hardest to replicate and easiest to demonstrate to a buyer?"
    )

    vote = _vote_from_score(card)
    if differentiation == "strong" and score_total >= 65:
        vote = VOTE_GO
    elif differentiation == "weak" and score_total < 60:
        vote = VOTE_NO_GO
    else:
        vote = VOTE_CONDITIONAL if vote == VOTE_NO_GO else vote

    return {
        "role": "competitive_intelligence",
        "analysis": analysis,
        "recommendation": recs[:4],
        "risk": risk,
        "question_to_council": question,
        "vote": vote,
    }


def _product_developer(card: dict[str, Any]) -> dict[str, Any]:
    sid = _strategy_id(card)
    tier = _tier(card)
    eng = card.get("engineering_blueprint") or {}
    template = _template(card)
    score_total = _score_total(card)

    steps = eng.get("implementation_steps") or []
    stack = eng.get("tech_stack") or {"backend": ["Python"]}
    complexity = str(eng.get("complexity") or "MEDIUM")
    loc = str(eng.get("estimated_loc") or "300-800")
    build_effort = str((card.get("business_intelligence") or {}).get("build_effort") or "2-4 weeks")

    p0 = [template["mvp_core"]]
    p1 = ["ProofPacket sealing + hash chain", "Obsidian export + memory-layer sync"]
    p2 = ["Multi-node fleet scaling", "Enterprise RBAC + billing"]

    analysis = (
        f"MVP for `{sid}` centers on {p0[0]}. Tech stack: {json.dumps(stack)}. "
        f"Complexity {complexity}, estimated {loc} LOC, build effort {build_effort}. "
        f"{len(steps)} implementation steps defined. Score {score_total}/100."
    )

    recs = [
        f"P0: ship {p0[0]} with an executable done-test",
        f"P1: {'; '.join(p1[:2])}",
        f"P2: {'; '.join(p2[:2])}",
    ]
    if complexity == "HIGH":
        recs.append("Cut scope: pick one happy-path scenario for first release")
    if tier == "T0":
        recs.append("Add non-vacuity regression: plant a failure, confirm gate goes RED")

    if complexity == "HIGH" and score_total < 75:
        risk = "Technical risk: scope exceeds current team bandwidth; timeline likely slips"
    elif "N/A" in str(eng.get("estimated_loc") or "") or not steps:
        risk = "Specification risk: implementation plan is thin or missing"
    else:
        risk = "Integration risk: OSS dependencies may change before RIG can productize"

    question = (
        "AI Architect: which model or agent pattern is actually required at launch, "
        "and which can be deterministic code?"
    )

    vote = _vote_from_score(card)
    if complexity == "HIGH" and score_total < 70:
        vote = VOTE_CONDITIONAL if vote == VOTE_GO else vote
    if tier in {"T0", "T1"} and score_total >= 72:
        vote = VOTE_GO

    return {
        "role": "product_developer",
        "analysis": analysis,
        "recommendation": recs[:4],
        "risk": risk,
        "question_to_council": question,
        "vote": vote,
    }


def _ai_architect(card: dict[str, Any]) -> dict[str, Any]:
    sid = _strategy_id(card)
    tier = _tier(card)
    team = card.get("agent_team") or {}
    eng = card.get("engineering_blueprint") or {}
    template = _template(card)
    score_total = _score_total(card)

    agents = team.get("agents") or []
    agent_count = len(agents)
    model_set = {str(a.get("model") or "unknown") for a in agents}
    pattern = template["ai_pattern"]

    # failure catalog
    failures: list[str] = [
        "Model drift: output format changes across provider updates",
        "Tool hallucination: agent calls non-existent or wrong tool",
        "Verifier capture: builder and verifier share context or model weights",
    ]
    if sid in {"proof-false-done", "determinism-gates"}:
        failures.append("Vacuous pass: gate succeeds without checking the real condition")
    if sid in {"gtm-sales", "healthcare-ai-ops"}:
        failures.append("Gate-D bypass: agent sends outward action without human approval")

    # rough cost estimate
    cost_per_inference = "$0.002-0.02" if "qwen3:8b" in {m.lower() for m in model_set} else "$0.01-0.10"
    if "local" in sid or "ollama" in {m.lower() for m in model_set}:
        cost_per_inference = "$0.0001-0.001 (local) + fleet amortization"

    analysis = (
        f"Agent design for `{sid}` uses {agent_count} agent(s) ({', '.join(model_set) or 'none specified'}) "
        f"around pattern '{pattern}'. Cost-per-inference estimate {cost_per_inference}. "
        f"Failure catalog has {len(failures)} items. Score {score_total}/100."
    )

    recs = [
        f"Implement pattern: {pattern}",
        "Add structured output schemas (Pydantic) for every agent boundary",
        "Build eval harness with planted failures before any model selection finalization",
    ]
    if agent_count >= 2:
        recs.append("Enforce GEV separation: builder and verifier must differ in model or role")
    if sid in {"proof-false-done", "determinism-gates"}:
        recs.append("Make the verifier deterministic; only builder may use LLM heuristics")

    if agent_count < 2:
        risk = "Architectural risk: single-agent design cannot enforce independent verification"
    elif not team.get("skills_required"):
        risk = "Skill gap: no required skill set defined; integration will be ad-hoc"
    else:
        risk = "Eval risk: without planted-failure regressions, model changes will silently break guarantees"

    question = (
        "Potential Client: would you accept a deterministic verifier even if the agent's output is occasionally conservative?"
    )

    vote = _vote_from_score(card)
    if agent_count >= 2 and score_total >= 70:
        vote = VOTE_GO
    elif agent_count < 2 and score_total < 65:
        vote = VOTE_CONDITIONAL if vote == VOTE_GO else vote

    return {
        "role": "ai_architect",
        "analysis": analysis,
        "recommendation": recs[:4],
        "risk": risk,
        "question_to_council": question,
        "vote": vote,
    }


def _potential_client(card: dict[str, Any]) -> dict[str, Any]:
    sid = _strategy_id(card)
    tier = _tier(card)
    template = _template(card)
    biz = card.get("business_intelligence") or {}
    gtm = card.get("gtm_strategy") or {}
    score_total = _score_total(card)

    pain = template["buyer_pain"]
    price = str(biz.get("price_range") or "unknown")
    icp = str(gtm.get("icp") or "mid-market operators")

    # alternatives from competitor set + generic options
    alternatives = _template(card).get("competitor_set", [])[:3] + ["build in-house", "do nothing"]

    # decision criteria
    criteria = [
        "Can you prove it works on my data before I pay?",
        "Is the output auditable and deterministic?",
        "Will it ship inside 30 days or less?",
    ]
    if tier == "T0":
        criteria.append("Can I self-host to keep data on-prem?")

    analysis = (
        f"As a {icp} buyer, the pain is: '{pain}'. Price point {price} feels "
        f"{'reasonable for a proven pilot' if score_total >= 75 else 'high without a live demo'}. "
        f"Alternatives considered: {', '.join(alternatives)}. Decision criteria center on proof, auditability, and speed."
    )

    recs = [
        f"Offer a fixed-price pilot at the low end of {price} with a 14-day exit clause",
        "Provide a live demo using the buyer's own data or a public proxy",
        "Share the ProofPacket audit trail as a trust artifact",
    ]
    if tier == "T0":
        recs.append("Include a self-hosted option in the first proposal")

    if score_total >= 75:
        risk = "Buyer risk: low — evidence and proof points are strong enough to justify budget"
    elif score_total >= 60:
        risk = "Buyer risk: medium — buyer will demand a pilot before committing"
    else:
        risk = "Buyer risk: high — unclear ROI and no concrete proof of execution"

    what_makes_me_say_yes = (
        f"I say yes when I see a 14-day pilot with a deterministic done-test, "
        f"an audit trail, and a price no higher than the bottom of {price}."
    )

    question = (
        "Business Strategist: what is the smallest commercial commitment that funds the build without killing margin?"
    )

    vote = _vote_from_score(card)
    if score_total >= 75:
        vote = VOTE_GO
    elif score_total < 55:
        vote = VOTE_NO_GO
    else:
        vote = VOTE_CONDITIONAL

    return {
        "role": "potential_client",
        "analysis": analysis,
        "recommendation": recs[:4],
        "risk": risk,
        "question_to_council": question,
        "vote": vote,
        "what_would_make_me_say_yes": what_makes_me_say_yes,
    }


# ---------------------------------------------------------------------------
# Council synthesis
# ---------------------------------------------------------------------------


def _synthesize_council(card: dict[str, Any], perspectives: list[dict[str, Any]]) -> dict[str, Any]:
    votes = [p["vote"] for p in perspectives]
    vote_counts = Counter(votes)
    go = vote_counts.get(VOTE_GO, 0)
    no_go = vote_counts.get(VOTE_NO_GO, 0)
    conditional = vote_counts.get(VOTE_CONDITIONAL, 0)

    # consensus points: shared themes across perspectives
    consensus: list[str] = []
    if go >= 4:
        consensus.append("Majority believes the capability is worth building now")
    if _promote(card):
        consensus.append("Deterministic scorer already flags the card for promotion")
    if _source_count(card) >= 3:
        consensus.append("Multi-source evidence base gives the council confidence")
    if any(p["role"] == "ai_architect" and p["vote"] == VOTE_GO for p in perspectives):
        consensus.append("AI architect sees a verifiable agent design path")
    if not consensus:
        consensus.append("Council agrees the card needs more evidence before firm commitment")

    # disagreements
    disagreements: list[str] = []
    if go >= 1 and no_go >= 1:
        disagreements.append("Business/momentum view clashes with ri[REDACTED] members")
    if conditional >= 3:
        disagreements.append("Most members want conditions met before full GO")
    if any(p["vote"] == VOTE_NO_GO for p in perspectives):
        disagreements.append(f"{[p['role'] for p in perspectives if p['vote'] == VOTE_NO_GO]} see blocking risks")
    if not disagreements and conditional > 0:
        disagreements.append("All members are directionally aligned but some want guardrails")

    # top 3 actions: collect all recs, score by vote weight, pick top 3
    rec_pool: list[tuple[int, str]] = []
    for p in perspectives:
        weight = {"GO": 3, "CONDITIONAL": 2, "NO-GO": 1}.get(p["vote"], 1)
        for r in p.get("recommendation", []):
            rec_pool.append((weight, r))
    # stable sort by weight descending, then dedupe preserving order
    seen = set()
    top_actions: list[str] = []
    for weight, r in sorted(rec_pool, key=lambda x: (-x[0], x[1])):
        key = slugify(r, max_len=80)
        if key not in seen:
            seen.add(key)
            top_actions.append(r)
        if len(top_actions) >= 3:
            break

    # verdict
    if go >= 4 and no_go == 0 and _promote(card):
        verdict = "SHIP"
    elif go >= 3 and no_go <= 1:
        verdict = "ITERATE"
    elif no_go >= 3:
        verdict = "KILL"
    else:
        verdict = "RESEARCH_MORE"

    # confidence 0-100
    alignment = go + conditional * 0.5
    confidence = _clamp(int((alignment / 6) * 100), 0, 100)
    if _promote(card):
        confidence = _clamp(confidence + 5, 0, 100)
    if no_go >= 1:
        confidence = _clamp(confidence - 10, 0, 100)

    # dissent note
    dissenters = [p["role"] for p in perspectives if p["vote"] == VOTE_NO_GO]
    if dissenters:
        dissent_note = (
            f"Strong minority dissent from {', '.join(dissenters)}; "
            "address their blocking risks before advancing."
        )
    elif conditional >= 3:
        dissent_note = "No formal dissent, but most members attached conditions to their GO."
    else:
        dissent_note = "Council is broadly aligned."

    return {
        "consensus_points": consensus[:4],
        "disagreements": disagreements[:4],
        "top_3_actions": top_actions[:3],
        "overall_verdict": verdict,
        "confidence_score": confidence,
        "dissent_note": dissent_note,
        "vote_tally": {"GO": go, "NO-GO": no_go, "CONDITIONAL": conditional},
    }


# ---------------------------------------------------------------------------
# Enrich single card
# ---------------------------------------------------------------------------


def enrich_card_v20(card_path: Path, all_cards_data: list[dict] | None = None) -> dict[str, Any]:
    card = json.loads(card_path.read_text(encoding="utf-8"))

    if all_cards_data is None:
        all_cards_data = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in L2_CARDS.glob("l2-*.json")
            if p != card_path
        ]

    # Ensure V10 base exists; skip only if already V20
    if card.get("schema") == SCHEMA_V20:
        return {"ok": True, "card_id": card.get("card_id"), "status": "already_v20", "fields": len(card.keys())}

    # Run council
    perspectives = [
        _business_strategist(card),
        _marketing_director(card),
        _competitive_intelligence(card),
        _product_developer(card),
        _ai_architect(card),
        _potential_client(card),
    ]
    synthesis = _synthesize_council(card, perspectives)

    council = {
        "schema": "rig.omniscout.council-review.v1",
        "reviewed_at": utc_now(),
        "perspectives": perspectives,
        "synthesis": synthesis,
    }

    # Derived top-level fields
    council_summary = {
        "verdict": synthesis["overall_verdict"],
        "confidence": synthesis["confidence_score"],
        "go_votes": synthesis["vote_tally"]["GO"],
        "lead_action": synthesis["top_3_actions"][0] if synthesis["top_3_actions"] else "none",
    }

    card["council"] = council
    card["council_summary"] = council_summary
    card["council_votes"] = synthesis["vote_tally"]
    card["council_verdict"] = synthesis["overall_verdict"]
    card["council_confidence"] = synthesis["confidence_score"]
    card["council_hash"] = sha256_text(stable_json(council))
    card["v20_ready"] = synthesis["overall_verdict"] in {"SHIP", "ITERATE"}
    card["schema"] = SCHEMA_V20
    card["enriched_v20_at"] = utc_now()

    new_score = score_build_card(card)
    card["score"] = new_score
    card["artifact_sha256"] = sha256_text(stable_json({k: v for k, v in card.items() if k != "artifact_sha256"}))

    atomic_json(card_path, card)
    md = _card_v20_to_markdown(card)
    atomic_text(card_path.with_suffix(".md"), md)

    return {
        "ok": True,
        "card_id": card.get("card_id"),
        "status": "enriched_to_v20",
        "fields": len(card.keys()),
        "schema": card["schema"],
        "council_verdict": synthesis["overall_verdict"],
        "council_confidence": synthesis["confidence_score"],
        "score": new_score.get("total"),
        "rank": new_score.get("rank"),
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _card_v20_to_markdown(card: dict[str, Any]) -> str:
    """Append V20 Council sections to existing V10 markdown."""
    v10_md = _card_v10_to_markdown(card)
    council = card.get("council") or {}
    synthesis = council.get("synthesis") or {}
    perspectives = council.get("perspectives") or []

    lines: list[str] = [
        "",
        "---",
        "",
        "## V20: Council Review",
        "",
        f"**Verdict:** `{synthesis.get('overall_verdict','?')}` | "
        f"**Confidence:** {synthesis.get('confidence_score',0)}% | "
        f"**Votes:** GO={synthesis.get('vote_tally',{}).get('GO',0)} "
        f"CONDITIONAL={synthesis.get('vote_tally',{}).get('CONDITIONAL',0)} "
        f"NO-GO={synthesis.get('vote_tally',{}).get('NO-GO',0)}",
        "",
        "### Top 3 Actions",
        *[f"{i+1}. {a}" for i, a in enumerate(synthesis.get("top_3_actions", []))],
        "",
        "### Consensus Points",
        *[f"- {p}" for p in synthesis.get("consensus_points", [])],
        "",
        "### Disagreements",
        *[f"- {d}" for d in synthesis.get("disagreements", [])],
        "",
        f"**Dissent note:** {synthesis.get('dissent_note','')}",
        "",
        "### Perspectives",
    ]

    for p in perspectives:
        lines.extend([
            "",
            f"#### {p.get('role','member').replace('_',' ').title()}",
            f"- **Vote:** `{p.get('vote','?')}`",
            f"- **Analysis:** {p.get('analysis','')}",
            f"- **Risk:** {p.get('risk','')}",
            f"- **Question to council:** {p.get('question_to_council','')}",
            "- **Recommendations:**",
            *[f"  - {r}" for r in p.get("recommendation", [])],
        ])
        if p.get("what_would_make_me_say_yes"):
            lines.append(f"- **What would make me say yes:** {p['what_would_make_me_say_yes']}")

    lines.extend([
        "",
        f"_Council hash:_ `{card.get('council_hash','')}`",
        f"_Enriched V20 at:_ {card.get('enriched_v20_at','')}",
        "",
    ])

    return v10_md + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Enrich all cards
# ---------------------------------------------------------------------------


def _build_graph(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a simple node/edge graph from cards and semantic links."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: set[str] = set()

    for c in cards:
        cid = c.get("card_id")
        if not cid:
            continue
        node_ids.add(cid)
        nodes.append({
            "id": cid,
            "title": str(c.get("title") or "")[:60],
            "strategy_id": _strategy_id(c),
            "tier": _tier(c),
            "verdict": c.get("council_verdict") or (c.get("council") or {}).get("synthesis", {}).get("overall_verdict", "UNKNOWN"),
            "confidence": c.get("council_confidence") or (c.get("council") or {}).get("synthesis", {}).get("confidence_score", 0),
            "score": _score_total(c),
        })
        for link in (c.get("semantic_links") or {}).get("links", []):
            target = link.get("card_id")
            if target and target in node_ids:
                edges.append({
                    "source": cid,
                    "target": target,
                    "relationship": link.get("relationship", "RELATED_TO"),
                    "similarity": link.get("similarity", 0.0),
                })

    return {"node_count": len(nodes), "edge_count": len(edges), "nodes": nodes, "edges": edges}


def enrich_all_v20() -> dict[str, Any]:
    L2_CARDS.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    t0 = time.time()

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
            results.append(enrich_card_v20(path, all_cards_data))
        except Exception as exc:
            errors.append({"card_id": path.stem, "error": str(exc)[:300]})

    # reload enriched cards for graph
    enriched_cards: list[dict[str, Any]] = []
    for p in card_paths:
        try:
            enriched_cards.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue

    graph = _build_graph(enriched_cards)
    verdict_counts = Counter(r.get("council_verdict") or "UNKNOWN" for r in results if r.get("status") == "enriched_to_v20")

    summary = {
        "schema": "rig.omniscout.l2-enrichment-v20.v1",
        "ok": len(errors) == 0,
        "started_at": started,
        "finished_at": utc_now(),
        "elapsed_s": round(time.time() - t0, 2),
        "total_cards": len(card_paths),
        "enriched_v20": sum(1 for r in results if r.get("status") == "enriched_to_v20"),
        "already_v20": sum(1 for r in results if r.get("status") == "already_v20"),
        "errors": errors[:10],
        "verdict_counts": dict(verdict_counts),
        "graph": {
            "node_count": graph["node_count"],
            "edge_count": graph["edge_count"],
            "verdict_distribution": dict(verdict_counts),
        },
        "at": utc_now(),
    }
    atomic_json(L2_ROOT / "latest-enrichment-v20.json", summary)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _status() -> dict[str, Any]:
    total = len(list(L2_CARDS.glob("l2-*.json")))
    v20 = 0
    verdicts: Counter = Counter()
    for p in L2_CARDS.glob("l2-*.json"):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            if c.get("schema") == SCHEMA_V20:
                v20 += 1
                verdicts[c.get("council_verdict", "UNKNOWN")] += 1
        except (OSError, json.JSONDecodeError):
            continue
    return {
        "schema": SCHEMA_V20,
        "total": total,
        "v20": v20,
        "remaining": total - v20,
        "verdict_distribution": dict(verdicts),
    }


def _graph() -> dict[str, Any]:
    cards: list[dict[str, Any]] = []
    for p in sorted(L2_CARDS.glob("l2-*.json")):
        try:
            cards.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return _build_graph(cards)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="V20 Council Enrichment Engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("all")
    p_one = sub.add_parser("one")
    p_one.add_argument("path")
    sub.add_parser("status")
    sub.add_parser("graph")
    args = parser.parse_args(argv)

    if args.cmd == "all":
        out = enrich_all_v20()
    elif args.cmd == "one":
        out = enrich_card_v20(Path(args.path))
    elif args.cmd == "status":
        out = _status()
    elif args.cmd == "graph":
        out = _graph()
    else:
        parser.error("unknown subcommand")
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok", True) is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
