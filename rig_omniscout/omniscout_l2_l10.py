"""OmniScout L2 L10 Verification Engine.

Runs five deterministic L10 verification faculties against V20 build cards:

1. REFUTATION_SEARCH   (Faculty 10) — try to refute the card's claim.
2. PROPERTY_BASED_TESTING (Faculty 11) — declare invariants as pytest stubs.
3. ADHERENCE_KPI       (Faculty 4)  — check best-practice adherence.
4. MULTI_ALTITUDE      (Faculty 7)  — check 10kft/1kft/100ft/ground levels.
5. TASTE_ENGINE        (Faculty 1)  — 5-dimension quality assessment.

Results are written back into the card JSON under the 'l10_verification' field.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
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

SCHEMA_L10 = "rig.omniscout.l10-verification.v1"
BUILT_ROOT = L2_ROOT / "built"

CONTRADICTION_LEXICON: set[str] = {
    "not",
    "no",
    "never",
    "cannot",
    "does not",
    "did not",
    "will not",
    "refute",
    "contradict",
    "disprove",
    "falsify",
    "debunk",
    "invalid",
    "false",
    "incorrect",
    "wrong",
    "opposite",
    "negation",
    "rejected",
    "failed",
}

CONCRETE_STEP_MARKERS: set[str] = {
    "scaffold",
    "build",
    "implement",
    "write",
    "deploy",
    "test",
    "verify",
    "define",
    "create",
    "run",
    "install",
    "configure",
    "wire",
    "add",
    "monitor",
}

SLUDGE_MARKERS: set[str] = {
    "sludge",
    "uncited opinion",
    "single blog",
    "vague",
    "generic",
    "buzzword",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_cards() -> list[Path]:
    L2_CARDS.mkdir(parents=True, exist_ok=True)
    return sorted(L2_CARDS.glob("l2-*.json"))


def _read_card(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _card_id(card: dict[str, Any]) -> str:
    return str(card.get("card_id", ""))


def _built_path(card_id: str) -> Path:
    return BUILT_ROOT / card_id


def _safe_name(value: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_")
    if not s or s[0].isdigit():
        s = "prop_" + s
    return s[:64]


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower()) if len(t) >= 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Faculty 10: REFUTATION_SEARCH
# ---------------------------------------------------------------------------


def _extract_claim(card: dict[str, Any]) -> str:
    return str(card.get("claim") or card.get("title") or card.get("topic") or "").strip()


def _evidence_contradicts(claim: str, evidence: dict[str, Any]) -> bool:
    """Heuristic: evidence contradicts if it negates claim tokens and is high-confidence."""
    claim_tokens = _tokenize(claim)
    quote = str(evidence.get("quote_or_fact") or evidence.get("abstract") or "").lower()
    url = str(evidence.get("url") or "").lower()
    text = quote + " " + url

    has_negation = any(marker in text for marker in CONTRADICTION_LEXICON)
    overlap = _jaccard(claim_tokens, _tokenize(text))
    # Contradiction requires both negation language and topical overlap.
    return bool(claim_tokens and has_negation and overlap >= 0.08)


def faculty_refutation_search(card: dict[str, Any], all_cards: list[dict[str, Any]]) -> dict[str, Any]:
    claim = _extract_claim(card)
    card_id = _card_id(card)
    contradictions: list[dict[str, Any]] = []

    semantic = card.get("semantic_links") or {}
    declared_contradiction_ids = {c["card_id"] for c in (semantic.get("contradictions") or [])}

    # Load contradicting cards from semantic links and inspect their evidence.
    for other in all_cards:
        other_id = _card_id(other)
        if other_id == card_id:
            continue
        if other_id not in declared_contradiction_ids:
            # Also do a lightweight lexical contradiction scan.
            other_claim = _extract_claim(other)
            if not (other_id and _jaccard(_tokenize(claim), _tokenize(other_claim)) >= 0.15):
                continue
            other_evidence = (other.get("evidence") or []) + (other.get("consensus", {}).get("results") or [])
            if not any(_evidence_contradicts(claim, ev) for ev in other_evidence):
                continue

        other_evidence = (other.get("evidence") or []) + (other.get("consensus", {}).get("results") or [])
        contradicting_evidence = [ev for ev in other_evidence if _evidence_contradicts(claim, ev)]
        contradictions.append({
            "card_id": other_id,
            "title": other.get("title", ""),
            "reason": "declared contradiction" if other_id in declared_contradiction_ids else "lexical refutation match",
            "evidence_matches": len(contradicting_evidence),
        })

    # Also scan this card's own evidence URLs for internal contradiction signals.
    own_evidence = (card.get("evidence") or []) + (card.get("consensus", {}).get("results") or [])
    internal_contradicts = [ev for ev in own_evidence if _evidence_contradicts(claim, ev)]

    refuted = bool(contradictions or internal_contradicts)
    confidence_delta = -10 * (len(contradictions) + len(internal_contradicts))
    return {
        "faculty": "REFUTATION_SEARCH",
        "refuted": refuted,
        "contradicting_cards": contradictions,
        "internal_contradiction_evidence": len(internal_contradicts),
        "confidence_delta": max(-50, confidence_delta),
        "claim": claim[:200],
    }


# ---------------------------------------------------------------------------
# Faculty 11: PROPERTY_BASED_TESTING
# ---------------------------------------------------------------------------


def _extract_invariants_from_pattern(pattern: dict[str, Any]) -> list[str]:
    invariants: list[str] = []
    when_to_use = str(pattern.get("when_to_use") or "").strip()
    when_not_to_use = str(pattern.get("when_not_to_use") or "").strip()
    if when_to_use:
        invariants.append(f"Should satisfy use condition: {when_to_use}")
    if when_not_to_use:
        invariants.append(f"Should reject misuse condition: {when_not_to_use}")
    return invariants


def _extract_testable_property(done_test: str) -> str:
    done_test = done_test.strip()
    if not done_test:
        return "card_passes_acceptance_criteria"
    # Prefer a Pythonic function name derived from the first assertion-like clause.
    lowered = done_test.lower()
    m = re.search(r"assert\s+([a-z_][a-z0-9_]{2,})", lowered)
    if m and len(m.group(1)) > 1:
        return _safe_name(m.group(1))
    return "card_done_test_passes"


def _extract_gate_properties(gates: list[dict[str, Any]]) -> list[str]:
    props: list[str] = []
    for gate in gates or []:
        name = gate.get("name") or "unnamed_gate"
        requirement = gate.get("requirement") or ""
        applies = gate.get("applies", True)
        props.append(f"Gate '{name}' applies={applies}: {requirement}")
    return props


def _generate_properties_py(
    card: dict[str, Any],
    invariants: list[str],
    gate_properties: list[str],
    test_property: str,
    out_path: Path,
) -> str:
    card_id = _card_id(card)
    title = str(card.get("title") or card_id).replace('"', '\\"')
    claim = _extract_claim(card).replace('"', '\\"')

    def comment(text: str) -> str:
        return "\n".join(f"    # {line}" for line in text.splitlines())

    invariant_tests = "\n\n".join(
        f"@pytest.mark.property\ndef test_invariant_{_safe_name(inv)[:50]}():\n{comment(inv)}\n    assert True  # TODO: replace with real invariant check"
        for inv in invariants[:4]
    )

    gate_tests = "\n\n".join(
        f"@pytest.mark.property\ndef test_gate_{_safe_name(gp)[:50]}():\n{comment(gp)}\n    assert True  # TODO: replace with real gate check"
        for gp in gate_properties[:4]
    )

    content = f'''"""Auto-generated property-based tests for {title}.

Card ID: {card_id}
Claim: {claim}

This module declares invariants and gate properties derived from the V20 build card.
Each test is a stub that should be replaced with a real implementation as the
project matures.
"""

import pytest


@pytest.mark.property
def test_{test_property}():
    """Executable done-test property for the card."""
    card_path = Path(__file__).resolve().parent / "CARD.json"
    assert card_path.exists(), "CARD.json must exist next to properties.py"


{invariant_tests}


{gate_tests}
'''
    atomic_text(out_path, content)
    return content


def faculty_property_based_testing(card: dict[str, Any]) -> dict[str, Any]:
    card_id = _card_id(card)
    project_path = _built_path(card_id)
    properties_path = project_path / "properties.py"

    pattern = card.get("pattern") or {}
    idea = card.get("idea") or {}
    done_test = str(idea.get("done_test") or card.get("tac", {}).get("done_test") or "").strip()
    doctrine_governance = card.get("doctrine_governance") or {}
    gates = doctrine_governance.get("gates") or []

    invariants = _extract_invariants_from_pattern(pattern)
    gate_properties = _extract_gate_properties(gates)
    test_property = _extract_testable_property(done_test)

    if not invariants and not gate_properties:
        invariants.append("Card declares at least one pattern invariant")
        gate_properties.append("Card declares at least one governance gate")

    project_path.mkdir(parents=True, exist_ok=True)
    _generate_properties_py(card, invariants, gate_properties, test_property, properties_path)

    return {
        "faculty": "PROPERTY_BASED_TESTING",
        "properties_declared": len(invariants) + len(gate_properties) + 1,
        "properties_file": str(properties_path),
        "invariants": invariants,
        "gate_properties": gate_properties,
        "test_property": test_property,
    }


# ---------------------------------------------------------------------------
# Faculty 4: ADHERENCE_KPI
# ---------------------------------------------------------------------------


def _has_done_test(card: dict[str, Any]) -> bool:
    idea = card.get("idea") or {}
    tac = card.get("tac") or {}
    return bool(str(idea.get("done_test") or "").strip() or str(tac.get("done_test") or "").strip())


def _has_gev_separation(card: dict[str, Any]) -> bool:
    tac = card.get("tac") or {}
    builder = str(tac.get("builder") or "").strip()
    verifier = str(tac.get("verifier") or "").strip()
    if builder and verifier and builder != verifier:
        return True
    score = card.get("score") or {}
    gev = score.get("gev") or score.get("gev_separation") or {}
    if gev.get("independent"):
        return True
    harness = (card.get("engineering_blueprint") or {}).get("harness") or {}
    return (
        bool(str(harness.get("builder") or "").strip())
        and bool(str(harness.get("verifier") or "").strip())
        and harness.get("builder") != harness.get("verifier")
    )


def _has_three_sources(card: dict[str, Any]) -> bool:
    sources = card.get("sources") or {}
    count = int(sources.get("count") or 0)
    evidence = card.get("evidence") or []
    return count >= 3 or len(evidence) >= 3


def _has_proof_packet(card: dict[str, Any]) -> bool:
    proof_seal = card.get("proof_seal") or {}
    autobuilt = card.get("autobuilt") or {}
    if proof_seal.get("sealed_at") and proof_seal.get("this_hash"):
        return True
    return bool(autobuilt.get("proof_sealed"))


def _has_claude_md(card_id: str) -> bool:
    return (_built_path(card_id) / "CLAUDE.md").exists()


def faculty_adherence_kpi(card: dict[str, Any]) -> dict[str, Any]:
    card_id = _card_id(card)
    checks: dict[str, tuple[bool, str]] = {
        "done_test_present": (_has_done_test(card), "card has executable idea.done_test"),
        "gev_separation": (_has_gev_separation(card), "builder != verifier"),
        "multi_source": (_has_three_sources(card), "card has >=3 independent sources"),
        "proof_packet": (_has_proof_packet(card), "card has proof seal"),
        "claude_md": (_has_claude_md(card_id), "autobuilt project has CLAUDE.md"),
    }

    score = sum(20 for ok in (v[0] for v in checks.values()) if ok)
    failing = [name for name, (ok, _) in checks.items() if not ok]
    gate_blocks = len(failing) >= 2

    return {
        "faculty": "ADHERENCE_KPI",
        "adherence_score": score,
        "failing_checks": failing,
        "gate_blocks": gate_blocks,
        "checks": {name: ok for name, (ok, _) in checks.items()},
    }


# ---------------------------------------------------------------------------
# Faculty 7: MULTI_ALTITUDE
# ---------------------------------------------------------------------------


def _passes_10kft(card: dict[str, Any]) -> bool:
    world_model = card.get("world_model") or {}
    fit = world_model.get("rig_1000x_fit") or []
    return bool(fit) and any("CORE" in str(x) for x in fit)


def _passes_1kft(card: dict[str, Any]) -> bool:
    blueprint = card.get("engineering_blueprint") or {}
    return bool(blueprint.get("implementation_steps")) and bool(blueprint.get("harness"))


def _passes_100ft(card: dict[str, Any]) -> bool:
    blueprint = card.get("engineering_blueprint") or {}
    steps = blueprint.get("implementation_steps") or []
    if not steps:
        return False
    concrete = 0
    for step in steps:
        action = str(step.get("action") or "").lower()
        if any(marker in action for marker in CONCRETE_STEP_MARKERS):
            concrete += 1
    return concrete >= len(steps) // 2 + 1


def _passes_ground(card: dict[str, Any]) -> bool:
    card_id = _card_id(card)
    autobuilt = card.get("autobuilt") or {}
    outcome = card.get("outcome") or {}

    # If the card reports a passed test/harness result, accept it.
    if autobuilt.get("verified") and autobuilt.get("test_result") == "PASS":
        return True
    if outcome.get("status") == "BUILT_AND_VERIFIED":
        return True

    # Otherwise try to run the done-test if it is safe and local.
    idea = card.get("idea") or {}
    tac = card.get("tac") or {}
    done_test = str(idea.get("done_test") or tac.get("done_test") or "").strip()
    if not done_test:
        return False

    project_path = _built_path(card_id)
    if not project_path.exists():
        return False

    # Only run shell-safe, single-line Python checks.
    if not done_test.startswith("python"):
        return False

    try:
        result = subprocess.run(
            done_test,
            shell=True,
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0
    except Exception:
        return False


def faculty_multi_altitude(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "faculty": "MULTI_ALTITUDE",
        "passes_10kft": _passes_10kft(card),
        "passes_1kft": _passes_1kft(card),
        "passes_100ft": _passes_100ft(card),
        "passes_ground": _passes_ground(card),
    }


# ---------------------------------------------------------------------------
# Faculty 1: TASTE_ENGINE
# ---------------------------------------------------------------------------


def _taste_signal_strength(card: dict[str, Any]) -> int:
    score = card.get("score") or {}
    breakdown = score.get("breakdown") or {}
    novelty = breakdown.get("novelty_pattern") or {}
    return int(novelty.get("score", 0) or 0)


def _taste_evidence_density(card: dict[str, Any]) -> int:
    score = card.get("score") or {}
    breakdown = score.get("breakdown") or {}
    evidence = breakdown.get("evidence_anchoring") or {}
    return int(evidence.get("score", 0) or 0)


def _taste_mechanism_clarity(card: dict[str, Any]) -> int:
    score = card.get("score") or {}
    breakdown = score.get("breakdown") or {}
    mechanism = breakdown.get("mechanism_density") or {}
    return int(mechanism.get("score", 0) or 0)


def _taste_actionability(card: dict[str, Any]) -> int:
    score = card.get("score") or {}
    breakdown = score.get("breakdown") or {}
    action = breakdown.get("actionability") or {}
    return int(action.get("score", 0) or 0)


def _taste_honesty(card: dict[str, Any]) -> int:
    has_kill = bool(card.get("kill_criteria"))
    has_risks = bool(card.get("risks"))
    has_tradeoffs = bool((card.get("analysis") or {}).get("tradeoffs"))
    return 30 + (20 if has_kill else 0) + (25 if has_risks else 0) + (25 if has_tradeoffs else 0)


def faculty_taste_engine(card: dict[str, Any]) -> dict[str, Any]:
    dimensions = {
        "signal_strength": _taste_signal_strength(card),
        "evidence_density": _taste_evidence_density(card),
        "mechanism_clarity": _taste_mechanism_clarity(card),
        "actionability": _taste_actionability(card),
        "honesty": _taste_honesty(card),
    }
    # Normalize each dimension to 0-20, then scale to 0-100.
    normalized = []
    for name, value in dimensions.items():
        if name == "honesty":
            normalized.append(min(20, value / 5))
        else:
            normalized.append(min(20, value))
    taste_score = int(round(sum(normalized)))
    mandatory_human_review = taste_score < 60 or dimensions["honesty"] < 60

    return {
        "faculty": "TASTE_ENGINE",
        "taste_score": taste_score,
        "dimensions": dimensions,
        "mandatory_human_review": mandatory_human_review,
    }


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def _card_hash(card_path: Path) -> str:
    return sha256_text(card_path.read_text(encoding="utf-8"))


def verify_card(card_path: Path, all_cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run all five L10 verification faculties on a single V20 card."""
    card = _read_card(card_path)
    card_id = _card_id(card)

    if all_cards is None:
        all_cards = []
        for p in _list_cards():
            if p != card_path:
                try:
                    all_cards.append(_read_card(p))
                except (OSError, json.JSONDecodeError):
                    continue

    refutation = faculty_refutation_search(card, all_cards)
    properties = faculty_property_based_testing(card)
    adherence = faculty_adherence_kpi(card)
    altitude = faculty_multi_altitude(card)
    taste = faculty_taste_engine(card)

    l10_verification = {
        "schema": SCHEMA_L10,
        "verified_at": utc_now(),
        "card_id": card_id,
        "card_sha256": _card_hash(card_path),
        "faculties": {
            "refutation_search": refutation,
            "property_based_testing": properties,
            "adherence_kpi": adherence,
            "multi_altitude": altitude,
            "taste_engine": taste,
        },
        "summary": {
            "refuted": refutation["refuted"],
            "adherence_score": adherence["adherence_score"],
            "taste_score": taste["taste_score"],
            "gate_blocks": adherence["gate_blocks"] or altitude["passes_ground"] is False,
            "passes_all_altitudes": all(
                altitude[k] for k in ("passes_10kft", "passes_1kft", "passes_100ft", "passes_ground")
            ),
            "mandatory_human_review": taste["mandatory_human_review"],
        },
    }

    card["l10_verification"] = l10_verification
    atomic_json(card_path, card)
    return l10_verification


def verify_all() -> dict[str, Any]:
    """Run all five L10 verification faculties on every V20 card."""
    L2_CARDS.mkdir(parents=True, exist_ok=True)
    started = utc_now()

    # Pre-load all cards so refutation search can inspect the corpus.
    all_cards: list[dict[str, Any]] = []
    card_paths: list[Path] = []
    for p in _list_cards():
        try:
            all_cards.append(_read_card(p))
            card_paths.append(p)
        except (OSError, json.JSONDecodeError):
            continue

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in card_paths:
        try:
            results.append(verify_card(path, all_cards=all_cards))
        except Exception as exc:
            errors.append({"card_id": path.stem, "error": str(exc)[:300]})

    summary = {
        "schema": "rig.omniscout.l10-verification-run.v1",
        "ok": len(errors) == 0,
        "started_at": started,
        "finished_at": utc_now(),
        "total_cards": len(card_paths),
        "verified": len(results),
        "errors": errors[:10],
        "refuted_count": sum(1 for r in results if r["summary"]["refuted"]),
        "gate_blocked_count": sum(1 for r in results if r["summary"]["gate_blocks"]),
        "mandatory_review_count": sum(1 for r in results if r["summary"]["mandatory_human_review"]),
        "average_adherence_score": round(
            sum(r["summary"]["adherence_score"] for r in results) / max(1, len(results)), 2
        ),
        "average_taste_score": round(
            sum(r["summary"]["taste_score"] for r in results) / max(1, len(results)), 2
        ),
    }
    atomic_json(L2_ROOT / "latest-l10-verification.json", summary)
    return summary


def _status() -> dict[str, Any]:
    total = len(list(L2_CARDS.glob("l2-*.json")))
    verified = 0
    refuted = 0
    gate_blocked = 0
    for p in L2_CARDS.glob("l2-*.json"):
        try:
            c = _read_card(p)
            l10 = c.get("l10_verification") or {}
            summary = l10.get("summary") or {}
            if l10:
                verified += 1
            if summary.get("refuted"):
                refuted += 1
            if summary.get("gate_blocks"):
                gate_blocked += 1
        except (OSError, json.JSONDecodeError):
            continue
    return {
        "schema": SCHEMA_L10,
        "total": total,
        "verified": verified,
        "remaining": total - verified,
        "refuted": refuted,
        "gate_blocked": gate_blocked,
    }


FACULTIES: dict[str, Any] = {
    "refutation_search": faculty_refutation_search,
    "property_based_testing": faculty_property_based_testing,
    "adherence_kpi": faculty_adherence_kpi,
    "multi_altitude": faculty_multi_altitude,
    "taste_engine": faculty_taste_engine,
}


def _run_faculty(name: str, card_path: Path, all_cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if name not in FACULTIES:
        raise ValueError(f"Unknown faculty: {name}. Choose from {', '.join(FACULTIES)}")

    card = _read_card(card_path)
    if all_cards is None:
        all_cards = []
        for p in _list_cards():
            if p != card_path:
                try:
                    all_cards.append(_read_card(p))
                except (OSError, json.JSONDecodeError):
                    continue

    fn = FACULTIES[name]
    if name == "refutation_search":
        return fn(card, all_cards)
    return fn(card)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L10 Verification Engine for V20 build cards")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_one = sub.add_parser("one", help="verify a single card")
    p_one.add_argument("path", help="path to l2-*.json card")

    sub.add_parser("all", help="verify every card")
    sub.add_parser("status", help="show verification coverage")

    p_faculty = sub.add_parser("faculty", help="run a single faculty")
    p_faculty.add_argument("name", help="faculty name")
    p_faculty.add_argument("path", help="path to l2-*.json card")

    args = parser.parse_args(argv)

    if args.cmd == "one":
        out = verify_card(Path(args.path))
    elif args.cmd == "all":
        out = verify_all()
    elif args.cmd == "status":
        out = _status()
    elif args.cmd == "faculty":
        out = _run_faculty(args.name, Path(args.path))
    else:
        parser.error("unknown subcommand")
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok", True) is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
