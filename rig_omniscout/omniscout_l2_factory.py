"""Bridge V20 build cards → Factory/SSSF overnight build pipeline.

Takes top-scored V20 cards and queues them as Factory missions or SSSF ADW
processes so Mike wakes up to new solutions to sell.

Factory: factory mission --goal "<goal>" --target "<repo>"
SSSF: uv run adws/adw_plan_build_test.py "<request>" --adw-id <id>
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from rig_foundry.omniscout_build_cards import (  # noqa: E402
    L2_CARDS, L2_ROOT, atomic_json, atomic_text, sha256_text, stable_json, utc_now,
)

FACTORY_BIN = "/home/operator/.local/bin/factory"
SSSF_ROOT = Path.home() / "Developer" / "super-simple-software-factory"
MISSIONS_DIR = L2_ROOT / "factory-missions"
QUEUE_DIR = L2_ROOT / "factory-queue"


def card_to_mission_goal(card: dict[str, Any]) -> str:
    """Convert a V20 card into a Factory mission goal string."""
    title = card.get("title", "")
    claim = card.get("claim", "")
    idea = card.get("idea") or {}
    eng = card.get("engineering_blueprint") or {}
    biz = card.get("business_intelligence") or {}
    council = card.get("council") or {}
    syn = council.get("synthesis") or {}
    gtm = card.get("gtm_strategy") or {}

    steps = eng.get("implementation_steps") or []
    steps_text = "\n".join(
        f"  {s.get('step', i+1)}. {s.get('action', '')}"
        for i, s in enumerate(steps[:7])
    )

    top_actions = syn.get("top_3_actions") or []

    goal = f"""BUILD THIS CAPABILITY: {title}

CLAIM: {claim}

REVENUE MODEL: {biz.get('revenue_model', 'Consulting + custom build')}
PRICE: {biz.get('price_range', '$5-20K/mo')}
ICP: {gtm.get('icp', 'Mid-market operators')}

IMPLEMENTATION STEPS:
{steps_text}

DONE TEST: {idea.get('done_test', 'Define a test that proves the capability works')}

BUILD SLICE: {idea.get('name', title)}
ACCEPTANCE: {idea.get('acceptance', 'Capability works end-to-end with proof')}

COUNCIL VERDICT: {syn.get('overall_verdict', 'SHIP')} (confidence {syn.get('confidence_score', 0)}/100)
TOP ACTIONS: {'; '.join(top_actions[:3])}

CONSTRAINTS:
- Follow TAC v2 closed-loop: build → verify → seal ProofPacket
- GEV separation: builder ≠ verifier
- Deterministic gates: score ≥ 70, sources ≥ 3, done-test passes
- Ship working code with tests, not scaffolds"""

    return goal


def card_to_sssf_request(card: dict[str, Any]) -> str:
    """Convert V20 card to SSSF plan request."""
    title = card.get("title", "")
    claim = card.get("claim", "")
    idea = card.get("idea") or {}
    eng = card.get("engineering_blueprint") or {}
    stack = eng.get("tech_stack") or {}

    tech = ", ".join(
        f"{k}: {', '.join(v)}" for k, v in stack.items()
    ) if stack else "Python"

    return f"""Build: {title}

What: {claim}

Tech: {tech}

Done test: {idea.get('done_test', 'pytest passes')}

Steps:
{chr(10).join(f'- {s.get("action","")}' for s in (eng.get("implementation_steps") or [])[:5])}

Ship working code with tests."""


def queue_card_to_factory(card_path: Path) -> dict[str, Any]:
    """Queue a single V20 card as a Factory mission."""
    card = json.loads(card_path.read_text(encoding="utf-8"))
    cid = card.get("card_id", card_path.stem)
    goal = card_to_mission_goal(card)

    MISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    # Write mission packet
    mission = {
        "schema": "rig.omniscout.factory-mission.v1",
        "card_id": cid,
        "title": card.get("title", ""),
        "strategy_id": (card.get("strategy") or {}).get("strategy_id", ""),
        "tier": (card.get("strategy") or {}).get("tier", ""),
        "score": (card.get("score") or {}).get("total", 0),
        "rank": (card.get("score") or {}).get("rank", ""),
        "goal": goal,
        "revenue_model": (card.get("business_intelligence") or {}).get("revenue_model", ""),
        "price_range": (card.get("business_intelligence") or {}).get("price_range", ""),
        "council_verdict": (card.get("council", {}).get("synthesis", {})).get("overall_verdict", ""),
        "queued_at": utc_now(),
        "status": "QUEUED",
    }

    mission_path = MISSIONS_DIR / f"{cid}.json"
    atomic_json(mission_path, mission)

    # Write queue entry (for batch runner)
    queue_entry = {
        "card_id": cid,
        "goal": goal,
        "mission_path": str(mission_path),
        "queued_at": utc_now(),
    }
    queue_path = QUEUE_DIR / f"{cid}.json"
    atomic_json(queue_path, queue_entry)

    # Update card with factory mission reference
    card["factory_mission"] = {
        "queued": True,
        "mission_path": str(mission_path),
        "goal_preview": goal[:200],
        "queued_at": utc_now(),
    }
    atomic_json(card_path, card)

    return {"ok": True, "card_id": cid, "mission_path": str(mission_path)}


def queue_top_cards(limit: int = 10) -> dict[str, Any]:
    """Queue the top N V20 cards by score as Factory missions."""
    cards = []
    for p in sorted(L2_CARDS.glob("l2-*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        score = (d.get("score") or {}).get("total", 0)
        # only queue cards that are verified (outcome = BUILT_AND_VERIFIED)
        outcome = (d.get("outcome") or {}).get("status", "")
        if outcome == "BUILT_AND_VERIFIED" or score >= 85:
            cards.append((score, p, d))

    cards.sort(key=lambda x: -x[0])
    queued = []
    for score, path, d in cards[:limit]:
        result = queue_card_to_factory(path)
        result["score"] = score
        result["title"] = d.get("title", "")[:60]
        result["revenue"] = (d.get("business_intelligence") or {}).get("revenue_model", "")
        queued.append(result)

    summary = {
        "schema": "rig.omniscout.factory-queue.v1",
        "ok": True,
        "queued_count": len(queued),
        "missions_dir": str(MISSIONS_DIR),
        "queued_at": utc_now(),
        "missions": queued,
    }
    atomic_json(L2_ROOT / "latest-factory-queue.json", summary)
    return summary


def run_sssf_build(card_id: str, goal: str) -> dict[str, Any]:
    """Run SSSF plan+build+test for one card."""
    if not SSSF_ROOT.exists():
        return {"ok": False, "error": "SSSF not found"}

    try:
        r = subprocess.run(
            ["uv", "run", "adws/adw_plan_build_test.py", goal],
            capture_output=True, text=True, timeout=600,
            cwd=str(SSSF_ROOT),
        )
        return {
            "ok": r.returncode == 0,
            "card_id": card_id,
            "exit_code": r.returncode,
            "stdout_tail": r.stdout[-500:],
            "stderr_tail": r.stderr[-200:],
        }
    except Exception as exc:
        return {"ok": False, "card_id": card_id, "error": str(exc)[:200]}


def run_factory_mission(card_id: str, goal: str, target: str = ".") -> dict[str, Any]:
    """Queue a Factory mission (does not auto-execute — requires Gate-D)."""
    try:
        r = subprocess.run(
            [FACTORY_BIN, "mission", "--target", target, "--goal", goal],
            capture_output=True, text=True, timeout=60,
        )
        return {
            "ok": r.returncode == 0,
            "card_id": card_id,
            "stdout": r.stdout[:500],
            "stderr": r.stderr[:200],
        }
    except Exception as exc:
        return {"ok": False, "card_id": card_id, "error": str(exc)[:200]}


def nightly_build_queue(limit: int = 10) -> dict[str, Any]:
    """Nightly: queue top cards as Factory missions + run SSSF builds."""
    # Queue missions
    queue_result = queue_top_cards(limit=limit)

    # For each queued mission, attempt SSSF build (best effort)
    sssf_results = []
    for mission in queue_result.get("missions", [])[:3]:  # top 3 only for SSSF
        cid = mission["card_id"]
        goal = mission.get("goal", "")
        result = run_sssf_build(cid, goal)
        sssf_results.append(result)
        time.sleep(2)

    summary = {
        "schema": "rig.omniscout.nightly-factory.v1",
        "ok": True,
        "queued": queue_result.get("queued_count", 0),
        "sssf_built": sum(1 for r in sssf_results if r.get("ok")),
        "sssf_results": sssf_results,
        "at": utc_now(),
    }
    atomic_json(L2_ROOT / "latest-nightly-factory.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="V20 → Factory/SSSF build bridge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("queue", help="queue top V20 cards as Factory missions")
    p_queue = sub.add_parser("queue-top", help="queue top N cards")
    p_queue.add_argument("--limit", type=int, default=10)

    p_sssf = sub.add_parser("sssf", help="run SSSF build for one card")
    p_sssf.add_argument("card_id")
    p_sssf.add_argument("--goal-file")

    p_factory = sub.add_parser("factory", help="queue Factory mission for one card")
    p_factory.add_argument("card_id")
    p_factory.add_argument("--target", default=".")

    sub.add_parser("nightly", help="nightly build queue + SSSF builds")
    sub.add_parser("status", help="show queue status")

    args = parser.parse_args(argv)

    if args.cmd in ("queue", "queue-top"):
        limit = getattr(args, "limit", 10)
        out = queue_top_cards(limit=limit)
    elif args.cmd == "sssf":
        card = json.loads((L2_CARDS / f"{args.card_id}.json").read_text())
        goal = card_to_sssf_request(card)
        if args.goal_file:
            goal = Path(args.goal_file).read_text()
        out = run_sssf_build(args.card_id, goal)
    elif args.cmd == "factory":
        card = json.loads((L2_CARDS / f"{args.card_id}.json").read_text())
        goal = card_to_mission_goal(card)
        out = run_factory_mission(args.card_id, goal, args.target)
    elif args.cmd == "nightly":
        out = nightly_build_queue(limit=10)
    elif args.cmd == "status":
        queued = list(QUEUE_DIR.glob("*.json")) if QUEUE_DIR.exists() else []
        missions = list(MISSIONS_DIR.glob("*.json")) if MISSIONS_DIR.exists() else []
        out = {
            "queued": len(queued),
            "missions": len(missions),
            "queue_dir": str(QUEUE_DIR),
            "missions_dir": str(MISSIONS_DIR),
            "factory_installed": Path(FACTORY_BIN).exists(),
            "sssf_installed": SSSF_ROOT.exists(),
        }
    else:
        parser.error("unknown")
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
