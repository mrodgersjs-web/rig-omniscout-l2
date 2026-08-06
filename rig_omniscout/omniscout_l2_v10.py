"""OmniScout L2 V10 Capability Engine — turns research cards into RIG build blueprints.

Each card gets 10 enrichment layers that make it a complete operational artifact:

V4: Business Intelligence — revenue model, pricing, unit economics, market sizing
V5: Engineering Blueprint — architecture, tech stack, harness design, implementation steps
V6: Agent & Team Design — agent types, roles, skills, PAI substrate, team composition
V7: Doctrine & Governance — policy rules, safety gates, proof requirements
V8: Go-to-Market — ICP, outreach, content plan, sales motion, channel strategy
V9: Open Source Integration — relevant OSS, integration points, fork/build decisions
V10: World Model Context — RIG 1000x vision fit, dependencies, roadmap, Jake briefing

Output surfaces:
- Obsidian Jake vault (Jake context)
- RIG Memory OS (event capture + promotion candidate)
- QNAP backup (durable store)
- Recall local corpus (knowledge base)
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

SCHEMA_V10 = "rig.omniscout.build-card.v10"
JAKE = Path(os.environ.get("JAKESTUDIO_VAULT", str(Path.home() / "Documents" / "JakeStudio")))

# ---------------------------------------------------------------------------
# V4: Business Intelligence
# ---------------------------------------------------------------------------

BUSINESS_MODELS: dict[str, dict[str, Any]] = {
    "agent-engineering": {"model": "Fractional CAIO + agent build retainer", "price_range": "$8K-25K/mo", "cac": "$2K-5K", "ltv": "$96K-300K", "margin": "70%"},
    "proof-false-done": {"model": "Verification SaaS + audit licensing", "price_range": "$500-5K/mo", "cac": "$1K-3K", "ltv": "$24K-60K", "margin": "85%"},
    "local-inference-fleet": {"model": "Fleet management platform + consulting", "price_range": "$2K-15K/mo", "cac": "$3K-8K", "ltv": "$48K-180K", "margin": "75%"},
    "gtm-sales": {"model": "GTM automation retainer + pipeline rev-share", "price_range": "$10K-30K/mo", "cac": "$5K-10K", "ltv": "$120K-360K", "margin": "65%"},
    "healthcare-ai-ops": {"model": "Healthcare AI ops retainer + RCM rev-share", "price_range": "$15K-50K/mo", "cac": "$10K-25K", "ltv": "$180K-600K", "margin": "60%"},
    "knowledge-memory": {"model": "Knowledge platform + integration consulting", "price_range": "$3K-12K/mo", "cac": "$2K-6K", "ltv": "$36K-144K", "margin": "80%"},
    "scraping-intelligence": {"model": "Data pipeline SaaS + custom scraping", "price_range": "$1K-8K/mo", "cac": "$1K-4K", "ltv": "$12K-96K", "margin": "82%"},
    "automation-runtime": {"model": "Workflow orchestration platform + managed service", "price_range": "$2K-10K/mo", "cac": "$2K-5K", "ltv": "$24K-120K", "margin": "78%"},
    "determinism-gates": {"model": "CI/verification tooling + enterprise licensing", "price_range": "$500-3K/mo", "cac": "$1K-3K", "ltv": "$12K-36K", "margin": "88%"},
    "doctrine-control-plane": {"model": "Doctrine governance platform + consulting", "price_range": "$5K-20K/mo", "cac": "$3K-8K", "ltv": "$60K-240K", "margin": "72%"},
}

def _safe_ratio(ltv_str: str, cac_str: str) -> str:
    """Parse price ranges like '$96K-300K' and compute LTV/CAC ratio."""
    def parse_range(s: str) -> float:
        s = s.replace('$','').replace(',','').upper()
        m = re.search(r'(\d+)\s*-?\s*(\d+)?\s*(K|M)', s)
        if not m:
            return 0
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
        unit = 1000 if m.group(3) == 'K' else 1000000
        return ((lo + hi) / 2) * unit
    ltv = parse_range(ltv_str)
    cac = parse_range(cac_str)
    if cac == 0:
        return "N/A"
    return f"{ltv/cac:.0f}x"


def generate_business_intelligence(card: dict[str, Any]) -> dict[str, Any]:
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    tier = (card.get("strategy") or {}).get("tier") or "na"
    claim = str(card.get("claim") or "").lower()
    blob = str(card.get("summary") or "") + str(card.get("mechanism") or "")

    biz = BUSINESS_MODELS.get(sid, {"model": "Consulting + custom build", "price_range": "$5K-20K/mo", "cac": "$3K-8K", "ltv": "$60K-240K", "margin": "70%"})

    # revenue ideas specific to this card
    ideas: list[str] = []
    ideas.append(f"Productize as `{sid}` capability — sell as {biz['model']}")
    ideas.append(f"Package as a build card for fractional CAIO clients at {biz['price_range']}")
    if tier == "T0":
        ideas.append("Open-source the core, sell managed cloud + enterprise support")
        ideas.append("License to other AI consultancies as a white-label capability")
    if tier == "T1":
        ideas.append("Vertical-specific pricing: charge 2-3x for regulated industries")
        ideas.append("Bundle with adjacent T1 capabilities for sticky multi-product deals")

    # market sizing (rough)
    tam_map = {"agent-engineering": "$8B", "gtm-sales": "$12B", "healthcare-ai-ops": "$25B", "local-inference-fleet": "$3B"}
    tam = tam_map.get(sid, "$2-5B")
    # safe parse of TAM range
    tam_num = re.search(r'(\d+)', tam.replace('$',''))
    tam_val = float(tam_num.group(1)) if tam_num else 2.0
    sam = f"${tam_val * 0.01:.1f}B (RIG's serviceable segment)"

    # competitive moat
    moat: list[str] = []
    if (card.get("consensus") or {}).get("used"):
        moat.append("Consensus-backed evidence base — competitors can't replicate the research depth")
    moat.append("RIG doctrine governance — proof-chained quality that consulting firms can't match")
    moat.append("Local-first fleet — data sovereignty advantage over cloud-only competitors")
    if card.get("entities", {}).get("entity_count", 0) > 5:
        moat.append(f"Entity graph with {card.get('entities',{}).get('entity_count',0)} extracted concepts — compounding knowledge advantage")

    return {
        "revenue_model": biz["model"],
        "price_range": biz["price_range"],
        "estimated_cac": biz["cac"],
        "estimated_ltv": biz["ltv"],
        "ltv_cac_ratio": _safe_ratio(biz.get("ltv",""), biz.get("estimated_cac","")),
        "gross_margin": biz["margin"],
        "tam": tam,
        "sam": sam,
        "revenue_ideas": ideas[:6],
        "competitive_moat": moat[:4],
        "monetization_priority": "HIGH" if tier in {"T0","T1"} else "MEDIUM",
        "build_effort": "2-4 weeks" if tier == "T0" else ("1-3 weeks" if tier == "T1" else "1-2 weeks"),
    }

# ---------------------------------------------------------------------------
# V5: Engineering Blueprint
# ---------------------------------------------------------------------------

TECH_STACKS: dict[str, dict[str, list[str]]] = {
    "agent-engineering": {"backend": ["Python", "Pydantic", "FastAPI"], "agent": ["LangGraph", "CrewAI", "custom harness"], "infra": ["Ollama", "Prefect", "Docker"]},
    "local-inference-fleet": {"backend": ["Python", "asyncio"], "infra": ["Ollama", "QNAP", "launchd"], "monitoring": ["Prometheus", "custom health checks"]},
    "knowledge-memory": {"backend": ["Python", "pgvector"], "frontend": ["Obsidian", "Next.js"], "infra": ["Supabase", "ChromaDB"]},
    "gtm-sales": {"backend": ["Python", "Twenty CRM"], "automation": ["n8n", "Prefect"], "data": ["Apollo", "Clay"]},
    "scraping-intelligence": {"backend": ["Python", "Playwright"], "infra": ["QNAP", "Crawlee"], "storage": ["SQLite", "Parquet"]},
    "proof-false-done": {"backend": ["Python", "hashlib"], "infra": ["Prefect", "SQLite ledger"], "testing": ["pytest", "non-vacuity gates"]},
}

def generate_engineering_blueprint(card: dict[str, Any]) -> dict[str, Any]:
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    stack = TECH_STACKS.get(sid, {"backend": ["Python"], "infra": ["Docker"]})

    # implementation steps from card idea + mechanism
    idea = card.get("idea") or {}
    mechanism = str(card.get("mechanism") or "")
    pattern = card.get("pattern") or {}

    steps: list[dict[str, str]] = []
    steps.append({"step": 1, "action": "Define goal-card with executable done-test", "gate": "ultraplan"})
    steps.append({"step": 2, "action": f"Scaffold project: {' + '.join(stack.get('backend',[]))}", "gate": "create"})
    steps.append({"step": 3, "action": f"Build core: {idea.get('description','implement the mechanism')[:100]}", "gate": "build"})
    steps.append({"step": 4, "action": "Wire deterministic gates (score ≥ 70, sources ≥ 3, done-test passes)", "gate": "verify"})
    steps.append({"step": 5, "action": "Add ProofPacket sealing + hash chain", "gate": "prove"})
    steps.append({"step": 6, "action": "Deploy to Prefect + QNAP", "gate": "ship"})
    steps.append({"step": 7, "action": "Monitor via self-heal watchdog + fleet health", "gate": "operate"})

    # harness design
    harness = {
        "type": "TAC closed-loop",
        "builder": "rig-agent (Hermes/Claude/Codex)",
        "verifier": "deterministic scorer + GEV separate identity",
        "loop": "build → score → verify artifact → seal ProofPacket → deploy",
        "timeout_s": 3600,
        "retry_policy": "exponential backoff, max 3, fail-closed",
    }

    # architecture from entities
    arch_components: list[str] = []
    for e in (card.get("entities") or {}).get("entities") or []:
        if e.get("type") == "TOOL":
            arch_components.append(e["name"])
    arch_components = list(dict.fromkeys(arch_components))[:8]

    return {
        "tech_stack": stack,
        "architecture_components": arch_components or ["Python core", "SQLite store", "Prefect scheduler"],
        "implementation_steps": steps,
        "harness": harness,
        "testing_strategy": {
            "unit": "pytest with ≥80% coverage on core logic",
            "integration": "verify done-test against real artifact on disk",
            "non_vacuity": "plant a failure → confirm gate goes RED → restore → keep as regression",
            "load": "sustain 100 cards/day throughput on 36GB node",
        },
        "estimated_loc": "800-2000" if sid in {"agent-engineering","gtm-sales"} else "300-800",
        "complexity": "MEDIUM" if len(arch_components) <= 4 else "HIGH",
    }

# ---------------------------------------------------------------------------
# V6: Agent & Team Design
# ---------------------------------------------------------------------------

AGENT_TEMPLATES: dict[str, list[dict[str, str]]] = {
    "agent-engineering": [
        {"role": "Build Agent", "model": "qwen3-coder:30b", "skills": ["TAC v2", "closed-loop", "done-test"], "identity": "omniscout-builder"},
        {"role": "Verify Agent", "model": "deterministic scorer", "skills": ["score", "GEV", "non-vacuity"], "identity": "omniscout-verifier"},
        {"role": "Research Agent", "model": "qwen3:8b", "skills": ["Consensus MCP", "multi-source", "cluster"], "identity": "omniscout-researcher"},
    ],
    "gtm-sales": [
        {"role": "Prospect Agent", "model": "qwen3:8b", "skills": ["ICP matching", "signal scoring", "Apollo"], "identity": "darius-prospector"},
        {"role": "Outreach Agent", "model": "qwen3:8b", "skills": ["cold email", "personalization", "A/B"], "identity": "darius-writer"},
        {"role": "Pipeline Agent", "model": "deterministic", "skills": ["CRM sync", "stage tracking", "forecasting"], "identity": "darius-pipeline"},
    ],
    "healthcare-ai-ops": [
        {"role": "Clinical Agent", "model": "qwen3-coder:30b", "skills": ["RCM", "clinical ops", "compliance"], "identity": "vera-clinical"},
        {"role": "Compliance Agent", "model": "deterministic", "skills": ["HIPAA", "audit trail", "Gate-D"], "identity": "vera-compliance"},
    ],
}

def generate_agent_team_design(card: dict[str, Any]) -> dict[str, Any]:
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    tier = (card.get("strategy") or {}).get("tier") or "na"

    agents = AGENT_TEMPLATES.get(sid, [
        {"role": "Build Agent", "model": "qwen3:8b", "skills": ["TAC v2", "closed-loop"], "identity": f"rig-{sid}-builder"},
        {"role": "Verify Agent", "model": "deterministic", "skills": ["score", "GEV"], "identity": f"rig-{sid}-verifier"},
    ])

    # skill sets needed
    skills_needed: list[str] = []
    for a in agents:
        skills_needed.extend(a.get("skills") or [])
    skills_needed = list(dict.fromkeys(skills_needed))

    # PAI substrate per agent
    pai_substrates: list[dict[str, str]] = []
    for a in agents:
        pai_substrates.append({
            "agent_name": a["identity"],
            "role": a["role"],
            "telos": f"Improve RIG {sid} capability through {a['role'].lower()}",
            "quality_profile": f"agent_outcome_evaluation_v0",
            "memory_layer": "L7_VERIFIED_PROCEDURES" if "verify" in a["role"].lower() else "L6_EPISODIC_RUN_STATE",
            "proof_path": f"ProofPacket sealed after each {a['role'].lower()} cycle",
        })

    return {
        "agent_count": len(agents),
        "agents": agents,
        "skills_required": skills_needed,
        "pai_substrates": pai_substrates,
        "team_composition": {
            "humans": 1 if tier in {"T0","T1"} else 0,
            "agents": len(agents),
            "oversight": "Gate-D human approval for all outward actions",
            "cadence": "nightly batch + continuous collection",
        },
        "department_routing": _route_to_department(sid),
        "jake_context_update": f"Jake should know: {sid} capability uses {len(agents)} agents ({', '.join(a['role'] for a in agents)}). Department: {_route_to_department(sid)}.",
    }

def _route_to_department(sid: str) -> str:
    routing = {
        "proof-false-done": "intelligence", "agent-engineering": "intelligence",
        "determinism-gates": "intelligence", "local-inference-fleet": "data",
        "knowledge-memory": "intelligence", "automation-runtime": "operations",
        "scraping-intelligence": "intelligence", "doctrine-control-plane": "intelligence",
        "gtm-sales": "gtm", "pricing-finance": "gtm", "healthcare-ai-ops": "gtm",
        "marketing-content-linkedin": "content", "forecasting-calibration": "intelligence",
        "strategy-decision-routing": "strategy", "founder-performance": "operations",
    }
    return routing.get(sid, "intelligence")

# ---------------------------------------------------------------------------
# V7: Doctrine & Governance
# ---------------------------------------------------------------------------

def generate_doctrine_governance(card: dict[str, Any]) -> dict[str, Any]:
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    tier = (card.get("strategy") or {}).get("tier") or "na"
    score = card.get("score") or {}
    promotion = card.get("promotion_state") or {}

    gates: list[dict[str, Any]] = [
        {"name": "Gate-D", "requirement": "Human approval for any outward action (publish, send, deploy)", "applies": tier in {"T0","T1"}},
        {"name": "GEV Separation", "requirement": "Builder ≠ Verifier — different team + service identity", "applies": True},
        {"name": "ProofPacket", "requirement": "Hash-chained proof sealed before claiming done", "applies": True},
        {"name": "Non-vacuity", "requirement": "Plant failure → confirm RED → restore → keep regression", "applies": True},
        {"name": "Temporal validity", "requirement": f"Re-verify by {(card.get('temporal_validity') or {}).get('reverify_by','?')}", "applies": True},
        {"name": "Consensus backing", "requirement": "≥1 Consensus MCP paper for T0 promote", "applies": tier == "T0"},
    ]

    policy_rules: list[str] = []
    policy_rules.append(f"This capability operates under RIG {sid} doctrine")
    policy_rules.append("All artifacts must pass deterministic scorer (≥70/100)")
    policy_rules.append("No agent self-certifies done — independent verifier required")
    if tier == "T0":
        policy_rules.append("T0 capabilities require Consensus MCP evidence for promotion")
        policy_rules.append("T0 failures trigger self-heal watchdog within 5 minutes")

    return {
        "gates": gates,
        "policy_rules": policy_rules,
        "safety_authority": "L4 SAFETY_AUTHORITY" if tier == "T0" else "L7 VERIFIED_PROCEDURES",
        "proof_requirements": {
            "artifact_hash": True,
            "done_test_exit_code": True,
            "consensus_citation": tier == "T0",
            "gev_separation_proof": True,
        },
        "doctrine_file_target": f"~/.rig/agent-doctrine/RIG_{sid.upper()}_DOCTRINE.md",
        "promotion_eligible": promotion.get("state") == "READY_FOR_PROMOTION",
    }

# ---------------------------------------------------------------------------
# V8: Go-to-Market
# ---------------------------------------------------------------------------

GTM_MOTIONS: dict[str, dict[str, Any]] = {
    "agent-engineering": {"motion": "Product-led + content", "icp": "AI-first startups ($1-10M ARR)", "channels": ["YouTube", "LinkedIn", "GitHub"], "cycle": "30-60 days"},
    "gtm-sales": {"motion": "Outbound + ABM", "icp": "B2B SaaS ($5-50M ARR)", "channels": ["Cold email", "LinkedIn", "Calls"], "cycle": "14-45 days"},
    "healthcare-ai-ops": {"motion": "Consultative + partner", "icp": "DSOs, multi-site clinics ($10M+ rev)", "channels": ["Conferences", "Referrals", "Partnerships"], "cycle": "60-120 days"},
    "local-inference-fleet": {"motion": "Developer marketing + OSS", "icp": "AI teams needing local inference", "channels": ["GitHub", "Hugging Face", "Discord"], "cycle": "30-90 days"},
}

def generate_gtm_strategy(card: dict[str, Any]) -> dict[str, Any]:
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    tier = (card.get("strategy") or {}).get("tier") or "na"
    gtm = GTM_MOTIONS.get(sid, {"motion": "Consulting + content", "icp": "Mid-market operators", "channels": ["LinkedIn", "Content"], "cycle": "30-60 days"})

    # content plan from card
    content: list[dict[str, str]] = []
    title = str(card.get("title") or "")[:60]
    content.append({"type": "LinkedIn post", "topic": f"How RIG uses {title[:40]}", "cadence": "this week"})
    content.append({"type": "YouTube video", "topic": f"Building {title[:40]} — live agent build", "cadence": "2 weeks"})
    content.append({"type": "Substack essay", "topic": f"The {sid} capability: mechanism + proof", "cadence": "1 month"})
    content.append({"type": "GitHub repo", "topic": f"Open-source the {sid} harness", "cadence": "1 month"})

    # outreach targets
    outreach: list[str] = []
    outreach.append(f"Search LinkedIn for: {gtm['icp']}")
    outreach.append(f"Post in: r/MachineLearning, HN, AI subreddits about {sid}")
    outreach.append(f"Reference this card's Consensus papers in outreach for credibility")
    if tier == "T1":
        outreach.append(f"Target 50 prospects/week in {gtm['icp']} via Apollo + Clay")

    return {
        "sales_motion": gtm["motion"],
        "icp": gtm["icp"],
        "channels": gtm["channels"],
        "sales_cycle": gtm["cycle"],
        "content_plan": content,
        "outreach_targets": outreach[:5],
        "positioning": f"RIG is the only AI consultancy with Consensus-backed, proof-chained {sid} capability",
        "differentiation": [
            "Proof-chained quality (competitors can't match)",
            "Consensus academic backing (not just opinions)",
            "Local-first fleet (data sovereignty)",
            "Nightly automated capability compounding (100 cards/day)",
        ],
        "pricing_anchor": f"Anchor at {(card.get('business_intelligence') or {}).get('price_range', '$5-20K/mo')}",
    }

# ---------------------------------------------------------------------------
# V9: Open Source Integration
# ---------------------------------------------------------------------------

OSS_MAP: dict[str, list[dict[str, str]]] = {
    "agent-engineering": [
        {"project": "LangGraph", "use": "Agent orchestration", "fork_or_use": "use", "url": "github.com/langchain-ai/langgraph"},
        {"project": "CrewAI", "use": "Multi-agent crews", "fork_or_use": "use", "url": "github.com/crewAIInc/crewAI"},
        {"project": "AutoGen", "use": "Multi-agent conversation", "fork_or_use": "evaluate", "url": "github.com/microsoft/autogen"},
    ],
    "local-inference-fleet": [
        {"project": "Ollama", "use": "Local model serving", "fork_or_use": "use", "url": "github.com/ollama/ollama"},
        {"project": "vLLM", "use": "High-throughput inference", "fork_or_use": "evaluate", "url": "github.com/vllm-project/vllm"},
        {"project": "MLX", "use": "Apple Silicon optimization", "fork_or_use": "use", "url": "github.com/ml-explore/mlx"},
    ],
    "knowledge-memory": [
        {"project": "ChromaDB", "use": "Vector storage", "fork_or_use": "use", "url": "github.com/chroma-core/chroma"},
        {"project": "LlamaIndex", "use": "RAG pipeline", "fork_or_use": "use", "url": "github.com/run-llama/llama_index"},
    ],
    "automation-runtime": [
        {"project": "Prefect", "use": "Workflow orchestration", "fork_or_use": "use", "url": "github.com/PrefectHQ/prefect"},
        {"project": "Temporal", "use": "Durable execution", "fork_or_use": "evaluate", "url": "github.com/temporalio/temporal"},
    ],
    "scraping-intelligence": [
        {"project": "Crawlee", "use": "Web scraping", "fork_or_use": "use", "url": "github.com/apify/crawlee"},
        {"project": "Playwright", "use": "Browser automation", "fork_or_use": "use", "url": "github.com/microsoft/playwright"},
    ],
    "proof-false-done": [
        {"project": "pytest", "use": "Test framework + non-vacuity", "fork_or_use": "use", "url": "github.com/pytest-dev/pytest"},
    ],
}

def generate_oss_integration(card: dict[str, Any]) -> dict[str, Any]:
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    projects = OSS_MAP.get(sid, [])

    # also detect OSS from entities
    for e in (card.get("entities") or {}).get("entities") or []:
        if e.get("type") == "TOOL":
            name = e["name"]
            if not any(p["project"].lower() == name.lower() for p in projects):
                projects.append({"project": name, "use": "detected from card entities", "fork_or_use": "evaluate", "url": f"search: {name}"})

    return {
        "relevant_projects": projects[:8],
        "integration_strategy": "Wrap as RIG capability layer — OSS handles commodity, RIG adds governance + proof",
        "contribution_opportunities": [
            f"Contribute {sid} patterns back to upstream projects",
            "Publish RIG doctrine modules as OSS (attract talent + leads)",
        ],
        "build_vs_buy": {p["project"]: p["fork_or_use"] for p in projects[:5]},
        "lock_in_risk": "LOW — RIG adds proprietary governance layer on top of OSS",
    }

# ---------------------------------------------------------------------------
# V10: World Model Context (Jake briefing)
# ---------------------------------------------------------------------------

def generate_world_model_context(card: dict[str, Any], all_cards: list[dict[str, Any]]) -> dict[str, Any]:
    sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
    tier = (card.get("strategy") or {}).get("tier") or "na"
    title = str(card.get("title") or "")[:60]

    # count cards in same strategy
    same_strategy = sum(1 for c in all_cards if (c.get("strategy") or {}).get("strategy_id") == sid)
    total_cards = len(all_cards)

    # RIG 1000x vision fit
    vision_fit: list[str] = []
    if tier == "T0":
        vision_fit.append("CORE — this is what makes RIG's product true")
        vision_fit.append("Without this, RIG is just another AI consultancy")
    elif tier == "T1":
        vision_fit.append("REVENUE — this directly funds the 1000x build-out")
        vision_fit.append("Monetize first, then productize")
    else:
        vision_fit.append("MOAT — depth that compounds over time")
        vision_fit.append("Horizon scanning — promote if signal repeats")

    # dependencies
    deps: list[str] = []
    if sid != "proof-false-done":
        deps.append("proof-false-done (honest completion)")
    if sid != "local-inference-fleet":
        deps.append("local-inference-fleet (compute substrate)")
    if sid != "automation-runtime":
        deps.append("automation-runtime (scheduling)")

    # Jake context — what Jake should know
    jake_briefing = (
        f"JAKE CONTEXT: Card `{card.get('card_id')}` advances `{sid}` (tier {tier}). "
        f"Title: {title}. "
        f"Strategy has {same_strategy} cards in corpus ({total_cards} total). "
        f"Promotion state: {(card.get('promotion_state') or {}).get('state', '?')}. "
        f"Revenue model: {(card.get('business_intelligence') or {}).get('revenue_model', '?')} at {(card.get('business_intelligence') or {}).get('price_range', '?')}. "
        f"Build effort: {(card.get('engineering_blueprint') or {}).get('build_effort' if False else 'estimated_loc', '?')} LOC. "
        f"Agents needed: {(card.get('agent_team') or {}).get('agent_count', '?')}. "
        f"Department: {(card.get('agent_team') or {}).get('department_routing', '?')}. "
        f"Next action: {(card.get('promotion_state') or {}).get('next_action', '?')[:80]}. "
    )

    return {
        "rig_1000x_fit": vision_fit,
        "capability_cluster_size": same_strategy,
        "total_corpus": total_cards,
        "dependencies": deps[:4],
        "temporal_roadmap": {
            "now": f"Enrich + score this card (done)",
            "week_1": f"Build the harness for {sid}" if tier in {"T0","T1"} else "Monitor for signal repetition",
            "month_1": f"Deploy as RIG capability + first paying client" if tier == "T1" else f"Productize as internal tool",
            "quarter_1": f"Open-source core + enterprise licensing" if tier == "T0" else f"Bundle into vertical pack",
        },
        "risk_model": {
            "evidence_risk": (card.get("temporal_validity") or {}).get("freshness", "?"),
            "competition_risk": "MEDIUM" if tier == "T1" else "LOW",
            "execution_risk": "LOW" if (card.get("score") or {}).get("total",0) >= 80 else "MEDIUM",
        },
        "jake_briefing": jake_briefing,
        "jake_action": f"Add to Jake daily brief: {sid} capability ({same_strategy} cards). Revenue: {(card.get('business_intelligence') or {}).get('revenue_model', '?')}. Next: {(card.get('promotion_state') or {}).get('next_action', '?')[:60]}",
        "obsidian_target": f"Documents/JakeStudio/Capabilities/{sid}/{card.get('card_id')}.md",
        "memory_os_event": {
            "event_type": "capability_card_enriched",
            "layer": (card.get("memory_layer") or {}).get("layer", 8),
            "scope": f"strategy:{sid}",
            "sensitivity": "internal",
            "action": "promote_to_obsidian",
        },
    }

# ---------------------------------------------------------------------------
# Enrich single card to V10
# ---------------------------------------------------------------------------

def enrich_card_v10(card_path: Path, all_cards_data: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    card = json.loads(card_path.read_text(encoding="utf-8"))
    if card.get("schema") == SCHEMA_V10:
        return {"ok": True, "card_id": card.get("card_id"), "status": "already_v10"}

    if all_cards_data is None:
        all_cards_data = [json.loads(p.read_text(encoding="utf-8")) for p in L2_CARDS.glob("l2-*.json") if p != card_path]

    # V4-V10 layers
    card["business_intelligence"] = generate_business_intelligence(card)
    card["engineering_blueprint"] = generate_engineering_blueprint(card)
    card["agent_team"] = generate_agent_team_design(card)
    card["doctrine_governance"] = generate_doctrine_governance(card)
    card["gtm_strategy"] = generate_gtm_strategy(card)
    card["oss_integration"] = generate_oss_integration(card)
    card["world_model"] = generate_world_model_context(card, all_cards_data)

    card["schema"] = SCHEMA_V10
    card["enriched_v10_at"] = utc_now()

    # re-score
    new_score = score_build_card(card)
    card["score"] = new_score
    card["artifact_sha256"] = sha256_text(stable_json({k: v for k, v in card.items() if k != "artifact_sha256"}))

    atomic_json(card_path, card)
    md = _card_v10_to_markdown(card)
    atomic_text(card_path.with_suffix(".md"), md)

    return {
        "ok": True, "card_id": card.get("card_id"), "status": "enriched_to_v10",
        "fields": len(card.keys()), "score": new_score.get("total"), "rank": new_score.get("rank"),
        "revenue_model": card["business_intelligence"]["revenue_model"],
        "agents": card["agent_team"]["agent_count"],
        "jake_ready": True,
    }


def _card_v10_to_markdown(card: dict[str, Any]) -> str:
    """Full V10 markdown — complete capability blueprint."""
    s = lambda *xs: "\n".join(str(x) for x in xs)

    biz = card.get("business_intelligence") or {}
    eng = card.get("engineering_blueprint") or {}
    team = card.get("agent_team") or {}
    gov = card.get("doctrine_governance") or {}
    gtm = card.get("gtm_strategy") or {}
    oss = card.get("oss_integration") or {}
    wm = card.get("world_model") or {}

    return s(
        f"# {card.get('title')}",
        "",
        f"**V10 Capability Blueprint** | `{card.get('card_id')}` | {(card.get('score') or {}).get('rank')} ({(card.get('score') or {}).get('total')}/100)",
        f"Strategy: `{(card.get('strategy') or {}).get('strategy_id')}` | Tier: `{(card.get('strategy') or {}).get('tier')}`",
        f"Schema: `{SCHEMA_V10}` | Fields: {len(card.keys())}",
        "",
        "---",
        "",
        "## Claim", str(card.get("claim") or ""),
        "## Summary", str(card.get("summary") or ""),
        "## Mechanism", str(card.get("mechanism") or ""),
        "",
        "---",
        "",
        "## V4: Business Intelligence",
        "",
        f"- **Revenue model:** {biz.get('revenue_model','?')}",
        f"- **Price range:** {biz.get('price_range','?')}",
        f"- **LTV/CAC:** {biz.get('ltv_cac_ratio','?')} (CAC: {biz.get('estimated_cac','?')}, LTV: {biz.get('estimated_ltv','?')})",
        f"- **Gross margin:** {biz.get('gross_margin','?')}",
        f"- **TAM:** {biz.get('tam','?')} | **SAM:** {biz.get('sam','?')}",
        f"- **Build effort:** {biz.get('build_effort','?')}",
        f"- **Monetization priority:** {biz.get('monetization_priority','?')}",
        "",
        "### Revenue Ideas",
        *[f"- {i}" for i in biz.get("revenue_ideas",[])],
        "",
        "### Competitive Moat",
        *[f"- {m}" for m in biz.get("competitive_moat",[])],
        "",
        "---",
        "",
        "## V5: Engineering Blueprint",
        "",
        f"- **Tech stack:** {json.dumps(eng.get('tech_stack',{}))}",
        f"- **Architecture:** {', '.join(eng.get('architecture_components',[]))}",
        f"- **Complexity:** {eng.get('complexity','?')} | **Est. LOC:** {eng.get('estimated_loc','?')}",
        "",
        "### Implementation Steps (TAC v2)",
        *[f"{st['step']}. {st['action']} → Gate: `{st['gate']}`" for st in eng.get("implementation_steps",[])],
        "",
        "### Harness Design",
        f"```json\n{json.dumps(eng.get('harness',{}), indent=2)}\n```",
        "",
        "### Testing Strategy",
        *[f"- {k}: {v}" for k,v in (eng.get("testing_strategy") or {}).items()],
        "",
        "---",
        "",
        "## V6: Agent & Team Design",
        "",
        f"**{team.get('agent_count',0)} agents** | Department: `{team.get('department_routing','?')}`",
        "",
        "### Agents",
        *[f"- **{a['role']}** ({a['model']}): {', '.join(a.get('skills',[]))} → `{a['identity']}`" for a in team.get("agents",[])],
        "",
        "### Skills Required",
        *[f"- `{sk}`" for sk in team.get("skills_required",[])],
        "",
        "### Team Composition",
        *[f"- {k}: {v}" for k,v in (team.get("team_composition") or {}).items()],
        "",
        f"**Jake context:** {team.get('jake_context_update','')}",
        "",
        "---",
        "",
        "## V7: Doctrine & Governance",
        "",
        "### Gates",
        *[f"- {'✅' if g['applies'] else '⬜'} **{g['name']}**: {g['requirement']}" for g in gov.get("gates",[])],
        "",
        "### Policy Rules",
        *[f"- {r}" for r in gov.get("policy_rules",[])],
        "",
        f"**Safety authority:** {gov.get('safety_authority','?')}",
        f"**Doctrine target:** `{gov.get('doctrine_file_target','?')}`",
        f"**Promotion eligible:** {gov.get('promotion_eligible',False)}",
        "",
        "---",
        "",
        "## V8: Go-to-Market",
        "",
        f"- **Motion:** {gtm.get('sales_motion','?')}",
        f"- **ICP:** {gtm.get('icp','?')}",
        f"- **Cycle:** {gtm.get('sales_cycle','?')}",
        f"- **Channels:** {', '.join(gtm.get('channels',[]))}",
        f"- **Positioning:** {gtm.get('positioning','?')}",
        "",
        "### Content Plan",
        *[f"- [{c['type']}] {c['topic']} ({c['cadence']})" for c in gtm.get("content_plan",[])],
        "",
        "### Outreach Targets",
        *[f"- {o}" for o in gtm.get("outreach_targets",[])],
        "",
        "---",
        "",
        "## V9: Open Source Integration",
        "",
        "### Relevant Projects",
        *[f"- **{p['project']}** ({p['fork_or_use']}): {p['use']} — `{p.get('url','')}`" for p in oss.get("relevant_projects",[])],
        "",
        f"**Strategy:** {oss.get('integration_strategy','?')}",
        "",
        "---",
        "",
        "## V10: World Model Context",
        "",
        "### RIG 1000x Fit",
        *[f"- {v}" for v in wm.get("rig_1000x_fit",[])],
        "",
        f"**Cluster:** {wm.get('capability_cluster_size',0)} cards in strategy | **Corpus:** {wm.get('total_corpus',0)} total",
        "",
        "### Dependencies",
        *[f"- {d}" for d in wm.get("dependencies",[])],
        "",
        "### Temporal Roadmap",
        *[f"- **{k.replace('_',' ').title()}:** {v}" for k,v in (wm.get("temporal_roadmap") or {}).items()],
        "",
        "### Risk Model",
        *[f"- {k.replace('_',' ').title()}: {v}" for k,v in (wm.get("risk_model") or {}).items()],
        "",
        "### Jake Briefing",
        f"> {wm.get('jake_briefing','')}",
        "",
        f"**Jake action:** {wm.get('jake_action','')}",
        f"**Obsidian target:** `{wm.get('obsidian_target','')}`",
        "",
        "---",
        "",
        "## Entity Graph",
        f"_{(card.get('entities') or {}).get('entity_count',0)} entities, {(card.get('entities') or {}).get('relationship_count',0)} relationships_",
        "",
        "## Semantic Links",
        f"_{(card.get('semantic_links') or {}).get('link_count',0)} links, {(card.get('semantic_links') or {}).get('contradiction_count',0)} contradictions_",
        "",
        "## Memory Layer",
        f"**L{(card.get('memory_layer') or {}).get('layer',8)}/8** — {(card.get('memory_layer') or {}).get('layer_name','EVIDENCE')}",
        "",
        "## Temporal Validity",
        f"**{(card.get('temporal_validity') or {}).get('freshness','?')}** — re-verify by {(card.get('temporal_validity') or {}).get('reverify_by','?')}",
        "",
        "## Score",
        f"```json\n{json.dumps((card.get('score') or {}).get('breakdown',{}), indent=2)}\n```",
        "",
    ) + "\n"


def enrich_all_v10() -> dict[str, Any]:
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

    results = []
    errors = []
    for path in card_paths:
        try:
            results.append(enrich_card_v10(path, all_cards_data))
        except Exception as exc:
            errors.append({"card_id": path.stem, "error": str(exc)[:300]})

    # export to Obsidian capabilities vault
    obsidian_count = _export_to_obsidian()

    summary = {
        "schema": "rig.omniscout.l2-enrichment-v10.v1",
        "ok": len(errors) == 0,
        "started_at": started,
        "finished_at": utc_now(),
        "elapsed_s": round(time.time() - t0, 2),
        "total_cards": len(card_paths),
        "enriched_v10": sum(1 for r in results if r.get("status") == "enriched_to_v10"),
        "already_v10": sum(1 for r in results if r.get("status") == "already_v10"),
        "obsidian_exported": obsidian_count,
        "errors": errors[:10],
        "at": utc_now(),
    }
    (L2_ROOT / "latest-enrichment-v10.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _export_to_obsidian() -> int:
    """Export all V10 cards to Jake Obsidian vault under Capabilities/."""
    cap_root = JAKE / "Capabilities"
    count = 0
    for jp in sorted(L2_CARDS.glob("l2-*.json")):
        try:
            card = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = (card.get("strategy") or {}).get("strategy_id") or "unmapped"
        tier = (card.get("strategy") or {}).get("tier") or "na"
        dest_dir = cap_root / str(tier) / str(sid)
        dest_dir.mkdir(parents=True, exist_ok=True)
        md_src = jp.with_suffix(".md")
        if md_src.exists():
            dest = dest_dir / f"{jp.stem}.md"
            dest.write_text(md_src.read_text(encoding="utf-8"), encoding="utf-8")
            count += 1
    # write index
    idx = {"updated_at": utc_now(), "count": count, "vault": str(cap_root)}
    (cap_root / "index.json").write_text(json.dumps(idx, indent=2) + "\n", encoding="utf-8")
    return count


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="V10 Capability Engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("all")
    p_one = sub.add_parser("one"); p_one.add_argument("path")
    sub.add_parser("status")
    sub.add_parser("obsidian")
    args = parser.parse_args(argv)

    if args.cmd == "all":
        out = enrich_all_v10()
    elif args.cmd == "one":
        out = enrich_card_v10(Path(args.path))
    elif args.cmd == "status":
        v10 = sum(1 for p in L2_CARDS.glob("l2-*.json") if json.loads(p.read_text()).get("schema") == SCHEMA_V10)
        out = {"v10": v10, "total": len(list(L2_CARDS.glob("l2-*.json")))}
    elif args.cmd == "obsidian":
        out = {"exported": _export_to_obsidian()}
    else:
        parser.error("unknown"); return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
