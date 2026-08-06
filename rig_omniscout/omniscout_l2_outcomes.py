"""OmniScout L2 outcome tracking system.

Adds outcome/accuracy fields to V20 build cards and tracks
card -> pipeline -> revenue without LLM calls in the hot path.
All enrichment is deterministic/pattern-based.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

from rig_foundry.omniscout_build_cards import (
    L2_CARDS,
    L2_ROOT,
    atomic_json,
    sha256_text,
    stable_json,
    utc_now,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUTCOME_SCHEMA = "rig.omniscout.build-card-outcome.v1"

VALID_STATUSES = {
    "UNEVALUATED",
    "BUILDING",
    "SHIPPED",
    "REVENUE",
    "FAILED",
}

WORD_PROBS = {
    "certain": 0.99,
    "definite": 0.95,
    "definitely": 0.95,
    "almost certainly": 0.90,
    "highly likely": 0.85,
    "very likely": 0.80,
    "likely": 0.70,
    "probable": 0.65,
    "probably": 0.60,
    "possibly": 0.40,
    "maybe": 0.35,
    "uncertain": 0.30,
    "unlikely": 0.20,
    "improbable": 0.15,
    "very unlikely": 0.10,
    "almost never": 0.05,
    "impossible": 0.01,
}

# Percentage / decimal probability patterns
_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_DECIMAL_RE = re.compile(r"\b(0?\.\d{1,4})\b")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_card(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _card_text_blob(card: dict[str, Any]) -> str:
    """Flatten card text fields for deterministic scanning."""
    parts: list[str] = []
    for key in (
        "claim",
        "summary",
        "mechanism",
        "why_not_median",
        "council_summary",
        "world_model",
        "business_intelligence",
        "gtm_strategy",
        "engineering_blueprint",
    ):
        val = card.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, dict):
            parts.append(stable_json(val))
        elif isinstance(val, list):
            parts.append(stable_json(val))
    return " ".join(parts)


def _card_file_by_id(card_id: str) -> Path | None:
    """Locate a card file by card_id in the canonical L2 card directory."""
    if not L2_CARDS.exists():
        return None
    for path in L2_CARDS.glob("l2-*.json"):
        try:
            card = _load_card(path)
        except Exception:
            continue
        if card.get("card_id") == card_id:
            return path
    return None


def _default_outcome() -> dict[str, Any]:
    return {
        "schema": OUTCOME_SCHEMA,
        "status": "UNEVALUATED",
        "pipeline_created": 0,
        "revenue_generated": 0.0,
        "accuracy_predictions": [],
        "build_started_at": None,
        "shipped_at": None,
        "feedback_score": None,
        "lessons": [],
        "initialized_at": utc_now(),
        "updated_at": utc_now(),
    }


def _calculate_feedback_score(outcome: dict[str, Any], card: dict[str, Any] | None = None) -> float:
    """Deterministic feedback score (0-100) from outcome state.

    Weighs revenue realization, pipeline creation, build completion,
    and accuracy calibration. No LLM calls.
    """
    if outcome.get("status") == "UNEVALUATED":
        return 0.0

    status_score = {
        "BUILDING": 25.0,
        "SHIPPED": 60.0,
        "REVENUE": 90.0,
        "FAILED": 10.0,
    }.get(outcome.get("status"), 0.0)

    revenue = float(outcome.get("revenue_generated") or 0.0)
    revenue_component = min(20.0, math.log1p(revenue / 1000.0) * 5.0) if revenue > 0 else 0.0

    pipeline = int(outcome.get("pipeline_created") or 0)
    pipeline_component = min(10.0, pipeline * 2.0)

    predictions = outcome.get("accuracy_predictions") or []
    if predictions:
        avg_brier = sum(p.get("brier_score", 1.0) for p in predictions) / len(predictions)
        calibration_component = max(0.0, (1.0 - avg_brier) * 20.0)
    else:
        calibration_component = 0.0

    lessons = outcome.get("lessons") or []
    lesson_component = min(5.0, len(lessons) * 1.0)

    raw = status_score + revenue_component + pipeline_component + calibration_component + lesson_component
    return round(min(100.0, raw), 2)


# ---------------------------------------------------------------------------
# Probability claim extraction
# ---------------------------------------------------------------------------


def _extract_probability_claims(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract probability claims from card text deterministically.

    Returns list of {claim_text, predicted_probability, source_field}.
    """
    blob = _card_text_blob(card).lower()
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Percentage claims
    for match in _PERCENT_RE.finditer(blob):
        pct = float(match.group(1))
        if pct > 100:
            continue
        prob = pct / 100.0
        text = match.group(0)
        key = sha256_text(text)
        if key in seen:
            continue
        seen.add(key)
        claims.append(
            {
                "claim_text": text,
                "predicted_probability": round(prob, 4),
                "source_field": "percentage",
            }
        )

    # Decimal probability claims (only if context hints at probability/confidence)
    probability_context = "probability" in blob or "confidence" in blob or "forecast" in blob
    if probability_context:
        for match in _DECIMAL_RE.finditer(blob):
            val = float(match.group(1))
            if 0.0 < val < 1.0:
                text = match.group(0)
                key = sha256_text(text)
                if key in seen:
                    continue
                seen.add(key)
                claims.append(
                    {
                        "claim_text": text,
                        "predicted_probability": round(val, 4),
                        "source_field": "decimal",
                    }
                )

    # Lexical probability claims
    for phrase, prob in sorted(WORD_PROBS.items(), key=lambda kv: -len(kv[0])):
        if phrase in blob:
            key = sha256_text(phrase)
            if key in seen:
                continue
            seen.add(key)
            claims.append(
                {
                    "claim_text": phrase,
                    "predicted_probability": prob,
                    "source_field": "lexical",
                }
            )

    return claims


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_outcome(card_path: str | Path) -> dict[str, Any]:
    """Add an 'outcome' field to a V20 card if missing and persist it."""
    path = Path(card_path)
    card = _load_card(path)

    if "outcome" in card:
        return card["outcome"]

    outcome = _default_outcome()
    card["outcome"] = outcome
    card["outcome_initialized_at"] = utc_now()
    atomic_json(path, card)
    return outcome


def update_outcome(card_id: str, outcome_data: dict[str, Any]) -> dict[str, Any]:
    """Update the outcome field for a card by card_id.

    Validates status, merges updates, recalculates feedback_score,
    and persists the card.
    """
    path = _card_file_by_id(card_id)
    if path is None:
        raise FileNotFoundError(f"Card not found for id: {card_id}")

    card = _load_card(path)
    outcome = card.get("outcome") or _default_outcome()

    # Merge allowed fields
    allowed = {
        "status",
        "pipeline_created",
        "revenue_generated",
        "accuracy_predictions",
        "build_started_at",
        "shipped_at",
        "lessons",
    }
    for key, value in outcome_data.items():
        if key not in allowed:
            continue
        if key == "status" and value not in VALID_STATUSES:
            continue
        outcome[key] = value

    outcome["updated_at"] = utc_now()
    outcome["feedback_score"] = _calculate_feedback_score(outcome, card)

    card["outcome"] = outcome
    atomic_json(path, card)
    return outcome


def compute_brier(card_path: str | Path) -> dict[str, Any]:
    """Compute a self-referential Brier score for probability claims in a card.

    For forecasting-calibration cards this is especially meaningful: we extract
    probability claims from the card text and compare them to the actual score
    outcome (score.total / 100). Returns a numeric Brier score, an assessment,
    and the list of extracted claims.
    """
    path = Path(card_path)
    card = _load_card(path)

    claims = _extract_probability_claims(card)
    score = card.get("score") or {}
    total = score.get("total")
    actual = total / 100.0 if isinstance(total, (int, float)) and total is not None else 0.5
    actual = max(0.0, min(1.0, actual))

    scored_claims: list[dict[str, Any]] = []
    brier_sum = 0.0
    for claim in claims:
        p = float(claim["predicted_probability"])
        brier = (p - actual) ** 2
        claim["brier_score"] = round(brier, 6)
        claim["actual_outcome"] = round(actual, 4)
        scored_claims.append(claim)
        brier_sum += brier

    if scored_claims:
        brier_score = brier_sum / len(scored_claims)
    else:
        # No claims: report a neutral/maximum-uncertainty score of 0.25
        brier_score = 0.25

    if brier_score < 0.05:
        assessment = "well-calibrated"
    elif brier_score < 0.15:
        assessment = "moderately-calibrated"
    elif brier_score < 0.25:
        assessment = "poorly-calibrated"
    else:
        assessment = "uncalibrated"

    return {
        "brier_score": round(brier_score, 6),
        "calibration_assessment": assessment,
        "claim_probabilities": scored_claims,
        "card_id": card.get("card_id"),
        "actual_outcome": round(actual, 4),
        "computed_at": utc_now(),
    }


def get_outcomes() -> dict[str, Any]:
    """Summarize outcomes across all V20 cards in L2_CARDS."""
    if not L2_CARDS.exists():
        return {
            "schema": "rig.omniscout.outcome-summary.v1",
            "total_evaluated": 0,
            "total_revenue": 0.0,
            "avg_feedback_score": 0.0,
            "status_distribution": {},
            "top_performers": [],
            "underperformers": [],
            "generated_at": utc_now(),
        }

    status_distribution: dict[str, int] = {}
    evaluated = 0
    total_revenue = 0.0
    feedback_scores: list[float] = []
    performers: list[dict[str, Any]] = []

    for path in L2_CARDS.glob("l2-*.json"):
        try:
            card = _load_card(path)
        except Exception:
            continue
        if card.get("schema") != "rig.omniscout.build-card.v20":
            continue
        outcome = card.get("outcome")
        if not outcome:
            continue

        status = outcome.get("status", "UNEVALUATED")
        status_distribution[status] = status_distribution.get(status, 0) + 1

        if status != "UNEVALUATED":
            evaluated += 1

        revenue = float(outcome.get("revenue_generated") or 0.0)
        total_revenue += revenue

        fb = outcome.get("feedback_score")
        if fb is not None:
            feedback_scores.append(float(fb))

        score = card.get("score") or {}
        performers.append(
            {
                "card_id": card.get("card_id"),
                "title": card.get("title"),
                "status": status,
                "revenue_generated": revenue,
                "feedback_score": fb,
                "score_total": score.get("total"),
                "score_rank": score.get("rank"),
            }
        )

    avg_feedback = round(sum(feedback_scores) / len(feedback_scores), 2) if feedback_scores else 0.0

    top_performers = sorted(
        [p for p in performers if p["feedback_score"] is not None],
        key=lambda x: (x["feedback_score"] or 0.0, x["revenue_generated"]),
        reverse=True,
    )[:10]

    underperformers = sorted(
        [p for p in performers if p["feedback_score"] is not None],
        key=lambda x: (x["feedback_score"] or 0.0, -x["revenue_generated"]),
    )[:10]

    return {
        "schema": "rig.omniscout.outcome-summary.v1",
        "total_evaluated": evaluated,
        "total_revenue": round(total_revenue, 2),
        "avg_feedback_score": avg_feedback,
        "status_distribution": status_distribution,
        "top_performers": top_performers,
        "underperformers": underperformers,
        "generated_at": utc_now(),
    }


def init_all() -> dict[str, Any]:
    """Initialize outcome tracking on all V20 cards that do not have it yet."""
    initialized: list[str] = []
    skipped: list[str] = []

    if L2_CARDS.exists():
        for path in L2_CARDS.glob("l2-*.json"):
            try:
                card = _load_card(path)
            except Exception:
                continue
            if card.get("schema") != "rig.omniscout.build-card.v20":
                continue
            if "outcome" in card:
                skipped.append(card.get("card_id", path.name))
            else:
                init_outcome(path)
                initialized.append(card.get("card_id", path.name))

    return {
        "schema": "rig.omniscout.outcome-init-all.v1",
        "initialized": initialized,
        "skipped": skipped,
        "initialized_count": len(initialized),
        "skipped_count": len(skipped),
        "generated_at": utc_now(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniScout L2 outcome tracker")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize outcome tracking on all V20 cards")
    sub.add_parser("summary", help="Print outcome summary across all cards")

    p_init_one = sub.add_parser("init-one", help="Initialize outcome tracking on a single card file")
    p_init_one.add_argument("path", help="Path to a V20 build-card JSON file")

    p_update = sub.add_parser("update", help="Update outcome for a card by card_id")
    p_update.add_argument("card_id", help="Card ID")
    p_update.add_argument("json_data", help="JSON object with outcome fields")

    p_brier = sub.add_parser("brier", help="Compute self-referential Brier score for a card")
    p_brier.add_argument("path", help="Path to a V20 build-card JSON file")

    args = parser.parse_args(argv)

    if args.command == "init":
        out = init_all()
        print(stable_json(out))
        return 0

    if args.command == "init-one":
        outcome = init_outcome(args.path)
        print(stable_json({"ok": True, "outcome": outcome}))
        return 0

    if args.command == "update":
        data = json.loads(args.json_data)
        outcome = update_outcome(args.card_id, data)
        print(stable_json({"ok": True, "outcome": outcome}))
        return 0

    if args.command == "brier":
        result = compute_brier(args.path)
        print(stable_json(result))
        return 0

    if args.command == "summary":
        summary = get_outcomes()
        print(stable_json(summary))
        return 0

    # Default: init all + summary
    init_all()
    summary = get_outcomes()
    print(stable_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
