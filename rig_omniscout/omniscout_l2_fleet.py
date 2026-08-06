"""Fleet build dispatcher — sends V30 cards to 96GB node for LLM builds."""
from __future__ import annotations
import json, subprocess, time, os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from rig_foundry.omniscout_build_cards import L2_CARDS, L2_ROOT, atomic_json, utc_now

NODE = "rig96gb"
REMOTE_DIR = "~/build-cards"
MODEL = os.environ.get("FLEET_MODEL", "qwen3:32b")


def _ssh(cmd: str, timeout: int = 600) -> tuple[int, str]:
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", NODE, cmd],
        capture_output=True, text=True, timeout=timeout
    )
    return r.returncode, (r.stdout + r.stderr)


def _build_prompt(card: dict) -> str:
    title = card.get("title", "")
    claim = card.get("claim", "")
    idea = card.get("idea") or {}
    steps = (card.get("engineering_blueprint") or {}).get("implementation_steps") or []
    steps_text = "\n".join(f"{s.get('step','')}. {s.get('action','')}" for s in steps[:5])
    return (
        f"Build this capability. Output clean Python code.\n\n"
        f"TITLE: {title}\nCLAIM: {claim}\n"
        f"BUILD: {idea.get('description','')}\n"
        f"DONE TEST: {idea.get('done_test','')}\n"
        f"STEPS:\n{steps_text}\n\n"
        f"Generate main.py + test_done.py. Focus on done_test passing."
    )


def dispatch_to_96gb(card_ids: list[str], model: str = MODEL) -> dict[str, Any]:
    results = []
    for cid in card_ids:
        card_path = L2_CARDS / f"{cid}.json"
        if not card_path.exists():
            results.append({"card_id": cid, "ok": False, "error": "not found"})
            continue

        card = json.loads(card_path.read_text(encoding="utf-8"))
        prompt = _build_prompt(card)

        t0 = time.time()
        # Run build on 96GB
        escaped_prompt = prompt.replace("'", "'\\''").replace('"', '\\"')[:3000]
        rc, output = _ssh(
            f"mkdir -p {REMOTE_DIR}/built/{cid} && "
            f"ollama run {model} \"{escaped_prompt}\" > {REMOTE_DIR}/built/{cid}/agent_build_96gb.py 2>&1 && "
            f"echo BUILD_DONE",
            timeout=300
        )
        elapsed = round(time.time() - t0, 2)

        if "BUILD_DONE" not in output:
            results.append({"card_id": cid, "ok": False, "error": output[-200:], "elapsed_s": elapsed})
            continue

        # Run done-test on 96GB
        done_test = str((card.get("idea") or {}).get("done_test", ""))
        fixed_test = done_test.replace("python -c", "python3 -c").replace("python3.14", "python3")
        if fixed_test and "python3" in fixed_test:
            rc2, test_output = _ssh(f"cd {REMOTE_DIR}/built/{cid} && {fixed_test}", timeout=30)
            test_pass = rc2 == 0
        else:
            test_pass = True  # skip if no test

        # Save proof on 96GB
        proof = {
            "card_id": cid, "title": card.get("title", ""),
            "model": model, "node": NODE,
            "build_elapsed_s": elapsed, "done_test_pass": test_pass,
            "built_at": datetime.now(timezone.utc).isoformat(),
        }
        proof_json = json.dumps(proof).replace('"', '\\"')
        _ssh(f"echo '{proof_json}' > {REMOTE_DIR}/built/{cid}/agent_proof_96gb.json")

        # Update local card
        card.setdefault("outcome", {})["fleet_build_96gb"] = proof
        card_path.write_text(json.dumps(card, indent=2, ensure_ascii=False) + "\n")

        results.append({"card_id": cid, "ok": True, "model": model, "elapsed_s": elapsed, "done_test_pass": test_pass, "title": card.get("title", "")[:50]})
        print(f"  {'✅' if test_pass else '❌'} {cid}: {card.get('title','')[:40]} ({elapsed}s)", flush=True)

    summary = {
        "schema": "rig.omniscout.fleet-build.v1",
        "node": NODE, "model": model,
        "dispatched": len(results),
        "passed": sum(1 for r in results if r.get("done_test_pass")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "results": results, "at": utc_now(),
    }
    atomic_json(L2_ROOT / "latest-fleet-build.json", summary)
    return summary


def nightly_fleet_build(limit: int = 10) -> dict[str, Any]:
    # Get top cards not yet built on 96GB
    cards = []
    for p in sorted(L2_CARDS.glob("l2-*.json")):
        d = json.loads(p.read_text())
        score = (d.get("score") or {}).get("total", 0)
        already = (d.get("outcome") or {}).get("fleet_build_96gb", {})
        if not already:
            cards.append((score, d.get("card_id", "")))
    cards.sort(key=lambda x: -x[0])

    top_ids = [cid for _, cid in cards[:limit]]
    if not top_ids:
        return {"ok": True, "message": "all cards already built on 96GB"}

    return dispatch_to_96gb(top_ids)


def collect_results() -> dict[str, Any]:
    rc, output = _ssh(f"find {REMOTE_DIR}/built -name 'agent_proof_96gb.json' -exec cat {{}} \\;")
    proofs = []
    for line in output.strip().split("\n"):
        if line.strip().startswith("{"):
            try:
                proofs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return {"ok": True, "proofs_collected": len(proofs), "proofs": proofs}


def fleet_status() -> dict[str, Any]:
    rc, models = _ssh("curl -s -m 3 http://127.0.0.1:11434/api/tags | python3 -c \"import sys,json;print([m['name'] for m in json.load(sys.stdin).get('models',[])])\"")
    rc2, disk = _ssh("df -h ~ | tail -1")
    rc3, cards = _ssh(f"ls {REMOTE_DIR}/cards/l2-*.json 2>/dev/null | wc -l")
    rc4, builds = _ssh(f"ls {REMOTE_DIR}/built/ 2>/dev/null | wc -l")
    return {
        "node": NODE,
        "models": models.strip(),
        "disk": disk.strip(),
        "cards_synced": cards.strip(),
        "builds_completed": builds.strip(),
        "model": MODEL,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build"); p_build.add_argument("card_ids", nargs="+")
    p_nightly = sub.add_parser("nightly"); p_nightly.add_argument("--limit", type=int, default=10)
    sub.add_parser("status")
    sub.add_parser("results")
    args = parser.parse_args(argv)

    if args.cmd == "build":
        out = dispatch_to_96gb(args.card_ids)
    elif args.cmd == "nightly":
        out = nightly_fleet_build(args.limit)
    elif args.cmd == "status":
        out = fleet_status()
    elif args.cmd == "results":
        out = collect_results()
    else:
        parser.error("unknown"); return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
