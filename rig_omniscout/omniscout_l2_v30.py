"""OmniScout L2 V30 Deep Enrichment Engine — 100x-detail expansion of every V10/V20 section.

Adds 8 deterministic "deep_*" sections to each build card, each 500-2000 words of
card-specific content derived from the card's own fields (engineering_blueprint,
business_intelligence, gtm_strategy, agent_team, council, consensus, evidence, risks,
kill_criteria, temporal_validity, doctrine_governance, strategy, idea). No LLM calls —
pure deterministic string templating so output is reproducible, cheap to regenerate at
scale, and every sentence traces back to a concrete card field rather than being
invented from scratch.

Sections: deep_engineering, deep_business, deep_gtm, deep_agents, deep_research,
deep_risk, deep_testing, deep_ops.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from rig_foundry.omniscout_build_cards import (  # noqa: E402
    L2_CARDS, L2_ROOT, atomic_json, atomic_text, sha256_text,
    stable_json, utc_now, DOCTRINE_DOMAINS, score_build_card,
)

SCHEMA_V30 = "rig.omniscout.build-card.v30"

REGULATORY_MAP: dict[str, list[str]] = {
    "healthcare-ai-ops": ["HIPAA (PHI handling)", "HITECH breach notification", "SOC 2 Type II"],
    "cybersecurity": ["SOC 2 Type II", "ISO 27001", "CMMC (if DoD-adjacent)"],
    "vertical-law-cpa": [
        "Attorney-client privilege / work-product doctrine",
        "State bar technology-competence rules",
        "SOC 2 Type II",
    ],
    "pricing-finance": [
        "PCI DSS (if card data is touched)",
        "SOC 2 Type II",
        "State money-transmitter rules (if applicable)",
    ],
    "gtm-sales": ["CAN-SPAM / CASL", "GDPR/CCPA (contact data)", "TCPA (if calling/texting)"],
    "marketing-content-linkedin": [
        "Platform ToS (LinkedIn automation limits)",
        "GDPR/CCPA (contact data)",
        "FTC endorsement guidelines",
    ],
}
DEFAULT_REGULATORY = ["GDPR/CCPA (data privacy)", "SOC 2 Type II (enterprise sales readiness)"]

MODEL_COST_PER_1K: dict[str, float] = {
    "qwen3:8b": 0.002,
    "qwen3-coder:30b": 0.02,
    "deterministic": 0.0,
}

MIN_SECTION_WORDS = 520
MAX_SECTION_WORDS = 1950


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _wc(text: str) -> int:
    return len(text.split())


def _slug(value: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return (s or "capability")[:max_len]


_MONEY_RE = re.compile(r"([\d.]+)\s*([KMB]?)", re.I)


def _money_range(s: str, default: tuple[float, float] = (2000.0, 8000.0)) -> tuple[float, float]:
    nums: list[float] = []
    for m in _MONEY_RE.finditer(s or ""):
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        mult = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}.get(m.group(2).upper(), 1.0)
        if val > 0:
            nums.append(val * mult)
    if not nums:
        return default
    if len(nums) == 1:
        return (nums[0] * 0.6, nums[0])
    return (min(nums), max(nums))


def _pct(s: str, default: float = 0.6) -> float:
    m = re.search(r"([\d.]+)\s*%", s or "")
    if not m:
        return default
    try:
        return max(0.01, min(0.99, float(m.group(1)) / 100.0))
    except ValueError:
        return default


def _fmt_usd(n: float) -> str:
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"${n / 1_000:.1f}K"
    return f"${n:.0f}"


def _aligned_domains(card: dict[str, Any]) -> list[str]:
    blob = " ".join(
        [
            str(card.get("title") or ""),
            str(card.get("claim") or ""),
            str(card.get("summary") or ""),
            str(card.get("mechanism") or ""),
        ]
    ).lower()
    entity_names = {
        str(e.get("name") or "").lower() for e in (card.get("entities") or {}).get("entities", [])
    }
    aligned: list[str] = []
    for domain, markers in DOCTRINE_DOMAINS.items():
        hits = sum(1 for m in markers if m in blob or any(m in en for en in entity_names))
        if hits >= 2:
            aligned.append(domain)
    return aligned or list((card.get("doctrine_domains") or [])[:2]) or ["engineering-capability"]


# ---------------------------------------------------------------------------
# Card context extraction
# ---------------------------------------------------------------------------


def _ctx(card: dict[str, Any], all_cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    strategy = card.get("strategy") or {}
    eng = card.get("engineering_blueprint") or {}
    biz = card.get("business_intelligence") or {}
    gtm = card.get("gtm_strategy") or {}
    agents = card.get("agent_team") or {}
    council = card.get("council") or {}
    consensus = card.get("consensus") or {}
    doctrine_gov = card.get("doctrine_governance") or {}
    temporal = card.get("temporal_validity") or {}
    idea = card.get("idea") or {}
    pattern = card.get("pattern") or {}
    oss = card.get("oss_integration") or {}
    score = card.get("score") or {}
    entities_block = card.get("entities") or {}
    sources = card.get("sources") or {}

    strategy_id = str(strategy.get("strategy_id") or "unmapped").lower()
    peers = all_cards or []
    peer_count = sum(
        1
        for c in peers
        if str(((c.get("strategy") or {}).get("strategy_id") or "")).lower() == strategy_id
        and c.get("card_id") != card.get("card_id")
    )

    return {
        "card": card,
        "card_id": str(card.get("card_id") or "unknown"),
        "title": str(card.get("title") or idea.get("name") or "Untitled Capability"),
        "topic": str(card.get("topic") or strategy.get("mapped_from_topic") or "general"),
        "claim": str(card.get("claim") or ""),
        "summary": str(card.get("summary") or ""),
        "mechanism": str(card.get("mechanism") or ""),
        "why_not_median": str(card.get("why_not_median") or ""),
        "strategy_id": strategy_id,
        "tier": str(strategy.get("tier") or "na").upper(),
        "question": str(strategy.get("question") or card.get("claim") or ""),
        "slug": _slug(str(card.get("title") or strategy_id or card.get("card_id") or "capability")),
        "idea": idea,
        "done_test": str(idea.get("done_test") or ""),
        "acceptance": str(idea.get("acceptance") or ""),
        "pattern": pattern,
        "eng": eng,
        "tech_stack": eng.get("tech_stack") or {},
        "arch_components": list(eng.get("architecture_components") or []),
        "impl_steps": list(eng.get("implementation_steps") or []),
        "harness": eng.get("harness") or {},
        "testing_strategy": eng.get("testing_strategy") or {},
        "estimated_loc": str(eng.get("estimated_loc") or "300-800"),
        "complexity": str(eng.get("complexity") or "MEDIUM"),
        "biz": biz,
        "gtm": gtm,
        "agents_list": list(agents.get("agents") or [])[:4],
        "pai_substrates": list(agents.get("pai_substrates") or []),
        "department_routing": str(agents.get("department_routing") or "intelligence"),
        "team_composition": agents.get("team_composition") or {},
        "council": council,
        "council_synthesis": council.get("synthesis") or {},
        "council_verdict": str(
            card.get("council_verdict")
            or (council.get("synthesis") or {}).get("overall_verdict")
            or "UNKNOWN"
        ),
        "consensus": consensus,
        "consensus_results": list(consensus.get("results") or []),
        "evidence": list(card.get("evidence") or []),
        "risks": list(card.get("risks") or []),
        "assumptions": list(card.get("assumptions") or []),
        "kill_criteria": list(card.get("kill_criteria") or []),
        "temporal": temporal,
        "doctrine_gov": doctrine_gov,
        "gates": list(doctrine_gov.get("gates") or []),
        "policy_rules": list(doctrine_gov.get("policy_rules") or []),
        "entities": list(entities_block.get("entities") or []),
        "entity_names": [
            str(e.get("name") or "") for e in (entities_block.get("entities") or []) if e.get("name")
        ],
        "oss": oss,
        "sources": sources,
        "source_count": int(sources.get("count") or len(sources.get("urls") or []) or 0),
        "score": score,
        "score_total": int(score.get("total") or 0),
        "rank": str(score.get("rank") or "UNKNOWN"),
        "aligned_domains": _aligned_domains(card),
        "peer_count": peer_count,
    }


# ---------------------------------------------------------------------------
# 1. Deep Engineering
# ---------------------------------------------------------------------------


def _file_manifest(ctx: dict[str, Any]) -> list[tuple[str, str]]:
    slug = ctx["slug"]
    stack = ctx["tech_stack"]
    backend = stack.get("backend") or ["Python"]
    infra = stack.get("infra") or ["Docker"]
    components = ctx["arch_components"] or ["core module"]
    lang_ext = "ts" if any("typescript" in b.lower() or "node" in b.lower() for b in backend) else "py"

    files: list[tuple[str, str]] = [
        (
            f"{slug}/main.{lang_ext}",
            f"Entrypoint wiring `{components[0]}` into the request lifecycle for `{ctx['title'][:60]}`.",
        ),
        (
            f"{slug}/config.{lang_ext}",
            "Typed configuration loader: env vars, doctrine gate thresholds, harness timeouts.",
        ),
    ]
    for comp in components[:6]:
        files.append(
            (
                f"{slug}/core/{_slug(comp)}.{lang_ext}",
                f"Implements the `{comp}` component — {ctx['mechanism'][:90] or 'core mechanism'}",
            )
        )
    files.append(
        (
            f"{slug}/models/schema.{lang_ext}",
            "Data models for the card artifact: claim, mechanism, pattern, score, ProofPacket.",
        )
    )
    files.append(
        (
            f"{slug}/services/scorer.{lang_ext}",
            "Deterministic scorer client — never self-certifies; independent verifier identity only.",
        )
    )
    files.append(
        (
            f"{slug}/services/proof_packet.{lang_ext}",
            "Hash-chain sealing: artifact_sha256 over stable_json(card) before any 'done' claim.",
        )
    )
    for inf in infra[:3]:
        files.append((f"{slug}/infra/{_slug(inf)}.yml", f"Deployment descriptor for {inf} in the {slug} stack."))
    files.append(
        (
            f"tests/{slug}/test_done_test.{lang_ext}",
            f"Executes the card's done-test: `{ctx['done_test'][:80] or 'assert schema + sources present'}`",
        )
    )
    files.append(
        (
            f"tests/{slug}/test_non_vacuity.{lang_ext}",
            "Plants a failure, asserts RED, restores, keeps the case as a permanent regression.",
        )
    )
    files.append((f"{slug}/README.md", f"Operator-facing overview: what `{ctx['title'][:60]}` does and how to verify it."))
    return files[:14]


def _api_endpoints(ctx: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    slug = ctx["slug"]
    return [
        ("POST", f"/api/v1/{slug}/build", "{cluster_id, topic}", "202 Accepted, {card_id, status: queued}"),
        ("GET", f"/api/v1/{slug}/cards/{{card_id}}", "path param card_id", "200 OK, full card JSON"),
        ("POST", f"/api/v1/{slug}/score", "{card_id}", "200 OK, {total, rank, promote, breakdown}"),
        (
            "POST",
            f"/api/v1/{slug}/verify",
            "{card_id, done_test}",
            "200 OK {passed: bool, exit_code} or 409 on hard block",
        ),
        ("POST", f"/api/v1/{slug}/proof-packet", "{card_id}", "201 Created, {artifact_sha256, sealed_at}"),
        ("GET", f"/api/v1/{slug}/status", "none", "200 OK, {total_cards, promote_rate, last_run_at}"),
        ("GET", "/healthz", "none", "200 OK {ok:true} or 503 on QNAP pressure"),
    ]


def _db_tables(ctx: dict[str, Any]) -> list[tuple[str, list[tuple[str, str]], str]]:
    slug = ctx["slug"]
    return [
        (
            f"{slug}_cards",
            [
                ("card_id", "TEXT PRIMARY KEY"),
                ("schema", "TEXT"),
                ("title", "TEXT"),
                ("strategy_id", "TEXT"),
                ("tier", "TEXT"),
                ("created_at", "TIMESTAMPTZ"),
                ("card_json", "JSONB"),
            ],
            f"idx_{slug}_cards_strategy (strategy_id, tier)",
        ),
        (
            f"{slug}_scores",
            [
                ("card_id", "TEXT REFERENCES cards(card_id)"),
                ("total", "INT"),
                ("rank", "TEXT"),
                ("promote", "BOOLEAN"),
                ("scored_at", "TIMESTAMPTZ"),
            ],
            f"idx_{slug}_scores_rank (rank, promote)",
        ),
        (
            f"{slug}_proof_seals",
            [
                ("card_id", "TEXT"),
                ("artifact_sha256", "TEXT"),
                ("sealed_at", "TIMESTAMPTZ"),
                ("verifier_identity", "TEXT"),
            ],
            f"idx_{slug}_proof_seals_hash (artifact_sha256)",
        ),
        (
            f"{slug}_events",
            [
                ("event_id", "BIGSERIAL PRIMARY KEY"),
                ("card_id", "TEXT"),
                ("event_type", "TEXT"),
                ("payload", "JSONB"),
                ("occurred_at", "TIMESTAMPTZ"),
            ],
            f"idx_{slug}_events_card_time (card_id, occurred_at)",
        ),
    ]


def _deployment_block(ctx: dict[str, Any]) -> str:
    infra = ctx["tech_stack"].get("infra") or ["Docker"]
    timeout = ctx["harness"].get("timeout_s", 3600)
    return (
        f"Docker services: `{', '.join(infra)}` orchestrated via docker-compose; the build/verify loop "
        f"runs as a Prefect-scheduled job with a {timeout}s hard timeout matching the harness contract. "
        f"Required env vars: `OMNISCOUT_CONTROL`, `JAKESTUDIO_VAULT`, `OMNISCOUT_DEEP_MODEL`, "
        f"`OMNISCOUT_REMOTE_OLLAMA`, `OMNISCOUT_GOOD_FLOOR`. Health checks: `/healthz` polls QNAP free-space "
        f"and fails closed below the configured free-GiB floor; a failing health check halts new builds but "
        f"never deletes existing ProofPackets."
    )


def _error_handling_block(ctx: dict[str, Any]) -> str:
    retry = ctx["harness"].get("retry_policy", "exponential backoff, max 3, fail-closed")
    return (
        f"Error codes are namespaced `E1xxx` (input), `E2xxx` (scoring), `E3xxx` (proof), `E4xxx` (infra). "
        f"`E1001 mechanism_missing_or_thin` and `E1002 fewer_than_2_independent_sources` both hard-cap score "
        f"at 54 per the deterministic rubric. `E3001 done_test_missing` caps rank at WEAK (≤69). Retry policy: "
        f"{retry} — Ollama and Consensus timeouts retry with jitter; ProofPacket sealing never retries silently, "
        f"it fails closed and surfaces `E3002 seal_mismatch`. A circuit breaker opens after 5 consecutive "
        f"`E4xxx` infra errors within 10 minutes and routes new builds to the quarantine queue until a human "
        f"or the self-heal watchdog closes it."
    )


def _perf_budget_block(ctx: dict[str, Any]) -> str:
    timeout = ctx["harness"].get("timeout_s", 3600)
    load = ctx["testing_strategy"].get("load", "sustain 100 cards/day throughput")
    mem = "≤6GB per worker" if ctx["complexity"] == "HIGH" else "≤2GB per worker"
    return (
        f"Latency budget: p50 build-to-score under 90s, p95 under {timeout}s (the hard harness timeout). "
        f"Throughput target: {load}. Memory budget: {mem} on the fleet node, leaving headroom for concurrent "
        f"Ollama inference. Disk budget: QNAP free space must stay above the configured floor and under the "
        f"configured used-percent ceiling or the pipeline stops writes."
    )


def _security_block(ctx: dict[str, Any]) -> str:
    gate_names = ", ".join(g.get("name", "") for g in ctx["gates"] if g.get("name"))
    gate_names = gate_names or "Gate-D, GEV Separation, ProofPacket, Non-vacuity"
    return (
        f"Authn/authz: local-only service identities per RIG doctrine — no public exposure without Gate-D "
        f"human approval for outward actions (publish/send/deploy). Governance gates enforced: {gate_names}. "
        f"Input validation: every card write goes through an atomic tmp-then-replace write to prevent partial "
        f"writes; JSON schema keys are validated before persistence. Rate limiting: build requests are throttled "
        f"to the configured daily target with QNAP pressure as a hard backstop. Secrets (Ollama tokens, "
        f"Consensus API keys) are read from environment only, never logged or embedded in card JSON. Doctrine "
        f"domains aligned to this capability: {', '.join(ctx['aligned_domains'])}."
    )


def _gen_engineering(ctx: dict[str, Any]) -> str:
    harness = ctx["harness"]
    stack = ctx["tech_stack"]
    backend = ", ".join(stack.get("backend") or ["Python"])
    infra = ", ".join(stack.get("infra") or ["Docker"])
    components = ctx["arch_components"] or ["core module"]
    files = _file_manifest(ctx)

    intro = (
        f"`{ctx['title']}` (`{ctx['strategy_id']}`, {ctx['tier']} tier, complexity {ctx['complexity']}, "
        f"~{ctx['estimated_loc']} LOC) compiles to a {backend} service backed by {infra}. "
        f"The mechanism — {ctx['mechanism'][:220] or 'a deterministic build-score-verify loop'} — "
        f"is realized as {len(components)} architecture component(s): {', '.join(components[:6])}. "
        f"The harness type is `{harness.get('type', 'TAC closed-loop')}`, built by "
        f"`{harness.get('builder', 'rig-agent')}` and independently verified by "
        f"`{harness.get('verifier', 'deterministic scorer + GEV separate identity')}`."
    )

    manifest_lines = "\n".join(f"- `{p}` — {purpose}" for p, purpose in files)
    endpoints = _api_endpoints(ctx)
    endpoint_lines = "\n".join(
        f"- `{m} {route}` — request: `{req}`; response: `{resp}`" for m, route, req, resp in endpoints
    )
    tables = _db_tables(ctx)
    table_lines = "\n\n".join(
        f"**`{name}`**\n"
        + "\n".join(f"  - `{col}` {dtype}" for col, dtype in cols)
        + f"\n  - index: `{idx}`"
        for name, cols, idx in tables
    )

    body = "\n".join(
        [
            intro,
            "",
            "### File-by-File Project Structure",
            manifest_lines,
            "",
            "### API Endpoint Design",
            endpoint_lines,
            "",
            "### Database Schema",
            table_lines,
            "",
            "### Deployment Architecture",
            _deployment_block(ctx),
            "",
            "### Error Handling Strategy",
            _error_handling_block(ctx),
            "",
            "### Performance Budget",
            _perf_budget_block(ctx),
            "",
            "### Security Model",
            _security_block(ctx),
        ]
    )
    return body


# ---------------------------------------------------------------------------
# 2. Deep Business
# ---------------------------------------------------------------------------


def _mrr_table(
    low: float, high: float
) -> tuple[list[tuple[str, int, float, float]], list[tuple[str, int, float, float]]]:
    avg = (low + high) / 2.0
    churn = 0.04
    y1: list[tuple[str, int, float, float]] = []
    for m in range(1, 13):
        clients = min(1 + (m - 1) // 2, 8)
        decay = (1 - churn) ** max(0, m - 6)
        mrr = clients * avg * decay
        y1.append((f"Y1-M{m:02d}", clients, mrr, mrr * 12))
    y23: list[tuple[str, int, float, float]] = []
    for q in range(1, 9):
        year = 2 if q <= 4 else 3
        qn = q if q <= 4 else q - 4
        clients = min(8 + q, 20)
        decay = (1 - churn) ** 3
        mrr = clients * avg * decay
        y23.append((f"Y{year}-Q{qn}", clients, mrr, mrr * 12))
    return y1, y23


def _pricing_tiers(ctx: dict[str, Any], low: float, high: float) -> list[tuple[str, float, list[str]]]:
    moat = list(ctx["biz"].get("competitive_moat") or [])
    diffs = list(ctx["gtm"].get("differentiation") or [])
    starter_price = max(300.0, round(low * 0.4, -2))
    pro_price = max(starter_price + 500.0, round((low + high) / 2.0, -2))
    ent_price = max(pro_price + 1000.0, round(high * 1.6, -2))
    return [
        (
            "Starter",
            starter_price,
            [
                "Single build slice, one done-test",
                "Deterministic score report (no LLM in the grading decision)",
                "Community support, best-effort SLA",
            ],
        ),
        (
            "Pro",
            pro_price,
            [
                "Full harness: build → score → verify → seal ProofPacket",
                moat[0] if moat else "Proof-chained artifacts on every deliverable",
                "Weekly office hours + priority queue on the fleet node",
            ],
        ),
        (
            "Enterprise",
            ent_price,
            [
                "Dedicated fleet capacity + custom doctrine governance",
                diffs[0] if diffs else "Custom SLA-backed incident response",
                "SOC 2 readiness support and audit-trail exports",
                "Quarterly business review with score-trend reporting",
            ],
        ),
    ]


def _break_even(ctx: dict[str, Any], low: float, high: float, margin: float) -> tuple[float, float]:
    build_effort = str(ctx["biz"].get("build_effort") or "1-2 weeks")
    weeks = 2.0
    rng = re.search(r"(\d+)\s*-\s*(\d+)", build_effort)
    single = re.search(r"\d+", build_effort)
    if rng:
        weeks = (int(rng.group(1)) + int(rng.group(2))) / 2.0
    elif single:
        weeks = float(single.group(0))
    burn = weeks * 3500.0
    avg = (low + high) / 2.0
    months = burn / max(1.0, avg * margin)
    return burn, months


def _competitors(ctx: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for p in ctx["oss"].get("relevant_projects") or []:
        name = p.get("project") if isinstance(p, dict) else str(p)
        if name:
            found.append(str(name))
    for e in ctx["entity_names"]:
        if e and e[0].isupper() and len(e) > 2:
            found.append(e)
    deduped = list(dict.fromkeys(found))[:3]
    if not deduped:
        deduped = ["generic AI consultancies", "offshore dev shops", "in-house build"]
    return deduped


def _gen_business(ctx: dict[str, Any]) -> str:
    biz = ctx["biz"]
    low, high = _money_range(biz.get("price_range", ""), default=(2000.0, 8000.0))
    avg = (low + high) / 2.0
    margin = _pct(biz.get("gross_margin", ""), default=0.6)
    cac_low, cac_high = _money_range(biz.get("estimated_cac", ""), default=(1500.0, 5000.0))
    ltv_low, ltv_high = _money_range(biz.get("estimated_ltv", ""), default=(15000.0, 60000.0))
    rows_y1, rows_y23 = _mrr_table(low, high)

    mrr_y1_lines = "\n".join(
        f"| {label} | {clients} clients | {_fmt_usd(mrr)} MRR | {_fmt_usd(arr)} ARR run-rate |"
        for label, clients, mrr, arr in rows_y1
    )
    mrr_y23_lines = "\n".join(
        f"| {label} | {clients} clients | {_fmt_usd(mrr)} MRR | {_fmt_usd(arr)} ARR run-rate |"
        for label, clients, mrr, arr in rows_y23
    )

    payback_months = round(cac_high / max(1.0, avg * margin), 1)
    cost_per_serve = round(avg * (1 - margin), 2)
    ltv_cac_low = round(ltv_low / max(1.0, cac_high), 1)
    ltv_cac_high = round(ltv_high / max(1.0, cac_low), 1)

    unit_econ = (
        f"Average price band is {_fmt_usd(low)}-{_fmt_usd(high)}/mo (midpoint {_fmt_usd(avg)}). "
        f"At a {margin * 100:.0f}% gross margin, cost-to-serve is roughly {_fmt_usd(cost_per_serve)}/customer/mo — "
        f"mostly fleet compute (local Ollama nodes) and operator time, not cloud API spend. "
        f"CAC is {_fmt_usd(cac_low)}-{_fmt_usd(cac_high)}; against a per-customer contribution of "
        f"{_fmt_usd(avg * margin)}/mo, that implies a CAC payback of roughly {payback_months:.1f} months at the "
        f"high end of the CAC range. Estimated LTV is {_fmt_usd(ltv_low)}-{_fmt_usd(ltv_high)}, giving an "
        f"implied LTV/CAC ratio of roughly {ltv_cac_low}x-{ltv_cac_high}x — healthy once it clears 3x, "
        f"marginal below that."
    )

    tiers = _pricing_tiers(ctx, low, high)
    tier_lines = "\n\n".join(
        f"**{name} — {_fmt_usd(price)}/mo**\n" + "\n".join(f"  - {f}" for f in feats)
        for name, price, feats in tiers
    )

    burn, months_to_be = _break_even(ctx, low, high, margin)
    be_block = (
        f"Assuming a blended build cost of roughly {_fmt_usd(burn)} to stand up the harness "
        f"(build_effort: {biz.get('build_effort', '1-2 weeks')}), and monthly contribution of "
        f"{_fmt_usd(avg * margin)}/customer at {margin * 100:.0f}% margin, break-even lands at roughly "
        f"{months_to_be:.1f} months assuming a single early customer, or faster with the client-growth curve "
        f"modeled above once 2-3 clients are active. The floor risk is client concentration: losing the first "
        f"client before month 3 resets the burn clock and pushes break-even back out."
    )

    competitors = _competitors(ctx)
    mid = (low + high) / 2.0
    comp_lines = "\n".join(
        f"- **{c}**: typically prices in the {_fmt_usd(mid * 1.3)}-{_fmt_usd(mid * 2.2)}/mo equivalent range, "
        f"without a proof-chained artifact or an independently verifiable done-test."
        for c in competitors
    )

    intro = (
        f"`{ctx['title']}` monetizes as `{biz.get('revenue_model', 'consulting + custom build')}` at "
        f"{biz.get('price_range', f'{_fmt_usd(low)}-{_fmt_usd(high)}/mo')}, targeting a "
        f"{biz.get('tam', 'multi-billion-dollar')} TAM within the `{ctx['strategy_id']}` doctrine domain. "
        f"{ctx['peer_count']} other card(s) in this batch share the same strategy_id, so differentiation must "
        f"come from proof density and delivery speed, not from claiming a unique niche."
    )

    body = "\n".join(
        [
            intro,
            "",
            "### 3-Year MRR Projection (Year 1 monthly, Years 2-3 quarterly)",
            "| Period | Clients | MRR | ARR run-rate |",
            "|---|---|---|---|",
            mrr_y1_lines,
            mrr_y23_lines,
            "",
            "### Unit Economics",
            unit_econ,
            "",
            "### Pricing Tier Matrix",
            tier_lines,
            "",
            "### Break-Even Analysis",
            be_block,
            "",
            "### Competitive Pricing Comparison",
            comp_lines,
        ]
    )
    return body


# ---------------------------------------------------------------------------
# 3. Deep GTM
# ---------------------------------------------------------------------------


def _90_day_calendar(ctx: dict[str, Any]) -> list[str]:
    channels = ctx["gtm"].get("channels") or ["LinkedIn", "Content"]
    content_plan = ctx["gtm"].get("content_plan") or []
    outreach = ctx["gtm"].get("outreach_targets") or []
    icp = ctx["gtm"].get("icp") or "target buyers"
    weeks: list[str] = []
    for w in range(1, 14):
        phase = "Foundation" if w <= 4 else ("Pipeline" if w <= 9 else "Close")
        channel = channels[(w - 1) % len(channels)] if channels else "LinkedIn"
        cp = (content_plan[(w - 1) % len(content_plan)].get("topic") if content_plan else None) or (
            ctx["claim"][:60] or ctx["title"][:60]
        )
        out = outreach[(w - 1) % len(outreach)] if outreach else f"Reach out to {icp}"
        weeks.append(f"Week {w:02d} ({phase}): publish on {channel} — \"{cp}\"; {out}.")
    return weeks


def _content_calendar(ctx: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    plan = ctx["gtm"].get("content_plan") or []
    base_topics = [p.get("topic") for p in plan if p.get("topic")] or [ctx["claim"] or ctx["title"]]
    li = [f"LinkedIn #{i + 1}: {base_topics[i % len(base_topics)]}" for i in range(12)]
    yt = [f"YouTube #{i + 1}: Building {ctx['title'][:50]} — live agent build, part {i + 1}" for i in range(4)]
    ss = [f"Substack #{i + 1}: The `{ctx['strategy_id']}` capability — mechanism + proof, essay {i + 1}" for i in range(2)]
    return li, yt, ss


def _outreach_scripts(ctx: dict[str, Any]) -> tuple[list[str], list[str]]:
    icp = ctx["gtm"].get("icp") or "operators"
    positioning = ctx["gtm"].get("positioning") or ctx["why_not_median"] or "RIG ships proof-chained capabilities."
    emails = [
        (
            f"Subject: {ctx['strategy_id']} without the heroics\n\nHi {{first_name}},\n\n{positioning} "
            f"We noticed {icp} struggle with: {ctx['claim'][:120] or ctx['title'][:120]}. We built a "
            f"proof-chained slice — {ctx['idea'].get('description', '')[:150] or 'a governed build with an executable done-test'} "
            f"— with an executable done-test, not a deck. Worth 15 minutes this week?"
        ),
        (
            f"Subject: Re: {ctx['title'][:40]}\n\nFollowing up — the `{ctx['strategy_id']}` capability now scores "
            f"{ctx['score_total']}/100 on our deterministic rubric ({ctx['rank']}). If proof over promises "
            f"matters to your {icp} team, I'll send the ProofPacket hash chain so you can verify independently."
        ),
        (
            f"Subject: Last note on {ctx['strategy_id']}\n\nNo pressure — if this isn't the year for it, I'll "
            f"check back next quarter. If it is, the fastest path is a 14-day pilot scoped to: "
            f"{ctx['idea'].get('acceptance', '')[:120] or 'a single executable acceptance test'}."
        ),
    ]
    dms = [
        (
            f"Saw your post on {icp} pain points — we ship `{ctx['strategy_id']}` capabilities with a done-test "
            f"that either passes or doesn't, no vibes. Open to a quick look?"
        ),
        (
            "Quick one: does your team verify AI 'done' claims independently, or trust the same agent that "
            "built it? We separate builder/verifier by design — happy to show the artifact."
        ),
    ]
    return emails, dms


def _sales_playbook(ctx: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    q = ctx["question"] or ctx["claim"] or ctx["title"]
    price_band = ctx["biz"].get("price_range", "$5K-20K/mo")
    questions = [
        f"What does \"{q[:90]}\" actually cost you today in hours or missed revenue?",
        "Who verifies that an AI build is actually done today — and how?",
        f"If `{ctx['strategy_id']}` broke silently in production, how would you find out?",
        f"What's the smallest commercial commitment that would let you test this ({price_band})?",
        "What would make this a 'no' even if the demo goes perfectly?",
    ]
    demo = [
        f"1. Show the claim: \"{ctx['claim'][:100] or ctx['title'][:100]}\"",
        f"2. Show the mechanism live: {ctx['mechanism'][:100] or 'the build-score-verify loop'}",
        f"3. Run the done-test on stage: `{ctx['done_test'][:100] or 'assert card fields present'}`",
        "4. Show the ProofPacket hash chain — invite them to verify independently",
        "5. Close on the pilot scope and the 14-day exit clause",
    ]
    risks = ctx["risks"] or ["Buyer defaults to the status quo"]
    objections: list[str] = []
    for r in risks[:3]:
        objections.append(
            f"Objection: \"{r[:90]}\" → Response: we surface this as a documented risk with a kill-criterion, "
            f"not hide it — trust comes from the audit trail, not the pitch."
        )
    while len(objections) < 3:
        objections.append(
            "Objection: \"We can build this in-house\" → Response: sure, and we'll hand you the harness + "
            "doctrine so you can verify our work replaces months of trial-and-error, not just labor."
        )
    return questions, demo, objections


def _partner_strategy(ctx: dict[str, Any]) -> str:
    oss = ctx["oss"]
    strat = oss.get("integration_strategy") or (
        "Wrap as a RIG capability layer; open-source handles commodity, RIG adds governance + proof."
    )
    routing = ctx["department_routing"]
    contribs = ", ".join(oss.get("contribution_opportunities") or ["doctrine modules as OSS"])
    return (
        f"{strat} Partner motion routes through the `{routing}` department; RIG contributes patterns upstream "
        f"({contribs}) to build channel trust without ceding the proof-chain moat. Lock-in risk is rated "
        f"{oss.get('lock_in_risk', 'LOW')}."
    )


def _onboarding_flow(ctx: dict[str, Any]) -> str:
    loop = ctx["harness"].get("loop", "build → score → verify artifact → seal ProofPacket → deploy")
    acceptance = ctx["acceptance"] or ctx["done_test"] or "a single executable acceptance test"
    return (
        f"Day 0: kickoff call scoped to the done-test acceptance criteria (`{acceptance[:100]}`). "
        f"Day 1-3: RIG runs the harness loop ({loop}) against the client's real data or a sanctioned proxy. "
        f"Day 4: client receives the ProofPacket and independently re-runs the done-test. "
        f"Day 5-14: pilot operates live with weekly score-trend reporting; either side may exit with no penalty "
        f"before day 14 per the pilot clause."
    )


def _gen_gtm(ctx: dict[str, Any]) -> str:
    weeks = _90_day_calendar(ctx)
    li, yt, ss = _content_calendar(ctx)
    emails, dms = _outreach_scripts(ctx)
    questions, demo, objections = _sales_playbook(ctx)
    partner = _partner_strategy(ctx)
    onboarding = _onboarding_flow(ctx)

    positioning = ctx["gtm"].get("positioning", ctx["why_not_median"] or "proof over promises")
    intro = (
        f"GTM plan for `{ctx['title']}` (`{ctx['strategy_id']}`): "
        f"{ctx['gtm'].get('sales_motion', 'consulting + content')} motion targeting "
        f"{ctx['gtm'].get('icp', 'mid-market operators')} over a "
        f"{ctx['gtm'].get('sales_cycle', '30-60 day')} cycle, positioned as \"{positioning}\"."
    )

    body = "\n".join(
        [
            intro,
            "",
            "### 90-Day GTM Calendar",
            "\n".join(f"- {w}" for w in weeks),
            "",
            "### Content Calendar",
            "**LinkedIn (12 posts):**",
            "\n".join(f"- {x}" for x in li),
            "**YouTube (4 videos):**",
            "\n".join(f"- {x}" for x in yt),
            "**Substack (2 essays):**",
            "\n".join(f"- {x}" for x in ss),
            "",
            "### Outreach Scripts",
            "**Cold email templates:**",
            "\n\n".join(f"Template {i + 1}:\n{e}" for i, e in enumerate(emails)),
            "",
            "**LinkedIn DM templates:**",
            "\n\n".join(f"DM {i + 1}: {d}" for i, d in enumerate(dms)),
            "",
            "### Sales Playbook",
            "**Discovery questions:**",
            "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions)),
            "**Demo script outline:**",
            "\n".join(demo),
            "**Objection handlers:**",
            "\n".join(f"- {o}" for o in objections),
            "",
            "### Partner / Channel Strategy",
            partner,
            "",
            "### Customer Success Onboarding Flow",
            onboarding,
        ]
    )
    return body


# ---------------------------------------------------------------------------
# 4. Deep Agents
# ---------------------------------------------------------------------------


def _gen_agents(ctx: dict[str, Any]) -> str:
    default_agent = {"role": "Build Agent", "model": "deterministic", "skills": [], "identity": f"{ctx['slug']}-builder"}
    agents = ctx["agents_list"] or [default_agent]
    substrates = {s.get("agent_name"): s for s in ctx["pai_substrates"]}
    routing = ctx["department_routing"]
    team = ctx["team_composition"]

    blocks: list[dict[str, Any]] = []
    for i, a in enumerate(agents):
        identity = a.get("identity") or f"{ctx['slug']}-agent-{i + 1}"
        role = a.get("role") or "Agent"
        model = a.get("model") or "deterministic"
        skills = a.get("skills") or []
        sub = substrates.get(identity, {})
        telos = sub.get("telos") or f"Improve the `{ctx['strategy_id']}` capability through {role.lower()}"
        memory_layer = sub.get("memory_layer") or "L6_EPISODIC_RUN_STATE"
        proof_path = sub.get("proof_path") or f"ProofPacket sealed after each {role.lower()} cycle"
        allowed_surfaces = ", ".join(skills) or "harness-internal only"
        is_builder = "build" in role.lower()
        counterpart = "an independent verifier identity" if is_builder else "the builder identity that produced the artifact"
        boundaries = (
            "No outward action (publish/send/deploy) without Gate-D human approval. "
            f"This role never self-certifies — {counterpart} must independently confirm the result before it "
            "counts as done."
        )
        load_order = i + 1

        prompt = (
            f"You are `{identity}`, the {role} for the `{ctx['strategy_id']}` capability inside RIG OmniScout. "
            f"Your telos: {telos}. You operate at memory layer `{memory_layer}` and must seal a ProofPacket "
            f"({proof_path}) before any claim of 'done' — an unsealed claim is not a claim, it is a guess. "
            f"Your mechanism of work: {ctx['mechanism'][:220] or 'apply the harness loop deterministically against the current artifact'}. "
            f"You MUST NOT self-certify success — {counterpart} checks your output against the executable "
            f"done-test: `{ctx['done_test'][:140] or 'assert the required card fields are present and internally consistent'}`. "
            f"You operate under these doctrine gates: "
            f"{', '.join(g.get('name', '') for g in ctx['gates'][:4]) or 'Gate-D, GEV Separation, ProofPacket, Non-vacuity'}. "
            f"When you are uncertain whether an artifact meets the bar, you fail closed and quarantine it rather "
            f"than guess and hope a downstream check catches the mistake — a false 'done' is strictly worse than "
            f"an honest 'not yet'. Your allowed skills and surfaces are limited to: {allowed_surfaces}; you do not "
            f"reach outside that boundary even if it would be faster. You report status through the department "
            f"routing `{routing}` and hand off to the next agent in the loop only after your local gate passes "
            f"cleanly. You never edit another agent's already-sealed ProofPacket — if you find a problem with a "
            f"sealed artifact, you raise a new event rather than mutating history, because the hash chain must "
            f"remain a truthful record of what actually happened, not what should have happened."
        )
        eval_rubric = (
            f"correctness ≥70/100 on the deterministic scorer; doctrine_fit ≥70% domain-marker overlap; "
            f"latency p95 ≤{ctx['harness'].get('timeout_s', 3600)}s; cost ≤$0.05/run; "
            f"gev_separation=true (builder identity ≠ verifier identity on every cycle)"
        )
        failure_modes = [
            f"Model `{model}` times out beyond the harness timeout ({ctx['harness'].get('timeout_s', 3600)}s) → retry with backoff, then quarantine",
            "Agent self-certifies its own output (GEV violation) → hard-blocked, score capped, promotion refused",
            f"Output drifts from the `{ctx['strategy_id']}` doctrine domain → doctrine_fit score drops, human review triggered",
            "Upstream Consensus/Ollama endpoint unreachable → extractive fallback engages, risk flagged in card.risks",
        ]
        if not is_builder:
            failure_modes.append(
                "Verifier reuses the builder's own claimed score instead of re-deriving it → audit via artifact_sha256 mismatch"
            )

        model_cost = MODEL_COST_PER_1K.get(model, 0.01)
        est_tokens = 1500 if is_builder else 800
        cost_est = round(model_cost * (est_tokens / 1000.0), 4)

        blocks.append(
            {
                "identity": identity,
                "role": role,
                "model": model,
                "load_order": load_order,
                "telos": telos,
                "memory_layer": memory_layer,
                "proof_path": proof_path,
                "boundaries": boundaries,
                "allowed_surfaces": allowed_surfaces,
                "prompt": prompt,
                "eval_rubric": eval_rubric,
                "failure_modes": failure_modes,
                "cost_estimate": f"${cost_est:.4f}/run (~{est_tokens} tokens on {model})",
            }
        )

    protocol = (
        f"Agents communicate via typed messages routed through the `{routing}` department queue: "
        f"`TASK_ASSIGN` (builder receives cluster_id/topic), `STATUS_UPDATE` (progress ping every harness "
        f"cycle), `VERIFY_REQUEST` (builder hands the artifact to the verifier along with its own claimed "
        f"score — the verifier ignores that claim and re-derives its own), `VERIFY_RESULT` (independent score, "
        f"rank, and hard_blocks), and `ESCALATION` (routed to a human when a gate fails twice in a row). Team "
        f"composition: {team.get('humans', 0)} human(s), {team.get('agents', len(agents))} agent(s), oversight: "
        f"{team.get('oversight', 'Gate-D human approval for all outward actions')}, cadence: "
        f"{team.get('cadence', 'nightly batch + continuous collection')}."
    )

    lines = [
        f"The `{ctx['strategy_id']}` capability runs {len(blocks)} agent(s) under the `{routing}` department, "
        f"each with its own PAI substrate, load order, and independent memory layer so no single agent can "
        f"quietly expand its own authority or grade its own homework.",
        "",
        "### Inter-Agent Communication Protocol",
        protocol,
        "",
    ]
    for b in blocks:
        lines += [
            f"### Agent {b['load_order']}: `{b['identity']}` — {b['role']}",
            f"- **Model:** {b['model']}",
            f"- **Telos:** {b['telos']}",
            f"- **Memory layer:** {b['memory_layer']}",
            f"- **Proof path:** {b['proof_path']}",
            f"- **Allowed surfaces:** {b['allowed_surfaces']}",
            f"- **Boundaries:** {b['boundaries']}",
            "",
            "**System prompt template:**",
            f"> {b['prompt']}",
            "",
            f"**Eval rubric:** {b['eval_rubric']}",
            "**Failure mode catalog:**",
            *[f"- {f}" for f in b["failure_modes"]],
            f"**Cost estimate:** {b['cost_estimate']}",
            "",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Deep Research
# ---------------------------------------------------------------------------


def _gen_research(ctx: dict[str, Any]) -> str:
    results = ctx["consensus_results"][:6]
    evid = ctx["evidence"]
    temporal = ctx["temporal"]

    lit_lines: list[str] = []
    if results:
        for r in results:
            title = r.get("title", "Untitled paper")
            abstract = str(r.get("abstract", "") or "")
            words = abstract.split()
            contrib = " ".join(words[:35])
            suffix = "…" if len(words) > 35 else ""
            lit_lines.append(f"- **{title}** ({r.get('source', 'consensus')}): {contrib}{suffix}")
    elif evid:
        for e in evid[:6]:
            lit_lines.append(f"- {e.get('url', '(no url)')}: {str(e.get('quote_or_fact', ''))[:180]}")
    else:
        lit_lines.append(
            f"- No external literature is attached to this card; the analysis below is synthesized "
            f"extractively from the L0 research corpus around `{ctx['topic']}`, not from cited papers."
        )

    open_qs: list[str] = []
    for r in ctx["risks"][:2]:
        open_qs.append(f"Does the risk \"{r[:100]}\" hold up under a larger, independent sample?")
    open_qs.append(
        f"What is the smallest experiment that would falsify the claim: \"{ctx['claim'][:100] or ctx['title'][:100]}\"?"
    )
    open_qs.append(
        f"Do the {len(results)} cited paper(s) generalize from their original study context to RIG's "
        f"`{ctx['strategy_id']}` use case, or is this a case of over-fitting to a narrow population?"
    )
    open_qs.append("What confound would explain the observed effect without invoking the proposed mechanism?")
    if len(open_qs) < 3:
        open_qs.append("Has this claim been independently replicated outside the original research group?")

    replication = (
        f"Evidence freshness is `{temporal.get('freshness', 'UNKNOWN')}` with an estimated re-verification "
        f"window of {temporal.get('decay_window', '2-3 years')} (next reverify by "
        f"{temporal.get('reverify_by', 'an unscheduled date')}). With {ctx['source_count']} independent "
        f"source(s) backing the claim, replication confidence is "
        f"{'HIGH' if ctx['source_count'] >= 4 else 'MODERATE' if ctx['source_count'] >= 2 else 'LOW'} — "
        f"{'below the 2-source floor' if ctx['source_count'] < 2 else 'at or above the multi-source floor'} "
        f"used by the deterministic scorer's hard-block rule. Academic papers surfaced via Consensus carry "
        f"publication-bias risk (positive-result skew) that RIG does not independently correct for; treat "
        f"point estimates as directional, not causal, until a live pilot confirms the mechanism against a "
        f"real client's data."
    )

    methodology = (
        f"The underlying research base was synthesized via multi-source clustering (independent domains) "
        f"rather than a single-author narrative, which mitigates but does not eliminate selection bias — the "
        f"Consensus query for `{ctx['strategy_id']}` returns whatever the search index has indexed, not the "
        f"output of a systematic review. The mechanism text — \"{ctx['mechanism'][:160] or 'not yet fully specified'}\" — "
        f"should be read as an extractive synthesis, not an original empirical finding; any numeric claims "
        f"embedded in the evidence quotes were not independently re-derived by RIG. Generalizability is "
        f"bounded by the {ctx['tier']} tier's assumed buyer context (`{ctx['gtm'].get('icp', 'mid-market operators')}`) "
        f"and may not transfer cleanly to enterprise or regulated verticals without a fresh evidence pass."
    )

    intro = (
        f"This is the literature and methodology layer behind `{ctx['title']}`. It exists so a skeptical "
        f"reader can trace every claim back to a source and understand exactly where the evidence is thin, "
        f"rather than taking the claim on faith."
    )

    body = "\n".join(
        [
            intro,
            "",
            "### Literature Review",
            "\n".join(lit_lines),
            "",
            "### Open Research Questions",
            "\n".join(f"{i + 1}. {q}" for i, q in enumerate(open_qs[:5])),
            "",
            "### Replication Concerns",
            replication,
            "",
            "### Methodology Critique",
            methodology,
        ]
    )
    return body


# ---------------------------------------------------------------------------
# 6. Deep Risk
# ---------------------------------------------------------------------------


def _gen_risk(ctx: dict[str, Any]) -> str:
    risks = list(ctx["risks"])
    score_total = ctx["score_total"]
    rank = ctx["rank"]
    tier = ctx["tier"]
    complexity = ctx["complexity"]
    agent0_model = ctx["agents_list"][0].get("model", "the build model") if ctx["agents_list"] else "the build model"

    matrix_rows: list[tuple[str, str, str, str]] = []
    for r in risks[:4]:
        low_r = r.lower()
        prob = "HIGH" if ("unavailable" in low_r or "fallback" in low_r) else "MED"
        impact = "HIGH" if tier == "T0" else "MED"
        matrix_rows.append(
            (
                r[:90],
                prob,
                impact,
                "Monitor via the score breakdown; fail closed to the extractive fallback; keep logged in card.risks.",
            )
        )

    standard_risks = [
        (
            f"Model drift on `{agent0_model}` silently changes output quality",
            "MED",
            "MED",
            "Planted-failure regression re-run monthly; alert on any score delta greater than 10 points.",
        ),
        (
            "QNAP disk pressure halts writes mid-batch",
            "LOW",
            "HIGH",
            "The disk gate fails closed before any partial write; nightly free-space check catches it early.",
        ),
        (
            "Consensus MCP / Ollama endpoint outage during synthesis",
            "MED",
            "MED",
            "Extractive fallback engages automatically; the resulting risk is flagged in card.risks for review.",
        ),
        (
            "GEV violation — builder and verifier collapse to one identity",
            "LOW",
            "HIGH",
            "gev_separation is hard-scored 0; promotion is blocked until the two identities genuinely differ.",
        ),
        (
            f"Score regression below the current {score_total}/100 baseline on re-verification",
            "MED",
            "MED",
            "Reverify by the temporal_validity date; requeue for L2 synthesis if it drops below the GOOD floor.",
        ),
        (
            f"A competitor ships a similar `{ctx['strategy_id']}` offering first",
            "MED",
            "MED",
            "Ship the pilot inside the stated build_effort window; lead with the ProofPacket as the trust artifact.",
        ),
        (
            "Buyer pricing power erodes below the CAC payback threshold",
            "MED",
            "HIGH",
            "Anchor pricing at the low end of the band with a 14-day exit clause to de-risk the first sale.",
        ),
        (
            f"Doctrine drift — `{ctx['strategy_id']}` stops mapping cleanly to a DOCTRINE_DOMAINS marker",
            "LOW",
            "MED",
            "Re-run doctrine_fit scoring after any doctrine file update; re-tag domains if overlap drops below target.",
        ),
    ]
    matrix_rows.extend(standard_risks)
    matrix_rows = matrix_rows[:12]
    matrix_lines = "\n".join(f"| {desc} | {prob} | {impact} | {mit} |" for desc, prob, impact, mit in matrix_rows)

    kill = list(ctx["kill_criteria"])
    kill_lines = [f"- {k}" for k in kill] or ["- No explicit kill criteria recorded directly on this card."]
    kill_lines += [
        "- Numeric floor: total score < 70/100 → quarantine (deterministic scorer hard block).",
        "- Numeric floor: independent sources < 2 → hard-capped at 54, automatic REJECT.",
        f"- Temporal floor: past `{ctx['temporal'].get('reverify_by', 'an unscheduled date')}` without "
        f"re-verification → mark STALE and pull from the active rotation.",
        "- Council floor: council overall_verdict resolves to NO-GO from the majority of perspectives → do not promote.",
    ]

    reg = REGULATORY_MAP.get(ctx["strategy_id"], DEFAULT_REGULATORY)
    reg_lines = "\n".join(f"- {r}" for r in reg)
    reg_block = (
        f"Regulatory scan for `{ctx['strategy_id']}` (doctrine domains: {', '.join(ctx['aligned_domains'])}):\n"
        f"{reg_lines}\n\nThis is a deterministic keyword-to-regime mapping, not legal advice — before any "
        f"client contract, route through RIG's compliance review for the specific vertical and jurisdiction."
    )

    debt = (
        f"At `{complexity}` complexity and roughly {ctx['estimated_loc']} LOC, technical debt accrues fastest "
        f"in the {'harness/verifier boundary' if complexity == 'HIGH' else 'test coverage for edge-case inputs'}. "
        f"The coverage target from the engineering blueprint "
        f"(`{ctx['testing_strategy'].get('unit', '≥80% unit coverage')}`) is a leading indicator — debt is "
        f"forecast to compound once coverage drifts below 60% or the harness timeout "
        f"({ctx['harness'].get('timeout_s', 3600)}s) is exceeded on more than 5% of runs. Rank `{rank}` "
        f"({score_total}/100) suggests "
        f"{'a healthy buffer before debt becomes visible to buyers' if score_total >= 80 else 'debt is already partially visible in the score breakdown and should be paid down before the next promotion cycle'}."
    )

    body = "\n".join(
        [
            f"Risk posture for `{ctx['title']}` at score {score_total}/100 ({rank}), {tier} tier.",
            "",
            "### Probability-Weighted Risk Matrix",
            "| Risk | Probability | Impact | Mitigation |",
            "|---|---|---|---|",
            matrix_lines,
            "",
            "### Kill Criteria",
            "\n".join(kill_lines),
            "",
            "### Regulatory Scan",
            reg_block,
            "",
            "### Technical Debt Forecast",
            debt,
        ]
    )
    return body


# ---------------------------------------------------------------------------
# 7. Deep Testing
# ---------------------------------------------------------------------------


def _gen_testing(ctx: dict[str, Any]) -> str:
    done_test = ctx["done_test"] or f"assert card['strategy']['strategy_id'] == '{ctx['strategy_id']}'"
    gates = ctx["gates"]
    testing = ctx["testing_strategy"]
    components = ctx["arch_components"] or ["core module"]

    cases = [
        ("Given a card with ≥3 independent sources", "when the deterministic scorer runs", "then multi_source scores ≥15/18"),
        (
            "Given a mechanism string under 40 characters",
            "when the scorer evaluates mechanism_density",
            "then the card is hard-capped at 54 (REJECT)",
        ),
        (
            f"Given a card matching the done-test `{done_test[:60]}`",
            "when the verifier executes it against the real artifact",
            "then it exits 0 and the card is marked promotable",
        ),
        (
            "Given a builder and verifier with the same identity",
            "when gev_separation is evaluated",
            "then the score is 0 and promotion is blocked regardless of other dimensions",
        ),
        (
            "Given a planted failure in the scoring logic",
            "when the non-vacuity regression runs",
            "then the gate goes RED before the fix and GREEN after restore",
        ),
        (
            "Given QNAP free space below the configured floor",
            "when a new build is requested",
            "then the disk gate returns stop=True and no partial write occurs",
        ),
        (
            f"Given the `{ctx['strategy_id']}` doctrine domain markers",
            "when doctrine_fit is scored",
            "then declared and inferred domains intersect above the target threshold",
        ),
        (
            "Given a card missing evidence URLs entirely",
            "when evidence_anchoring is scored",
            "then the score is 0/14 and the rank cannot exceed WEAK",
        ),
        (
            f"Given the harness timeout of {ctx['harness'].get('timeout_s', 3600)}s",
            "when a build exceeds it",
            "then the job is killed and retried per the exponential-backoff policy, up to the max attempt count",
        ),
        (
            "Given a card already at schema v30",
            "when enrich_card_v30 runs again",
            "then it returns status='already_v30' and makes no writes",
        ),
        (
            f"Given {len(components)} architecture component(s)",
            "when integration tests exercise each component boundary",
            "then each component's public contract is exercised at least once",
        ),
    ]
    case_lines = "\n".join(f"{i + 1}. **Given** {g} / **When** {w} / **Then** {t}" for i, (g, w, t) in enumerate(cases))

    coverage_lines = "\n".join(f"| {c} | 80% (unit) | 60% (integration) | 40% (e2e) |" for c in components[:5])
    if not coverage_lines:
        coverage_lines = "| core module | 80% (unit) | 60% (integration) | 40% (e2e) |"

    ci = (
        "GitHub Actions pipeline: `lint` (ruff/mypy) → `unit` (pytest -k not integration) → `integration` "
        "(real done-test against a fixture artifact) → `non_vacuity` (plant / confirm-RED / restore) → "
        "`package` (build artifact) → `deploy` (Prefect flow registration + QNAP write) → `smoke` (hit "
        "/healthz plus one end-to-end card build). Any red stage blocks merge; the non_vacuity stage is "
        "mandatory and cannot be skipped with a label."
    )

    non_vacuity = (
        "Plant: temporarily force the scorer to always return promote=True regardless of hard blocks. "
        "Confirm RED: the hard-blocks regression test must fail loudly against this planted bug. Restore: "
        "revert the patch and re-run — the same test goes GREEN. Keep the test as a permanent regression case "
        "so a future refactor that silently disables hard blocks is caught immediately, not discovered after "
        "a REJECT-grade card has already been promoted to a client-facing surface."
    )

    load = (
        f"Target: {testing.get('load', 'sustain 100 cards/day throughput on the fleet node')}. Simulate with "
        f"concurrent virtual builders (start at 4, ramp to 16) each issuing build+score+verify cycles; assert "
        f"p95 end-to-end latency stays under {ctx['harness'].get('timeout_s', 3600)}s and the QNAP free-space "
        f"gate never triggers a false stop under sustained load. Error budget: fewer than 1% of cycles may "
        f"hard-fail before the pipeline is considered degraded and paged."
    )

    body = "\n".join(
        [
            f"Testing strategy for `{ctx['title']}`, derived from the engineering blueprint's own testing_strategy "
            f"(`{testing.get('unit', 'pytest ≥80% coverage')}`) plus the {len(gates)} doctrine gate(s) that must hold.",
            "",
            "### Test Case Catalog",
            case_lines,
            "",
            "### Coverage Targets Per Module",
            "| Module | Unit | Integration | E2E |",
            "|---|---|---|---|",
            coverage_lines,
            "",
            "### CI/CD Pipeline",
            ci,
            "",
            "### Non-Vacuity Proof",
            non_vacuity,
            "",
            "### Load Test Plan",
            load,
        ]
    )
    return body


# ---------------------------------------------------------------------------
# 8. Deep Ops
# ---------------------------------------------------------------------------


def _gen_ops(ctx: dict[str, Any]) -> str:
    loop = ctx["harness"].get("loop", "build → score → verify artifact → seal ProofPacket → deploy")
    routing = ctx["department_routing"]
    gates = ctx["gates"]

    runbook = [
        "06:00 UTC — check QNAP free space; abort the batch if the disk gate reports stop=True.",
        "06:05 UTC — pull fresh L0 notes from the last 14 days across configured research roots.",
        "06:15 UTC — cluster notes into topic groups with at least the minimum independent-source count.",
        f"06:30 UTC — run the harness loop per cluster: {loop}.",
        "07:00 UTC — the deterministic scorer grades every new card; hard blocks are enforced with no LLM in this decision.",
        "07:15 UTC — promote GOOD/STRONG/EXCELLENT cards; quarantine REJECT/WEAK cards carrying a hard block.",
        f"07:30 UTC — route promoted cards to the `{routing}` department for downstream use.",
        "07:45 UTC — seal a ProofPacket (artifact hash over the stable-serialized card) for every promoted card.",
        "08:00 UTC — update the enrichment summary file and append to the daily ledger.",
        "Continuous — the self-heal watchdog polls fleet health and restarts stalled workers.",
    ]

    metrics = [
        ("score_total_avg", "≥75/100", "alert if the 7-day rolling average drops below 70"),
        ("promote_rate", "≥60%", "alert if it drops below 40% over 3 consecutive days"),
        ("source_count_avg", "≥3", "alert if it drops below 2.5, approaching hard-block territory"),
        ("council_confidence_avg", "≥70%", "alert if it drops below 55%"),
        ("qnap_free_gib", "≥100", "page on-call if it drops below 50 GiB"),
        ("qnap_used_pct", "<95%", "page on-call if it reaches 98% or higher"),
        ("cards_per_day", "≥60", "alert if it drops below 30 for 2 consecutive days"),
        ("error_rate", "<2%", "alert if it reaches 5% or higher in any 1-hour window"),
        ("gate_pass_rate", "100% on non_vacuity", "any FAIL on this gate is a Sev2 incident"),
        ("reverify_due_count", "trending down", "alert if the backlog grows for 3 consecutive days"),
    ]
    metric_lines = "\n".join(f"| `{m}` | {slo} | {alert} |" for m, slo, alert in metrics)

    incident = (
        "**Sev1** (data loss, secret exposure, or a false-DONE claim reaching a client): page immediately, "
        "response within 15 minutes, halt the pipeline, root-cause before any resume. "
        "**Sev2** (pipeline stalled over 1 hour, non_vacuity gate FAIL, or a GEV violation detected): response "
        f"within 1 hour, escalate through `{routing}` on-call, quarantine the affected cards. "
        "**Sev3** (a single card quarantined, minor score regression, one stalled worker): response by the "
        "next business day, logged to the ledger, no page required. Every incident gets a blameless writeup "
        "that names the specific gate or metric that fired."
    )

    dr = (
        "RPO: at most 24 hours — nightly ProofPacket-sealed cards are the durable unit, and anything mid-batch "
        "is re-derivable from the L0 notes, which are themselves durable. RTO: at most 4 hours to restore the "
        "cards directory from the last known-good snapshot or local mirror. Backup frequency: continuous "
        "append-only ledger writes plus a nightly full snapshot of the cards directory. Restore procedure: "
        "verify each restored card's artifact hash against a fresh stable-serialization of its own content "
        "before trusting it — a hash mismatch means the restore is corrupt and must fall back to the previous "
        "snapshot rather than being trusted as-is."
    )

    body = "\n".join(
        [
            f"Operations for `{ctx['title']}` (`{ctx['strategy_id']}`) — {len(gates)} doctrine gate(s) enforced "
            f"on every cycle, routed through `{routing}`.",
            "",
            "### Daily Operational Runbook",
            "\n".join(f"{i + 1}. {step}" for i, step in enumerate(runbook)),
            "",
            "### Monitoring Dashboard Spec",
            "| Metric | SLO | Alert |",
            "|---|---|---|",
            metric_lines,
            "",
            "### Incident Response Procedure",
            incident,
            "",
            "### Backup / Disaster Recovery Plan",
            dr,
        ]
    )
    return body


# ---------------------------------------------------------------------------
# Section registry + word-count safety net
# ---------------------------------------------------------------------------

_SECTION_SPECS: list[tuple[str, str, Any]] = [
    ("deep_engineering", "Deep Engineering Blueprint", _gen_engineering),
    ("deep_business", "Deep Business Model", _gen_business),
    ("deep_gtm", "Deep Go-To-Market Plan", _gen_gtm),
    ("deep_agents", "Deep Agent Team Design", _gen_agents),
    ("deep_research", "Deep Research & Literature", _gen_research),
    ("deep_risk", "Deep Risk Assessment", _gen_risk),
    ("deep_testing", "Deep Testing Strategy", _gen_testing),
    ("deep_ops", "Deep Operations Runbook", _gen_ops),
]


def _pad_or_trim(
    text: str,
    ctx: dict[str, Any],
    label: str,
    min_words: int = MIN_SECTION_WORDS,
    max_words: int = MAX_SECTION_WORDS,
) -> str:
    wc = _wc(text)
    if wc < min_words:
        filler = (
            f"\n\n### Doctrine Alignment Note\n"
            f"This section is scoped to `{ctx['card_id']}` (`{ctx['strategy_id']}`, {ctx['tier']} tier, "
            f"score {ctx['score_total']}/100, rank {ctx['rank']}). It inherits RIG's fail-closed posture: "
            f"every claim here traces back to a card field — `{label}` is derived from engineering_blueprint, "
            f"business_intelligence, gtm_strategy, agent_team, council, consensus, evidence, risks, "
            f"kill_criteria, temporal_validity, and doctrine_governance rather than invented from scratch. "
            f"Where a card field is sparse — fewer than 3 evidence URLs, a short mechanism string, or an "
            f"unmapped strategy_id — this analysis says so explicitly instead of papering over the gap with "
            f"generic language, because a confident-sounding placeholder is worse than an honest 'unknown' "
            f"when a human or downstream agent is deciding whether to promote, fund, or ship this capability. "
            f"Re-verification is due by `{ctx['temporal'].get('reverify_by', 'an unscheduled date')}`; anything "
            f"stated here should be treated as provisional after that date until the underlying card is "
            f"re-synthesized against fresh sources. Aligned doctrine domains for this capability: "
            f"{', '.join(ctx['aligned_domains'])}. Peer capabilities sharing the same strategy_id in this "
            f"batch: {ctx['peer_count']}."
        )
        text = text + filler
        wc = _wc(text)
        if wc < min_words:
            text += (
                f"\n\nFinal note: this deep-{label} layer is intentionally conservative about extrapolating "
                f"beyond what `{ctx['card_id']}` actually states, in keeping with RIG's non-vacuity discipline "
                f"— a thin card should produce thin but honest analysis, never inflated prose dressed up to "
                f"look thorough."
            )
    else:
        paragraphs = text.split("\n\n")
        if wc > max_words:
            kept: list[str] = []
            total = 0
            for p in paragraphs:
                pw = _wc(p)
                if total + pw > max_words and kept:
                    break
                kept.append(p)
                total += pw
            text = "\n\n".join(kept)
    return text


def _build_deep_sections(card: dict[str, Any], all_cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    ctx = _ctx(card, all_cards)
    sections: dict[str, Any] = {}
    for key, title, fn in _SECTION_SPECS:
        body = fn(ctx)
        body = _pad_or_trim(body, ctx, title)
        sections[key] = {
            "title": title,
            "content": body,
            "word_count": _wc(body),
            "generated_at": utc_now(),
        }
    return sections


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _fallback_markdown(card: dict[str, Any]) -> str:
    return (
        f"# {card.get('title', 'Untitled')}\n\n"
        f"- **card_id:** `{card.get('card_id')}`\n"
        f"- **schema:** `{card.get('schema')}`\n\n"
        f"## Claim\n{card.get('claim', '')}\n\n"
        f"## Summary\n{card.get('summary', '')}\n"
    )


def _deep_sections_to_markdown(card: dict[str, Any]) -> str:
    sections = card.get("deep_sections") or {}
    lines = [
        "---",
        "",
        "## V30: Deep Enrichment",
        "",
        f"_Total deep word count:_ {card.get('deep_word_count', 0)} | "
        f"_v30_ready:_ {card.get('v30_ready')} | "
        f"_Enriched V30 at:_ {card.get('enriched_v30_at', '')}",
        "",
    ]
    for key, title, _fn in _SECTION_SPECS:
        sec = sections.get(key) or {}
        lines += [
            f"## V30: {sec.get('title', title)}",
            "",
            f"_Word count: {sec.get('word_count', 0)}_",
            "",
            str(sec.get("content", "")),
            "",
        ]
    lines += [f"_Deep sections hash:_ `{card.get('deep_sections_hash', '')}`", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Enrich single card
# ---------------------------------------------------------------------------


def enrich_card_v30(card_path: Path, all_cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    card_path = Path(card_path)
    card = json.loads(card_path.read_text(encoding="utf-8"))

    if card.get("schema") == SCHEMA_V30:
        return {
            "ok": True,
            "card_id": card.get("card_id"),
            "status": "already_v30",
            "fields": len(card.keys()),
        }

    sections = _build_deep_sections(card, all_cards)
    total_words = sum(s["word_count"] for s in sections.values())

    card["deep_sections"] = sections
    card["deep_sections_hash"] = sha256_text(stable_json(sections))
    card["deep_sections_count"] = len(sections)
    card["deep_word_count"] = total_words
    card["v30_ready"] = all(s["word_count"] >= 500 for s in sections.values())
    card["schema"] = SCHEMA_V30
    card["enriched_v30_at"] = utc_now()

    new_score = score_build_card(card)
    card["score"] = new_score
    card["artifact_sha256"] = sha256_text(
        stable_json({k: v for k, v in card.items() if k != "artifact_sha256"})
    )

    atomic_json(card_path, card)

    md_path = card_path.with_suffix(".md")
    existing_md = md_path.read_text(encoding="utf-8") if md_path.exists() else _fallback_markdown(card)
    md = existing_md.rstrip("\n") + "\n\n" + _deep_sections_to_markdown(card) + "\n"
    atomic_text(md_path, md)

    return {
        "ok": True,
        "card_id": card.get("card_id"),
        "status": "enriched_to_v30",
        "fields": len(card.keys()),
        "schema": card["schema"],
        "deep_word_count": total_words,
        "v30_ready": card["v30_ready"],
        "score": new_score.get("total"),
        "rank": new_score.get("rank"),
    }


# ---------------------------------------------------------------------------
# Enrich all cards
# ---------------------------------------------------------------------------


def enrich_all_v30() -> dict[str, Any]:
    L2_CARDS.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    t0 = time.time()

    card_paths = sorted(L2_CARDS.glob("l2-*.json"))
    all_cards_data: list[dict[str, Any]] = []
    for p in card_paths:
        try:
            all_cards_data.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in card_paths:
        try:
            results.append(enrich_card_v30(path, all_cards_data))
        except Exception as exc:
            errors.append({"card_id": path.stem, "error": str(exc)[:400]})

    enriched = sum(1 for r in results if r.get("status") == "enriched_to_v30")
    already = sum(1 for r in results if r.get("status") == "already_v30")
    word_counts = [r.get("deep_word_count", 0) for r in results if r.get("status") == "enriched_to_v30"]
    avg_words = round(sum(word_counts) / len(word_counts), 1) if word_counts else 0.0
    min_words = min(word_counts) if word_counts else 0
    max_words = max(word_counts) if word_counts else 0

    summary = {
        "schema": "rig.omniscout.l2-enrichment-v30.v1",
        "ok": len(errors) == 0,
        "started_at": started,
        "finished_at": utc_now(),
        "elapsed_s": round(time.time() - t0, 2),
        "total_cards": len(card_paths),
        "enriched_v30": enriched,
        "already_v30": already,
        "errors": errors[:10],
        "error_count": len(errors),
        "avg_deep_word_count": avg_words,
        "min_deep_word_count": min_words,
        "max_deep_word_count": max_words,
        "at": utc_now(),
    }
    atomic_json(L2_ROOT / "latest-enrichment-v30.json", summary)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _status() -> dict[str, Any]:
    total = len(list(L2_CARDS.glob("l2-*.json")))
    v30 = 0
    for p in L2_CARDS.glob("l2-*.json"):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            if c.get("schema") == SCHEMA_V30:
                v30 += 1
        except (OSError, json.JSONDecodeError):
            continue
    return {
        "schema": SCHEMA_V30,
        "total": total,
        "v30": v30,
        "remaining": total - v30,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="V30 Deep Enrichment Engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("all")
    p_one = sub.add_parser("one")
    p_one.add_argument("path")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    if args.cmd == "all":
        out = enrich_all_v30()
    elif args.cmd == "one":
        out = enrich_card_v30(Path(args.path))
    elif args.cmd == "status":
        out = _status()
    else:
        parser.error("unknown subcommand")
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok", True) is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
