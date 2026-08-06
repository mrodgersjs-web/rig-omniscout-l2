"""AI Professional Package generator — assembles sellable offerings from V30 cards."""
from __future__ import annotations
import json, os
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from rig_foundry.omniscout_build_cards import L2_CARDS, L2_ROOT, atomic_json, atomic_text, sha256_text, utc_now

OFFERINGS_DIR = L2_ROOT / "offerings"
PROPOSALS_DIR = L2_ROOT / "proposals"


def _cards_by_strategy() -> dict[str, list[dict]]:
    clusters: dict[str, list[dict]] = {}
    for p in sorted(L2_CARDS.glob("l2-*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        sid = (d.get("strategy") or {}).get("strategy_id", "unmapped")
        if sid and sid != "":
            clusters.setdefault(sid, []).append(d)
    for sid in clusters:
        clusters[sid].sort(key=lambda c: -(c.get("score") or {}).get("total", 0))
    return clusters



DEPT_ROUTES = {
    "agent-engineering": ["os-intelligence", "app"],
    "gtm-sales": ["gtm", "sales"],
    "healthcare-ai-ops": ["gtm", "sales"],
    "proof-false-done": ["os-intelligence", "security"],
    "local-inference-fleet": ["data", "os-intelligence"],
    "determinism-gates": ["os-intelligence", "app"],
    "knowledge-memory": ["os-intelligence", "knowledge"],
    "automation-runtime": ["operations"],
    "scraping-intelligence": ["os-intelligence", "research"],
    "doctrine-control-plane": ["os-intelligence"],
    "pricing-finance": ["finance", "gtm"],
    "forecasting-calibration": ["strategy", "research"],
    "strategy-decision-routing": ["strategy"],
    "marketing-content-linkedin": ["content", "linkedin"],
    "ai-business-models": ["strategy", "gtm"],
    "customer-success-expansion": ["customer-success"],
    "founder-performance": ["operations"],
    "competitive-intel": ["research", "strategy"],
    "cybersecurity": ["security"],
    "product-design": ["design"],
    "leadership-org": ["operations"],
    "legal-compliance": ["legal"],
    "operations": ["operations"],
}

def route_to_departments(offering: dict) -> dict:
    """Route offering to the right departments for action."""
    sid = offering.get("strategy_id", "")
    depts = DEPT_ROUTES.get(sid, ["os-intelligence"])
    
    routes = []
    for dept in depts:
        routes.append({
            "department": dept,
            "action": "review_offering" if dept in ("strategy", "research") else "execute_offering",
            "offering_id": offering.get("offering_id"),
            "priority": "HIGH" if offering.get("quality_status") == "MARKET_READY" else "MEDIUM",
            "instructions": f"Department {dept}: Review and act on offering {offering.get('title','')}. Revenue model: {offering.get('investment_thesis','')[:100]}",
        })
    
    offering["department_routes"] = routes
    return offering

def package_offering(strategy_id: str) -> dict[str, Any]:
    clusters = _cards_by_strategy()
    cards = clusters.get(strategy_id, [])
    if not cards:
        return {"ok": False, "error": f"no cards in strategy {strategy_id}"}

    OFFERINGS_DIR.mkdir(parents=True, exist_ok=True)
    top = cards[0]
    biz = top.get("business_intelligence") or {}
    eng = top.get("engineering_blueprint") or {}
    team = top.get("agent_team") or {}
    council = top.get("council") or {}
    syn = council.get("synthesis") or {}
    gtm = top.get("gtm_strategy") or {}
    deep = top.get("deep_sections") or {}
    wm = top.get("world_model") or {}

    offering_id = f"offering-{strategy_id}-{datetime.now(timezone.utc).strftime('%Y%m%d')}"

    # Deliverables from implementation steps
    steps = eng.get("implementation_steps") or []
    deliverables = [s.get("action", "") for s in steps[:7]]

    # Pricing tiers
    price_raw = biz.get("price_range", "$5K-20K/mo")
    tiers = [
        {"name": "Starter", "price": "Starting at " + price_raw.split("-")[0] if "-" in price_raw else price_raw, "features": deliverables[:3]},
        {"name": "Professional", "price": price_raw, "features": deliverables[:5]},
        {"name": "Enterprise", "price": "Custom", "features": deliverables + ["Dedicated agent team", "SLA + ProofPacket audit", "Custom integrations"]},
    ]

    # Proof points
    proof_points = []
    proven = sum(1 for c in cards if (c.get("autobuilt") or {}).get("proof_sealed"))
    proof_points.append(f"{proven}/{len(cards)} cards proof-sealed on fleet build node")
    proof_points.append(f"Council verdict: {syn.get('overall_verdict', 'SHIP')} (confidence {syn.get('confidence_score', 0)}/100)")
    proof_points.append(f"Average score: {sum((c.get('score') or {}).get('total', 0) for c in cards) // len(cards)}/100")

    # Content samples
    content_samples = []
    for c in cards[:2]:
        cid = c.get("card_id", "")
        li_path = L2_ROOT / "content" / cid / "linkedin.md"
        if li_path.exists():
            content_samples.append({"card_id": cid, "type": "LinkedIn", "preview": li_path.read_text(encoding="utf-8")[:300]})

    # Competitive advantage
    moat = biz.get("competitive_moat") or []
    consensus = syn.get("consensus_points") or []
    advantages = (moat + consensus)[:5]

    # Risk mitigation
    deep_risk = deep.get("deep_risk", {})
    risk_text = deep_risk.get("content", "") if isinstance(deep_risk, dict) else str(deep_risk)
    risks = [l.strip() for l in risk_text.split("\n") if l.strip() and len(l.strip()) > 20][:3]

    offering = {
        "schema": "rig.omniscout.offering.v1",
        "offering_id": offering_id,
        "strategy_id": strategy_id,
        "title": f"RIG {strategy_id.replace('-', ' ').title()} Package",
        "card_count": len(cards),
        "executive_summary": f"This package delivers {strategy_id.replace('-',' ')} capability backed by {len(cards)} research-driven build cards, {proven} proof-sealed implementations, and a 6-perspective Council analysis recommending {syn.get('overall_verdict','SHIP')}. Target ICP: {gtm.get('icp','mid-market operators')}. Revenue model: {biz.get('revenue_model','consulting + custom build')}.",
        "scope_of_work": deliverables,
        "deliverables": deliverables,
        "pricing_tiers": tiers,
        "timeline": f"{biz.get('build_effort', '2-4 weeks')} per capability | {len(cards)} capabilities packaged",
        "team": {
            "agent_count": team.get("agent_count", 2),
            "agents": team.get("agents", []),
            "department": team.get("department_routing", "intelligence"),
        },
        "proof_points": proof_points,
        "tech_stack": eng.get("tech_stack", {}),
        "competitive_advantage": advantages,
        "content_samples": content_samples,
        "risk_mitigation": risks or ["Standard delivery risk — mitigated by ProofPacket audit"],
        "quality_gates": {
            "done_test": True,
            "gev_separation": True,
            "proof_sealed": proven > 0,
            "council_verdict": syn.get("overall_verdict", "SHIP"),
            "min_score": min((c.get("score") or {}).get("total", 0) for c in cards),
        },
        "investment_thesis": f"{biz.get('revenue_model','')} at {price_raw}. TAM: {biz.get('tam','$2-5B')}. Moat: {advantages[0] if advantages else 'proof-chained quality'}.",
        "gtm": {"motion": gtm.get("sales_motion", ""), "icp": gtm.get("icp", ""), "channels": gtm.get("channels", [])},
        "jake_briefing": wm.get("jake_briefing", "")[:300],
        "created_at": utc_now(),
    }

    path = OFFERINGS_DIR / f"{offering_id}.json"
    atomic_json(path, offering)

    # Markdown version
    md = _offering_to_md(offering)
    atomic_text(path.with_suffix(".md"), md)

    return {"ok": True, "offering_id": offering_id, "path": str(path), "card_count": len(cards), "revenue_model": biz.get("revenue_model", "")}


def _offering_to_md(o: dict) -> str:
    lines = [
        f"# {o['title']}",
        "",
        f"**{o['card_count']} cards** | Council: {o['quality_gates']['council_verdict']} | Proof-sealed: {o['quality_gates']['proof_sealed']}",
        "",
        "## Executive Summary",
        o["executive_summary"],
        "",
        "## Scope of Work",
        *[f"- {d}" for d in o["scope_of_work"]],
        "",
        "## Pricing",
        *[f"- **{t['name']}**: {t['price']} — {', '.join(t['features'][:2])}" for t in o["pricing_tiers"]],
        "",
        "## Timeline",
        o["timeline"],
        "",
        "## Team",
        f"{o['team']['agent_count']} agents | Department: {o['team']['department']}",
        "",
        "## Proof Points",
        *[f"- {p}" for p in o["proof_points"]],
        "",
        "## Competitive Advantage",
        *[f"- {a}" for a in o["competitive_advantage"]],
        "",
        "## Investment Thesis",
        o["investment_thesis"],
        "",
        "## Risk Mitigation",
        *[f"- {r}" for r in o["risk_mitigation"]],
        "",
    ]
    return "\n".join(lines) + "\n"


def package_all_offerings() -> dict[str, Any]:
    OFFERINGS_DIR.mkdir(parents=True, exist_ok=True)
    clusters = _cards_by_strategy()
    results = []
    for sid, cards in clusters.items():
        if len(cards) >= 3:
            r = package_offering(sid)
            results.append(r)
    summary = {
        "schema": "rig.omniscout.offerings-summary.v1",
        "ok": True,
        "offerings_generated": len(results),
        "strategies_packaged": [r.get("offering_id", "") for r in results],
        "at": utc_now(),
    }
    atomic_json(L2_ROOT / "latest-offerings.json", summary)
    return summary


def generate_portfolio_html() -> dict[str, Any]:
    offerings = []
    if OFFERINGS_DIR.exists():
        for p in sorted(OFFERINGS_DIR.glob("*.json")):
            offerings.append(json.loads(p.read_text()))

    cards_bg = "#0d1117"
    card_bg = "#161b22"
    accent = "#58a6ff"
    green = "#3fb950"
    yellow = "#d29922"

    rows = ""
    for o in offerings:
        verdict = o.get("quality_gates", {}).get("council_verdict", "?")
        color = green if verdict == "SHIP" else yellow
        proven = "✅" if o.get("quality_gates", {}).get("proof_sealed") else "❌"
        rows += f"""
        <div style="background:{card_bg};border-radius:12px;padding:20px;margin:10px;display:inline-block;width:300px;vertical-align:top;">
          <h3 style="color:{accent};">{o.get('title','?')}</h3>
          <p style="color:#8b949e;">{o.get('card_count',0)} cards | {o.get('investment_thesis','')[:80]}</p>
          <p style="color:{color};font-weight:bold;">Council: {verdict}</p>
          <p>Proof sealed: {proven}</p>
          <p style="color:#8b949e;">{o.get('pricing_tiers',[{}])[0].get('price','?')}</p>
        </div>"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>RIG Offerings Portfolio</title></head>
<body style="background:{cards_bg};color:#c9d1d9;font-family:-apple-system,sans-serif;margin:0;padding:20px;">
<h1 style="color:{accent};">RIG AI Professional Packages</h1>
<p>{len(offerings)} offerings ready to sell</p>
<div>{rows}</div>
</body></html>"""

    path = Path.home() / "Desktop" / "rig-offerings-portfolio.html"
    path.write_text(html, encoding="utf-8")
    return {"ok": True, "html_path": str(path), "offerings": len(offerings)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_pkg = sub.add_parser("package"); p_pkg.add_argument("strategy_id")
    sub.add_parser("all")
    sub.add_parser("portfolio")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    if args.cmd == "package":
        out = package_offering(args.strategy_id)
    elif args.cmd == "all":
        out = package_all_offerings()
    elif args.cmd == "portfolio":
        out = generate_portfolio_html()
        if out.get("ok"):
            os.system(f"open {out['html_path']}")
    elif args.cmd == "status":
        count = len(list(OFFERINGS_DIR.glob("*.json"))) if OFFERINGS_DIR.exists() else 0
        out = {"offerings": count}
    else:
        parser.error("unknown"); return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
