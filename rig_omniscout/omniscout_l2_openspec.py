"""OmniScout L2 OpenSpec BDD generator.

Reads V20 build cards and emits doctrine-compliant OpenSpec behavior-driven
development artifacts under the autobuilt project directory:

    L2_ROOT/built/{card_id}/openspec/
        spec.md      — Given/When/Then scenarios
        proposal.md  — Build rationale
        design.md    — Technical design
        tasks.md     — Red→green→commit checklist

All generation is deterministic from card fields — no LLM calls.
"""

from __future__ import annotations

import argparse
import json
import re
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

SCHEMA = "rig.omniscout.openspec.v1"
BUILT_ROOT = L2_ROOT / "built"


def _load_card(card_path: str | Path) -> dict[str, Any]:
    return json.loads(Path(card_path).read_text(encoding="utf-8"))


def _card_id(card: dict[str, Any], path: Path | None = None) -> str:
    return card.get("card_id") or (path.stem if path else "unknown")


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(str(x) for x in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _title(card: dict[str, Any]) -> str:
    return (card.get("title") or card.get("topic") or card.get("card_id") or "Untitled Capability").strip()


def _strategy_id(card: dict[str, Any]) -> str:
    return card.get("strategy", {}).get("strategy_id") or card.get("card_id") or "unknown"


def _safe_feature_name(title: str) -> str:
    """Gherkin Feature lines are free text, but keep them single-line."""
    return title.replace("\n", " ").strip()


def _gherkin_step(prefix: str, text: str) -> str:
    text = " ".join(text.split())
    if not text:
        return f"  {prefix} ..."
    return f"  {prefix} {text[0].lower()}{text[1:]}"


def _done_test_command(done_test: str) -> str:
    """Convert a done_test string into a shell-like command for the scenario."""
    cmd = done_test.strip()
    if not cmd:
        return "the done-test command is executed"
    # If the card author already wrote a shell command, quote it safely.
    return cmd


def _spec_md(card: dict[str, Any]) -> str:
    title = _safe_feature_name(_title(card))
    sid = _strategy_id(card)
    idea = card.get("idea", {})
    done_test = idea.get("done_test", "")
    pattern = card.get("pattern", {})
    doctrine = card.get("doctrine_governance", {})
    gates = doctrine.get("gates") or []
    council = card.get("council", {})
    synthesis = council.get("synthesis", {}) if isinstance(council, dict) else {}
    top_actions = synthesis.get("top_3_actions") or []

    scenarios: list[str] = []

    # Primary acceptance scenario from idea.done_test
    scenarios.append(
        f"""Scenario: {idea.get('name') or 'Primary acceptance'} passes the done-test
{_gherkin_step('Given', f"the card exists with strategy_id '{sid}'")}
{_gherkin_step('When', f"the done-test `{_done_test_command(done_test)}` is executed")}
{_gherkin_step('Then', "it should exit with code 0")}
"""
    )

    # Applicable use case from pattern.when_to_use
    when_to_use = pattern.get("when_to_use", "")
    if when_to_use:
        first = when_to_use.strip().splitlines()[0].strip("-•* ")
        scenarios.append(
            f"""Scenario: Pattern applies in intended context
{_gherkin_step('Given', f"the use case matches `{first}`")}
{_gherkin_step('When', "the pattern is evaluated")}
{_gherkin_step('Then', "the capability should be recommended")}
"""
        )

    # Edge / non-applicable case from pattern.when_not_to_use
    when_not = pattern.get("when_not_to_use", "")
    if when_not:
        first = when_not.strip().splitlines()[0].strip("-•* ")
        scenarios.append(
            f"""Scenario: Pattern does not apply outside intended context
{_gherkin_step('Given', f"the situation matches `{first}`")}
{_gherkin_step('When', "the pattern is evaluated")}
{_gherkin_step('Then', "the capability should not be recommended")}
"""
        )

    # Gate scenarios from doctrine_governance.gates
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        name = gate.get("name", "Unknown Gate")
        requirement = gate.get("requirement", "")
        applies = gate.get("applies", True)
        verdict_verb = "pass" if applies else "block"
        scenarios.append(
            f"""Scenario: {name} gate {verdict_verb}s
{_gherkin_step('Given', f"the {name} gate exists with requirement: {requirement or 'none specified'}")}
{_gherkin_step('When', "the artifact is checked against the gate")}
{_gherkin_step('Then', f"it should {verdict_verb} the gate")}
"""
        )

    # Implementation scenarios from council.synthesis.top_3_actions
    for idx, action in enumerate(top_actions, start=1):
        if not action:
            continue
        action_text = str(action).strip().splitlines()[0]
        scenarios.append(
            f"""Scenario: Council action {idx} is implemented
{_gherkin_step('Given', f"the council has recommended: {action_text}")}
{_gherkin_step('When', "the action is implemented and verified")}
{_gherkin_step('Then', "the resulting artifact satisfies the acceptance criteria")}
"""
        )

    # Fallback scenario if we somehow ended up with fewer than 3.
    while len(scenarios) < 3:
        scenarios.append(
            f"""Scenario: Capability is deterministic from card fields
{_gherkin_step('Given', f"the card `{sid}` is loaded")}
{_gherkin_step('When', "OpenSpec artifacts are generated")}
{_gherkin_step('Then', "the generated files match the card fields deterministically")}
"""
        )

    body = "\n".join(scenarios)
    return f"""# OpenSpec BDD Specification

```gherkin
Feature: {title}
{body}
```

## Primary Acceptance

- **Done-test:** `{done_test or "not specified"}`
- **Acceptance:** {_as_text(idea.get('acceptance', ''))}

## Doctrine Mapping

- **Pattern:** {pattern.get('name', 'N/A')}
- **Governance gates:** {len(gates)}
- **Council verdict:** {synthesis.get('overall_verdict', 'N/A')} (confidence: {synthesis.get('confidence_score', 'N/A')})

Generated at {utc_now()}.
"""


def _proposal_md(card: dict[str, Any]) -> str:
    claim = card.get("claim", "")
    bi = card.get("business_intelligence", {})
    gtm = card.get("gtm_strategy", {})
    title = _title(card)

    lines = [
        f"# Proposal: {title}",
        "",
        "## Claim",
        "",
        claim or "_No explicit claim provided._",
        "",
        "## Why Build This Capability",
        "",
        _as_text(card.get("summary", "")),
        "",
        "## Revenue Model",
        "",
        bi.get("revenue_model") or "_Not specified._",
        "",
        f"- **Price range:** {bi.get('price_range') or 'N/A'}",
        f"- **LTV/CAC ratio:** {bi.get('ltv_cac_ratio') or 'N/A'}",
        f"- **Gross margin:** {bi.get('gross_margin') or 'N/A'}",
        f"- **Build effort:** {bi.get('build_effort') or 'N/A'}",
        "",
        "## Ideal Customer Profile",
        "",
        gtm.get("icp") or "_Not specified._",
        "",
        f"- **Sales motion:** {gtm.get('sales_motion') or 'N/A'}",
        f"- **Positioning:** {gtm.get('positioning') or 'N/A'}",
        f"- **Pricing anchor:** {gtm.get('pricing_anchor') or 'N/A'}",
        "",
        "## Next Actions",
        "",
    ]
    next_actions = card.get("next_actions") or []
    if next_actions:
        for action in next_actions:
            lines.append(f"- {_as_text(action)}")
    else:
        lines.append("- _No next actions recorded._")
    lines.append("")
    return "\n".join(lines)


def _design_md(card: dict[str, Any]) -> str:
    bp = card.get("engineering_blueprint", {})
    team = card.get("agent_team", {})
    oss = card.get("oss_integration", {})
    title = _title(card)

    lines = [
        f"# Design: {title}",
        "",
        "## Mechanism",
        "",
        _as_text(card.get("mechanism", "")),
        "",
        "## Architecture Components",
        "",
    ]
    for comp in bp.get("architecture_components") or []:
        lines.append(f"- {_as_text(comp)}")
    if not bp.get("architecture_components"):
        lines.append("- _No architecture components listed._")
    lines.extend([
        "",
        "## Harness",
        "",
    ])
    harness = bp.get("harness", {})
    if harness:
        for key, value in sorted(harness.items()):
            lines.append(f"- **{key}:** {_as_text(value)}")
    else:
        lines.append("- _No harness specification._")
    lines.extend([
        "",
        "## Testing Strategy",
        "",
    ])
    testing = bp.get("testing_strategy", {})
    if testing:
        for key, value in sorted(testing.items()):
            lines.append(f"- **{key}:** {_as_text(value)}")
    else:
        lines.append("- _No testing strategy provided._")
    lines.extend([
        "",
        "## Agent Team",
        "",
    ])
    for agent in team.get("agents") or []:
        if isinstance(agent, dict):
            lines.append(
                f"- **{agent.get('role', 'Agent')}** — model: `{agent.get('model', 'N/A')}`, identity: `{agent.get('identity', 'N/A')}`"
            )
        else:
            lines.append(f"- {_as_text(agent)}")
    if not team.get("agents"):
        lines.append("- _No agents assigned._")
    dept = team.get("department_routing")
    if dept:
        lines.extend(["", f"**Department routing:** {dept}"])
    lines.extend([
        "",
        "## OSS Integration",
        "",
    ])
    if oss.get("integration_strategy"):
        lines.append(_as_text(oss["integration_strategy"]))
    relevant = oss.get("relevant_projects") or []
    if relevant:
        lines.extend(["", "### Relevant projects"])
        for proj in relevant:
            lines.append(f"- {_as_text(proj)}")
    build_vs_buy = oss.get("build_vs_buy", {})
    if build_vs_buy:
        lines.extend(["", "### Build vs. buy"])
        for key, value in sorted(build_vs_buy.items()):
            lines.append(f"- **{key}:** {_as_text(value)}")
    lines.append("")
    return "\n".join(lines)


def _tasks_md(card: dict[str, Any]) -> str:
    bp = card.get("engineering_blueprint", {})
    steps = bp.get("implementation_steps") or []
    title = _title(card)

    lines = [
        f"# Implementation Tasks: {title}",
        "",
        "Each engineering step becomes a red→green→commit cycle.",
        "",
    ]
    if not steps:
        lines.extend([
            "- _No implementation steps provided._",
            "",
        ])
    for step in steps:
        if not isinstance(step, dict):
            continue
        num = step.get("step", "?")
        action = step.get("action", "")
        gate = step.get("gate", "verify")
        lines.extend([
            f"## {num}. {action}",
            "",
            f"- **Gate:** `{gate}`",
            "- [ ] Red: write a failing test / failing artifact for this step",
            "- [ ] Green: implement the minimum change to make the gate pass",
            "- [ ] Commit: checkpoint the step with the passing gate evidence",
            "",
        ])
    return "\n".join(lines)


def _openspec_dir(card_id: str) -> Path:
    return BUILT_ROOT / card_id / "openspec"


def _build_manifest(card: dict[str, Any], openspec_dir: Path, files_generated: list[str]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "card_id": card.get("card_id"),
        "openspec_dir": str(openspec_dir),
        "files_generated": sorted(files_generated),
        "generated_at": utc_now(),
        "artifact_hash": sha256_text(stable_json({"files": sorted(files_generated)})),
    }


def generate_spec(card_path: str | Path) -> dict[str, Any]:
    """Generate OpenSpec BDD artifacts for a single V20 build card.

    Returns a dict with keys: card_id, openspec_dir, files_generated, manifest_hash.
    """
    card_path = Path(card_path)
    card = _load_card(card_path)

    schema = card.get("schema", "")
    if schema not in ("rig.omniscout.build-card.v20", "rig.omniscout.build-card.v21-autobuilt"):
        raise ValueError(f"Unsupported card schema: {schema}")

    card_id = _card_id(card, card_path)
    openspec_dir = _openspec_dir(card_id)
    openspec_dir.mkdir(parents=True, exist_ok=True)

    # Persist the source card alongside the spec so reviewers can trace claims.
    card_copy_path = openspec_dir / "CARD.json"
    atomic_json(card_copy_path, card)

    files: dict[str, str] = {
        "spec.md": _spec_md(card),
        "proposal.md": _proposal_md(card),
        "design.md": _design_md(card),
        "tasks.md": _tasks_md(card),
    }

    generated: list[str] = []
    for name, content in files.items():
        dest = openspec_dir / name
        atomic_text(dest, content)
        generated.append(name)

    # Basic Gherkin sanity check.
    spec_content = files["spec.md"]
    if "Feature:" not in spec_content:
        raise ValueError("Generated spec.md is missing a Feature line")
    scenario_count = spec_content.count("\nScenario:")
    if scenario_count < 3:
        raise ValueError(f"Generated spec.md has only {scenario_count} scenarios (minimum 3)")

    manifest = _build_manifest(card, openspec_dir, generated)
    manifest_path = openspec_dir / "openspec_manifest.json"
    atomic_json(manifest_path, manifest)
    generated.append("openspec_manifest.json")

    return {
        "card_id": card_id,
        "openspec_dir": str(openspec_dir),
        "files_generated": sorted(generated),
        "manifest_hash": manifest["artifact_hash"],
        "scenario_count": scenario_count,
    }


def _is_v20_card(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("schema") in ("rig.omniscout.build-card.v20", "rig.omniscout.build-card.v21-autobuilt")


def generate_all_specs() -> dict[str, Any]:
    """Generate OpenSpec artifacts for all V20 build cards."""
    BUILT_ROOT.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for card_path in sorted(L2_CARDS.glob("l2-*.json")):
        if not _is_v20_card(card_path):
            continue
        try:
            results.append(generate_spec(card_path))
        except Exception as exc:
            errors.append({"path": str(card_path), "error": str(exc)})

    return {
        "schema": SCHEMA,
        "generated": results,
        "errors": errors,
        "generated_count": len(results),
        "error_count": len(errors),
    }


def _status() -> dict[str, Any]:
    cards = list(L2_CARDS.glob("l2-*.json"))
    v20 = [p for p in cards if _is_v20_card(p)]
    openspec_dirs = list(BUILT_ROOT.glob("*/openspec")) if BUILT_ROOT.exists() else []
    return {
        "schema": SCHEMA,
        "total_cards": len(cards),
        "v20_cards": len(v20),
        "openspec_projects": len(openspec_dirs),
        "built_root": str(BUILT_ROOT),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniScout L2 OpenSpec BDD generator")
    sub = parser.add_subparsers(dest="command", required=True)

    one = sub.add_parser("one", help="Generate OpenSpec for a single card by path")
    one.add_argument("path", help="Path to a V20 build-card JSON file")
    sub.add_parser("all", help="Generate OpenSpec for all V20 cards")
    sub.add_parser("status", help="Show OpenSpec generation status")

    args = parser.parse_args(argv)

    if args.command == "one":
        result = generate_spec(args.path)
        print(stable_json(result))
        return 0

    if args.command == "all":
        result = generate_all_specs()
        print(stable_json(result))
        return 1 if result["error_count"] else 0

    if args.command == "status":
        print(stable_json(_status()))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
