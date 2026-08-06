"""OmniScout L2 adaptive feedback loop.

Learns from card outcomes and rebalances the deterministic scorer weights.
No LLM calls in the hot path — all adjustments are pattern-based / arithmetic.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from rig_foundry.omniscout_build_cards import (
    L2_CARDS,
    L2_ROOT,
    atomic_json,
    stable_json,
    utc_now,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: dict[str, int] = {
    "multi_source": 18,
    "mechanism_density": 18,
    "evidence_anchoring": 14,
    "tac_structure": 12,
    "doctrine_fit": 12,
    "novelty_pattern": 10,
    "actionability": 8,
    "gev_separation": 8,
}

DIMENSIONS = list(DEFAULT_WEIGHTS.keys())

ADAPTIVE_WEIGHTS_PATH = L2_ROOT / "adaptive_weights.json"
FEEDBACK_CORRELATION_PATH = L2_ROOT / "feedback_correlation.json"

SCHEMA_ADAPTIVE_WEIGHTS = "rig.omniscout.l2-adaptive-weights.v1"
SCHEMA_FEEDBACK_CORRELATION = "rig.omniscout.l2-feedback-correlation.v1"

CORRELATION_SENSITIVITY = 0.5  # how strongly correlations move weights
MIN_WEIGHT = 1
MAX_WEIGHT = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _list_cards() -> list[Path]:
    return sorted(L2_CARDS.glob("l2-*.json"))


def _read_card(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _cards_with_outcomes() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in _list_cards():
        try:
            card = _read_card(p)
        except Exception:
            continue
        outcome = card.get("outcome") or {}
        if outcome.get("feedback_score") is not None:
            out.append(card)
    return out


def _outcome_value(outcome: dict[str, Any]) -> float:
    """Deterministic composite outcome signal.

    Combines explicit feedback_score with shipped status, accuracy, and
    (capped) revenue into a single scalar for correlation.
    """
    val = 0.0
    fs = outcome.get("feedback_score")
    if isinstance(fs, (int, float)):
        val += float(fs)
    acc = outcome.get("accuracy")
    if isinstance(acc, (int, float)):
        val += float(acc)
    if outcome.get("shipped"):
        val += 1.0
    rev = outcome.get("revenue")
    if isinstance(rev, (int, float)):
        # revenue is expected to dwarf the 0-1 scale; cap & compress
        val += min(float(rev) / 1000.0, 5.0)
    return val


def _dimension_score(card: dict[str, Any], dim: str) -> float:
    breakdown = ((card.get("score") or {}).get("breakdown")) or {}
    entry = breakdown.get(dim) or {}
    return float(entry.get("score", 0))


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def _round_to_hundred(values: dict[str, float]) -> dict[str, int]:
    """Round a set of floats that already sum ~100 to integers summing exactly 100."""
    floors = {k: math.floor(v) for k, v in values.items()}
    remainder = 100 - sum(floors.values())
    if remainder <= 0:
        return floors
    # distribute remainder to dimensions with largest fractional parts
    fracs = sorted(
        ((v - math.floor(v), k) for k, v in values.items()),
        key=lambda x: (x[0], x[1]),
        reverse=True,
    )
    for _, k in fracs[:remainder]:
        floors[k] += 1
    return floors


def _normalize_weights(raw: dict[str, float]) -> dict[str, int]:
    total = sum(max(MIN_WEIGHT, min(MAX_WEIGHT, v)) for v in raw.values())
    if total == 0:
        return dict(DEFAULT_WEIGHTS)
    scaled = {
        k: max(MIN_WEIGHT, min(MAX_WEIGHT, v)) / total * 100 for k, v in raw.items()
    }
    return _round_to_hundred(scaled)


def _weights_equal(a: dict[str, int], b: dict[str, int]) -> bool:
    return all(a.get(k) == b.get(k) for k in DIMENSIONS)


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def compute_adaptive_weights(*, write: bool = True) -> dict[str, int]:
    """Learn from card outcomes and return adjusted dimension weights.

    If no cards have outcome.feedback_score set, returns the default weights.
    """
    cards = _cards_with_outcomes()
    if not cards:
        weights = dict(DEFAULT_WEIGHTS)
        if write:
            atomic_json(
                ADAPTIVE_WEIGHTS_PATH,
                {
                    "schema": SCHEMA_ADAPTIVE_WEIGHTS,
                    "computed_at": utc_now(),
                    "reason": "no_outcomes_yet",
                    "weights": weights,
                    "correlations": {d: 0.0 for d in DIMENSIONS},
                },
            )
        return weights

    xs: dict[str, list[float]] = {d: [] for d in DIMENSIONS}
    ys: list[float] = []
    for card in cards:
        outcome = card.get("outcome") or {}
        ys.append(_outcome_value(outcome))
        for dim in DIMENSIONS:
            xs[dim].append(_dimension_score(card, dim))

    correlations: dict[str, float] = {}
    for dim in DIMENSIONS:
        correlations[dim] = _pearson(xs[dim], ys)

    raw: dict[str, float] = {}
    for dim in DIMENSIONS:
        base = float(DEFAULT_WEIGHTS[dim])
        corr = correlations[dim]
        # positive correlation increases weight, negative decreases it
        raw[dim] = base * (1.0 + CORRELATION_SENSITIVITY * corr)

    weights = _normalize_weights(raw)

    if write:
        atomic_json(
            ADAPTIVE_WEIGHTS_PATH,
            {
                "schema": SCHEMA_ADAPTIVE_WEIGHTS,
                "computed_at": utc_now(),
                "reason": "outcome_correlation",
                "cards_used": len(cards),
                "weights": weights,
                "correlations": {d: round(correlations[d], 4) for d in DIMENSIONS},
                "default_weights": dict(DEFAULT_WEIGHTS),
            },
        )

    return weights


def rescore_all(*, write: bool = True) -> dict[str, Any]:
    """Apply adaptive weights to every card and persist updated scores.

    Loads the weights stored by compute_adaptive_weights().
    Only mutates cards when the current adaptive weights differ from defaults.
    """
    stored = _load_json(ADAPTIVE_WEIGHTS_PATH, {})
    weights_raw = stored.get("weights") or {}
    if weights_raw and all(k in weights_raw for k in DIMENSIONS):
        weights = {k: int(weights_raw[k]) for k in DIMENSIONS}
    else:
        weights = dict(DEFAULT_WEIGHTS)
    if _weights_equal(weights, DEFAULT_WEIGHTS):
        return {
            "ok": True,
            "changed": False,
            "reason": "weights_are_defaults",
            "weights": weights,
            "cards_updated": 0,
        }

    updated = 0
    for p in _list_cards():
        try:
            card = _read_card(p)
        except Exception:
            continue
        score = card.get("score") or {}
        breakdown = score.get("breakdown") or {}
        if not breakdown:
            continue

        total = 0.0
        for dim in DIMENSIONS:
            entry = breakdown.get(dim)
            if not isinstance(entry, dict):
                continue
            old_weight = DEFAULT_WEIGHTS[dim]
            new_weight = weights[dim]
            entry["weight"] = new_weight
            raw_score = float(entry.get("score", 0))
            total += raw_score * (new_weight / old_weight)

        hard_blocks = list(score.get("hard_blocks") or [])
        if "fewer_than_2_independent_sources" in hard_blocks:
            total = min(total, 54)
        if "mechanism_missing_or_thin" in hard_blocks:
            total = min(total, 54)
        if "done_test_missing" in hard_blocks:
            total = min(total, 69)

        total = int(round(total))
        good_floor = int(score.get("good_floor", 70))
        if total >= 94:
            rank = "EXCELLENT"
        elif total >= 85:
            rank = "STRONG"
        elif total >= good_floor:
            rank = "GOOD"
        elif total >= 55:
            rank = "WEAK"
        else:
            rank = "REJECT"

        promote = rank in {"GOOD", "STRONG", "EXCELLENT"} and not any(
            b in hard_blocks for b in ("fewer_than_2_independent_sources", "mechanism_missing_or_thin")
        )

        score["total"] = total
        score["rank"] = rank
        score["promote"] = promote
        score["scored_at"] = utc_now()
        score["scorer"] = "omniscout-l2-scorer-adaptive-v1"
        card["score"] = score

        if write:
            atomic_json(p, card)
        updated += 1

    if write:
        atomic_json(
            ADAPTIVE_WEIGHTS_PATH,
            {
                "schema": SCHEMA_ADAPTIVE_WEIGHTS,
                "applied_at": utc_now(),
                "reason": "rescored_all",
                "weights": weights,
                "cards_updated": updated,
            },
        )

    return {
        "ok": True,
        "changed": True,
        "weights": weights,
        "cards_updated": updated,
    }


def get_feedback_summary() -> dict[str, Any]:
    """Return a concise summary of feedback coverage and weight adjustments."""
    cards = _cards_with_outcomes()
    weights = _load_json(ADAPTIVE_WEIGHTS_PATH, {}).get("weights") or dict(DEFAULT_WEIGHTS)
    correlations = _load_json(ADAPTIVE_WEIGHTS_PATH, {}).get("correlations") or {
        d: 0.0 for d in DIMENSIONS
    }

    feedback_scores: list[float] = []
    for card in cards:
        outcome = card.get("outcome") or {}
        fs = outcome.get("feedback_score")
        if isinstance(fs, (int, float)):
            feedback_scores.append(float(fs))

    avg_feedback = round(sum(feedback_scores) / len(feedback_scores), 4) if feedback_scores else 0.0

    weight_adjustments_made = not _weights_equal(
        {k: int(weights.get(k, DEFAULT_WEIGHTS[k])) for k in DIMENSIONS},
        DEFAULT_WEIGHTS,
    )

    top_corr = sorted(
        ((abs(float(correlations.get(d, 0.0))), d) for d in DIMENSIONS),
        key=lambda x: x[0],
        reverse=True,
    )
    top_correlated_dimensions = [
        {"dimension": d, "correlation": round(float(correlations.get(d, 0.0)), 4)}
        for _, d in top_corr
    ]

    recommendations: list[str] = []
    for d in DIMENSIONS:
        c = float(correlations.get(d, 0.0))
        if c > 0.3:
            recommendations.append(f"{d} is positively correlated with outcomes; weight was boosted.")
        elif c < -0.3:
            recommendations.append(f"{d} is negatively correlated with outcomes; weight was reduced.")
    if not cards:
        recommendations.append("No outcome data yet; record feedback_score on cards to enable learning.")
    if not weight_adjustments_made and cards:
        recommendations.append("Outcome correlations were too weak to justify weight adjustments.")

    return {
        "total_cards_with_outcomes": len(cards),
        "avg_feedback_score": avg_feedback,
        "weight_adjustments_made": weight_adjustments_made,
        "current_weights": {k: int(weights.get(k, DEFAULT_WEIGHTS[k])) for k in DIMENSIONS},
        "default_weights": dict(DEFAULT_WEIGHTS),
        "top_correlated_dimensions": top_correlated_dimensions,
        "recommendations": recommendations,
    }


def learn_from_outcome(card_id: str) -> dict[str, Any]:
    """Read a single card outcome and append it to the running correlation table."""
    path = L2_CARDS / f"{card_id}.json"
    if not path.exists():
        return {"ok": False, "error": f"card_not_found: {card_id}"}

    try:
        card = _read_card(path)
    except Exception as exc:
        return {"ok": False, "error": f"read_failed: {exc}"}

    outcome = card.get("outcome") or {}
    if outcome.get("feedback_score") is None:
        return {"ok": False, "error": "card_has_no_feedback_score"}

    corr = _load_json(
        FEEDBACK_CORRELATION_PATH,
        {
            "schema": SCHEMA_FEEDBACK_CORRELATION,
            "created_at": utc_now(),
            "records": [],
        },
    )
    if not isinstance(corr, dict):
        corr = {
            "schema": SCHEMA_FEEDBACK_CORRELATION,
            "created_at": utc_now(),
            "records": [],
        }

    record = {
        "card_id": card_id,
        "recorded_at": utc_now(),
        "dimension_scores": {d: _dimension_score(card, d) for d in DIMENSIONS},
        "outcome_snapshot": {
            "feedback_score": outcome.get("feedback_score"),
            "accuracy": outcome.get("accuracy"),
            "shipped": outcome.get("shipped"),
            "revenue": outcome.get("revenue"),
            "composite": round(_outcome_value(outcome), 4),
        },
    }
    corr.setdefault("records", []).append(record)
    corr["updated_at"] = utc_now()
    corr["record_count"] = len(corr["records"])

    if write := True:
        atomic_json(FEEDBACK_CORRELATION_PATH, corr)

    return {
        "ok": True,
        "card_id": card_id,
        "record_count": corr["record_count"],
        "composite_outcome": record["outcome_snapshot"]["composite"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="omniscout-l2-feedback",
        description="Adaptive feedback loop for OmniScout L2 build-card scores.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("weights", help="Compute and store adaptive weights from outcomes.")
    sub.add_parser("rescore", help="Re-score all cards using current adaptive weights.")
    sub.add_parser("summary", help="Show feedback summary.")
    learn_p = sub.add_parser("learn", help="Record one card's outcome into the correlation table.")
    learn_p.add_argument("card_id", help="Card ID (e.g. l2-abc123...)")

    args = parser.parse_args(argv)

    if args.command == "weights":
        weights = compute_adaptive_weights()
        print(stable_json({"command": "weights", "weights": weights}))
        return 0

    if args.command == "rescore":
        result = rescore_all()
        print(stable_json({"command": "rescore", **result}))
        return 0 if result.get("ok") else 1

    if args.command == "summary":
        summary = get_feedback_summary()
        print(stable_json({"command": "summary", **summary}))
        return 0

    if args.command == "learn":
        result = learn_from_outcome(args.card_id)
        print(stable_json({"command": "learn", **result}))
        return 0 if result.get("ok") else 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
