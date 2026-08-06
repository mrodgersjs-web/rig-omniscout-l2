"""OmniScout L2 TAC v2 closed-loop upgrader.

Reads V20 build cards and upgrades their autobuilt projects to follow
TAC v2 patterns: Core Four (Context, Model, Prompt, Tools), 8 Tactics,
and Builder -> Verifier -> loop -> escalate -> seal.
"""

from __future__ import annotations

import argparse
import json
import py_compile
from pathlib import Path
from typing import Any

from rig_foundry.omniscout_build_cards import (
    L2_CARDS,
    L2_ROOT,
    atomic_json,
    atomic_text,
    sha256_text,
    stable_json,
    utc_now,
)

SCHEMA = "rig.omniscout.tac-v2.v1"

BUILT_ROOT = L2_ROOT / "built"

TAC_CORE_FOUR = ["Context", "Model", "Prompt", "Tools"]

TAC_TACTICS = [
    "Hello Agentic",
    "12 Leverage Points",
    "Success is Planned",
    "AFK Agents (PITER)",
    "Close The Loops",
    "Let Agents Focus",
    "ZTE",
    "Agentic Layer",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _source_card_path(card_id: str) -> Path:
    return L2_CARDS / f"{card_id}.json"


def _project_dir(card_id: str) -> Path:
    return BUILT_ROOT / card_id


def _as_text(value: Any) -> str:
    """Normalize a card field to a string (handles str/list/dict/None)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_as_text(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return str(value)


def _escape_md(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _yaml_block(value: str) -> str:
    """Return a YAML literal block scalar (|) for multi-line text."""
    if not value:
        return '"""\n(none)\n"""'
    lines = value.splitlines()
    indented = "\n".join("  " + line for line in lines)
    return f"|\n{indented}"


# ---------------------------------------------------------------------------
# File generators
# ---------------------------------------------------------------------------


def _claude_md(card: dict[str, Any]) -> str:
    claim = _as_text(card.get("claim", ""))
    mechanism = _as_text(card.get("mechanism", ""))
    pattern = card.get("pattern", {}) or {}
    idea = card.get("idea", {}) or {}
    done_test = _as_text(idea.get("done_test", ""))
    blueprint = card.get("engineering_blueprint", {}) or {}
    steps = blueprint.get("implementation_steps", [])
    agent_team = card.get("agent_team", {}) or {}
    agents = agent_team.get("agents", [])
    doctrine = card.get("doctrine_governance", {}) or {}
    gates = doctrine.get("gates", [])
    card_id = card.get("card_id", "")
    harness = blueprint.get("harness", {}) or {}

    def _step_line(step: dict[str, Any]) -> str:
        num = step.get("step", 0)
        action = _as_text(step.get("action", ""))
        gate = _as_text(step.get("gate", ""))
        return f"{num}. [{gate}] {action}"

    plan_lines = "\n".join(_step_line(s) for s in steps) or "1. (no plan steps provided)"

    team_lines = "\n".join(
        f"- **{_as_text(a.get('role', 'Agent'))}**: model={_as_text(a.get('model', '?'))}, identity={_as_text(a.get('identity', '?'))}"
        for a in agents
    ) or "- (no team specified)"

    gate_lines = "\n".join(
        f"- **{_as_text(g.get('name', 'Gate'))}**: {_as_text(g.get('requirement', ''))} (applies={g.get('applies', True)})"
        for g in gates
    ) or "- (no governance gates specified)"

    tactics_lines = "\n".join(f"- {t}" for t in TAC_TACTICS)

    parts = [
        f"# CLAUDE.md — TAC v2 Context for {card_id}",
        "",
        "## Mission",
        "",
        "This project was autobuilt from an OmniScout V20 build card and upgraded to",
        "follow TAC v2 closed-loop patterns.",
        "",
        "## Claim",
        "",
        _escape_md(claim) or "(none)",
        "",
        "## Mechanism",
        "",
        _escape_md(mechanism) or "(none)",
        "",
        "## Pattern",
        "",
        f"- **Name**: {_as_text(pattern.get('name', '(none)'))}",
        f"- **Description**: {_as_text(pattern.get('description', '(none)'))}",
        f"- **When to use**: {_as_text(pattern.get('when_to_use', '(none)'))}",
        f"- **When not to use**: {_as_text(pattern.get('when_not_to_use', '(none)'))}",
        "",
        "## Acceptance Criterion (done_test)",
        "",
        "```bash",
        _escape_md(done_test) or "(none)",
        "```",
        "",
        "## Implementation Plan",
        "",
        plan_lines,
        "",
        "## Agent Team Composition",
        "",
        team_lines,
        "",
        "## Quality Contract (doctrine_governance gates)",
        "",
        gate_lines,
        "",
        "## TAC v2 Core Four",
        "",
        "- **Context**: this file + CARD.json",
        f"- **Model**: builder identity = `{_as_text(harness.get('builder', '?'))}`",
        "- **Prompt**: PITER.yaml Problem + Instruction",
        "- **Tools**: harness.py, gates.py, proof.py, tac_loop.py",
        "",
        "## TAC v2 8 Tactics Applied",
        "",
        tactics_lines,
        "",
    ]
    return "\n".join(parts) + "\n"


def _tac_loop_py(card: dict[str, Any]) -> str:
    blueprint = card.get("engineering_blueprint", {}) or {}
    harness = blueprint.get("harness", {}) or {}
    timeout = int(harness.get("timeout_s", 3600))
    card_id = card.get("card_id", "")
    return f'''"""TAC v2 closed-loop runner for {card_id}.

Build -> Verify -> loop until passing -> escalate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from harness import build_artifact
from gates import run_done_test
from proof import seal_proof_packet, write_proof_packet

ROOT = Path(__file__).resolve().parent
CARD_PATH = ROOT / "CARD.json"


def load_card() -> dict[str, Any]:
    return json.loads(CARD_PATH.read_text())


def write_escalation(card: dict[str, Any], attempts: int, last_output: str) -> Path:
    path = ROOT / "escalation.json"
    payload = {{
        "schema": "rig.omniscout.tac-v2-escalation.v1",
        "card_id": card.get("card_id", ""),
        "escalated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "attempts": attempts,
        "max_retries": 3,
        "last_output": last_output,
        "reason": "Verifier failed after max retries",
    }}
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\\n")
    return path


def run(max_retries: int = 3, timeout_s: int = {timeout}) -> int:
    print(f"[tac_loop] starting {{CARD_PATH.name}} (timeout={{timeout_s}}s)")
    card = load_card()
    artifact = build_artifact(card)

    last_output = ""
    for attempt in range(1, max_retries + 1):
        print(f"[tac_loop] BUILD phase attempt {{attempt}}/{{max_retries}}")
        # Adjust approach on retry by annotating artifact with retry context.
        if attempt > 1:
            artifact["retry_approach_adjustment"] = f"retry-{{attempt}}: tighten scope, re-verify assumptions"

        print("[tac_loop] VERIFY phase: independent verifier runs done_test")
        done_ok, last_output = run_done_test(CARD_PATH)
        print(f"[tac_loop] done_test ok={{done_ok}}")

        # Seal a proof packet for every attempt so history is preserved.
        packet = seal_proof_packet(
            card,
            artifact,
            {{"done_test_ok": done_ok, "attempt": attempt}},
            done_ok,
            True,
        )
        write_proof_packet(ROOT, packet)
        print(f"[tac_loop] proof sealed: {{packet['proof_hash'][:16]}}")

        if done_ok:
            print("[tac_loop] SEAL phase: verifier passed; loop complete")
            return 0

        print(f"[tac_loop] LOOP phase: verify failed, retrying\\n{{last_output[:500]}}")

    print("[tac_loop] ESCALATE phase: verifier failed after max retries")
    write_escalation(card, max_retries, last_output)
    return 1


def main(argv: list[str] | None = None) -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _piter_yaml(card: dict[str, Any]) -> str:
    claim = _as_text(card.get("claim", ""))
    blueprint = card.get("engineering_blueprint", {}) or {}
    steps = blueprint.get("implementation_steps", [])
    harness = blueprint.get("harness", {}) or {}
    idea = card.get("idea", {}) or {}
    done_test = _as_text(idea.get("done_test", ""))
    harness_verifier = _as_text(harness.get("verifier", "deterministic scorer + GEV separate identity"))

    def _step_line(step: dict[str, Any]) -> str:
        num = step.get("step", 0)
        action = _as_text(step.get("action", ""))
        gate = _as_text(step.get("gate", ""))
        return f"  {num}. [{gate}] {action}"

    plan_lines = "\n".join(_step_line(s) for s in steps) or "  - (no plan steps provided)"
    claim_block = _yaml_block(claim)

    parts = [
        f"schema: rig.omniscout.tac-v2-piter.v1",
        f"card_id: {card.get('card_id', '')}",
        "",
        "# PITER: Problem -> Instruction -> Template -> Execution -> Review",
        "",
        "Problem:",
        claim_block,
        "",
        "Instruction:",
        plan_lines,
        "",
        "Template:",
        f"  type: {_as_text(harness.get('type', 'TAC closed-loop'))}",
        f"  builder: {_as_text(harness.get('builder', 'rig-agent'))}",
        f"  verifier: {harness_verifier}",
        f"  loop: {_as_text(harness.get('loop', 'build -> verify -> seal'))}",
        f"  retry_policy: {_as_text(harness.get('retry_policy', 'exponential backoff, max 3, fail-closed'))}",
        "",
        "Execution:",
        "  file: tac_loop.py",
        "  phases:",
        "    - BUILD: calls harness.build_artifact()",
        "    - VERIFY: calls gates.run_done_test() with independent verifier identity",
        "    - LOOP: retry up to 3 times with adjusted approach if verify fails",
        "    - ESCALATE: write escalation.json and exit non-zero after 3 failures",
        "    - SEAL: write proof_packet.json when verify passes",
        "",
        "Review:",
        "  done_test: |",
    ]
    for line in done_test.splitlines() or ["(none)"]:
        parts.append(f"    {line}")
    parts.extend([
        f"  verifier: {harness_verifier}",
        "  gev_check: builder identity must differ from verifier identity",
        "",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def apply_tac(card_path: str | Path) -> dict[str, Any]:
    """Upgrade an autobuilt project for the given V20 card to TAC v2 patterns.

    Creates CLAUDE.md, tac_loop.py, and PITER.yaml inside the project's
    built/{card_id}/ directory, then updates the card JSON with a `tac_v2`
    metadata block.
    """
    card_path = Path(card_path).resolve()
    card = _load_json(card_path)

    if card.get("schema") not in (
        "rig.omniscout.build-card.v20",
        "rig.omniscout.build-card.v21-autobuilt",
    ):
        raise ValueError(f"Unsupported card schema: {card.get('schema')}")

    card_id = card["card_id"]
    source_path = _source_card_path(card_id)
    project_dir = _project_dir(card_id)

    if not project_dir.exists():
        raise FileNotFoundError(f"Autobuilt project missing: {project_dir}")

    claude_md = _claude_md(card)
    tac_loop = _tac_loop_py(card)
    piter_yaml = _piter_yaml(card)

    atomic_text(project_dir / "CLAUDE.md", claude_md)
    atomic_text(project_dir / "tac_loop.py", tac_loop)
    atomic_text(project_dir / "PITER.yaml", piter_yaml)

    # Ensure generated Python compiles.
    py_compile.compile(project_dir / "tac_loop.py", doraise=True)

    # Update the card in the project directory (CARD.json copy).
    project_card = project_dir / "CARD.json"
    if project_card.exists():
        project_card_data = _load_json(project_card)
        project_card_data["tac_v2"] = _build_tac_v2_meta(card, project_dir)
        atomic_json(project_card, project_card_data)

    # Update the source card if it exists.
    target_card = source_path if source_path.exists() else card_path
    source_data = _load_json(target_card)
    tac_meta = _build_tac_v2_meta(source_data, project_dir)
    source_data["tac_v2"] = tac_meta
    atomic_json(target_card, source_data)

    return {
        "schema": SCHEMA,
        "card_id": card_id,
        "project_dir": str(project_dir),
        "files_generated": ["CLAUDE.md", "tac_loop.py", "PITER.yaml"],
        "card_updated": str(target_card),
        "tac_v2": tac_meta,
    }


def _build_tac_v2_meta(card: dict[str, Any], project_dir: Path) -> dict[str, Any]:
    blueprint = card.get("engineering_blueprint", {}) or {}
    harness = blueprint.get("harness", {}) or {}
    idea = card.get("idea", {}) or {}
    return {
        "applied": True,
        "applied_at": utc_now(),
        "core_four": dict(zip(TAC_CORE_FOUR, [
            "CLAUDE.md + CARD.json",
            _as_text(harness.get("builder", "rig-agent")),
            "PITER.yaml Problem + Instruction",
            "harness.py, gates.py, proof.py, tac_loop.py",
        ])),
        "tactics_applied": TAC_TACTICS,
        "closed_loop": True,
        "piter_path": str(project_dir / "PITER.yaml"),
        "tac_loop_path": str(project_dir / "tac_loop.py"),
        "claude_md_path": str(project_dir / "CLAUDE.md"),
        "done_test": _as_text(idea.get("done_test", "")),
        "builder": _as_text(harness.get("builder", "rig-agent")),
        "verifier": _as_text(harness.get("verifier", "deterministic verifier")),
    }


def apply_all() -> dict[str, Any]:
    """Apply TAC v2 upgrade to every autobuilt project under L2_ROOT/built/."""
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if not BUILT_ROOT.exists():
        return {
            "schema": SCHEMA,
            "applied": results,
            "errors": errors,
            "applied_count": 0,
            "error_count": 0,
        }

    for project_dir in sorted(BUILT_ROOT.iterdir()):
        if not project_dir.is_dir():
            continue
        card_path = _source_card_path(project_dir.name)
        if not card_path.exists():
            card_path = project_dir / "CARD.json"
        if not card_path.exists():
            continue
        try:
            results.append(apply_tac(card_path))
        except Exception as exc:
            errors.append({"card_id": project_dir.name, "error": str(exc)})

    return {
        "schema": SCHEMA,
        "applied": results,
        "errors": errors,
        "applied_count": len(results),
        "error_count": len(errors),
    }


def _status() -> dict[str, Any]:
    cards = list(L2_CARDS.glob("l2-*.json"))
    built = [p for p in cards if (p.parent.parent / "built" / p.stem).exists()]
    tac_applied = [p for p in cards if _load_json(p).get("tac_v2", {}).get("applied")]
    return {
        "schema": SCHEMA,
        "total_cards": len(cards),
        "built_projects": len(built),
        "tac_applied": len(tac_applied),
        "built_root": str(BUILT_ROOT),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniScout L2 TAC v2 closed-loop upgrader")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("one", help="Apply TAC v2 to a single card by path")
    one.add_argument("path", help="Path to a V20 build-card JSON file")
    sub.add_parser("all", help="Apply TAC v2 to all autobuilt projects")
    sub.add_parser("status", help="Show TAC v2 application status")

    args = parser.parse_args(argv)

    if args.command == "one":
        result = apply_tac(args.path)
        print(stable_json(result))
        return 0

    if args.command == "all":
        result = apply_all()
        print(stable_json(result))
        return 1 if result["error_count"] else 0

    if args.command == "status":
        print(stable_json(_status()))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
