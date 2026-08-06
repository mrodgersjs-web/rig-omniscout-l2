"""OmniScout L8 Context Delivery Packet generator.

Reads V20 build cards and produces ta[REDACTED] L8 packets that let fleet agents
hydrate context from a card before acting.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
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

SCHEMA_L8 = "rig.l8-context-packet.v1"
CARD_SCHEMA = "rig.omniscout.build-card.v20"
PACKETS_DIR = L2_ROOT / "l8-packets"

HYDRATION_TARGETS = ["terminal-agent", "desktop-agent", "fleet-node", "lobehub"]

_SECRET_PATTERNS = [
    re.compile(r"[REDACTED][a-zA-Z0-9]{20,}", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"password\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"api[_-]?key\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"token\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN (RSA |OPENSSH |PGP )?PRIVATE KEY-----"),
]


def _card_path(card_id: str) -> Path:
    return L2_CARDS / f"{card_id}.json"


def _built_path(card_id: str) -> Path:
    return L2_ROOT / "built" / card_id


def _openspec_path(card_id: str) -> Path:
    return _built_path(card_id) / "openspec"


def _content_path(card_id: str) -> Path:
    return L2_ROOT / "content" / card_id


def _obsidian_path(card: dict[str, Any]) -> str:
    target = card.get("world_model", {}).get("obsidian_target")
    if target:
        return str(target)
    strategy_id = _strategy_id(card)
    return f"Documents/JakeStudio/Capabilities/{strategy_id}/{card.get('card_id', 'unknown')}.md"


def _strategy_id(card: dict[str, Any]) -> str:
    return str((card.get("strategy") or {}).get("strategy_id") or "unmapped").lower()


def _tier(card: dict[str, Any]) -> str:
    return str((card.get("strategy") or {}).get("tier") or "na").upper()


def _strategy_question(card: dict[str, Any]) -> str:
    return str((card.get("strategy") or {}).get("question") or card.get("claim", ""))


def _score_value(card: dict[str, Any]) -> int:
    return int((card.get("score") or {}).get("total") or 0)


def _rank(card: dict[str, Any]) -> str:
    return str((card.get("score") or {}).get("rank") or "UNKNOWN")


def _task_hash(card: dict[str, Any]) -> str:
    claim = card.get("claim", "")
    strategy = stable_json(card.get("strategy") or {})
    return sha256_text(f"{claim}\n{strategy}")


def _task_excerpt(card: dict[str, Any], length: int = 200) -> str:
    claim = card.get("claim", "")
    return claim[:length]


def _summary_excerpt(card: dict[str, Any], length: int = 500) -> str:
    summary = card.get("summary", "")
    return summary[:length]


def _reverify_by(card: dict[str, Any]) -> str:
    return str((card.get("temporal_validity") or {}).get("reverify_by") or "")


def _temporal_freshness(card: dict[str, Any]) -> dict[str, Any]:
    return dict(card.get("temporal_validity") or {})


def _council_verdict(card: dict[str, Any]) -> dict[str, Any]:
    council = card.get("council") or {}
    synthesis = council.get("synthesis") or {}
    return {
        "overall_verdict": synthesis.get("overall_verdict", "UNKNOWN"),
        "confidence_score": synthesis.get("confidence_score", 0),
        "vote_tally": synthesis.get("vote_tally", {}),
    }


def _proof_sealed(card: dict[str, Any]) -> bool:
    autobuilt = card.get("autobuilt") or {}
    if autobuilt.get("proof_sealed") is not None:
        return bool(autobuilt["proof_sealed"])
    proof_seal = card.get("proof_seal") or {}
    return bool(proof_seal.get("verifiable", False))


def _card_created_at(card: dict[str, Any]) -> str:
    return str(card.get("created_at") or card.get("enriched_at") or "")


def _has_secrets(text_blob: str) -> bool:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text_blob):
            return True
    return False


def _text_blob_for_redaction(card: dict[str, Any]) -> str:
    parts = [
        str(card.get("claim", "")),
        str(card.get("summary", "")),
        str(card.get("mechanism", "")),
        stable_json(card.get("pattern") or {}),
        stable_json(card.get("idea") or {}),
        stable_json(card.get("gtm_strategy") or {}),
        stable_json(card.get("business_intelligence") or {}),
    ]
    return "\n".join(parts)


def _is_fresh(card: dict[str, Any]) -> bool:
    validity = card.get("temporal_validity") or {}
    if validity.get("freshness") == "STALE":
        return False
    reverify = validity.get("reverify_by") or ""
    if reverify:
        try:
            reverify_dt = datetime.strptime(str(reverify)[:7], "%Y-%m")
            now = datetime.now(timezone.utc)
            return reverify_dt.year > now.year or (
                reverify_dt.year == now.year and reverify_dt.month >= now.month
            )
        except ValueError:
            return True
    return True


def build_packet(card_path: str | Path) -> dict[str, Any]:
    """Read a V20 build card and write an L8 context packet.

    Returns the packet dict and writes it deterministically to
    L2_ROOT/l8-packets/{card_id}.json.
    """
    card_path = Path(card_path)
    card = json.loads(card_path.read_text(encoding="utf-8"))
    card_id = str(card.get("card_id") or card_path.stem)

    strategy_id = _strategy_id(card)
    tier = _tier(card)
    rank = _rank(card)
    score = _score_value(card)

    autobuilt_path = _built_path(card_id)
    openspec_path = _openspec_path(card_id)
    content_path = _content_path(card_id)

    packet: dict[str, Any] = {
        "schema": SCHEMA_L8,
        "card_id": card_id,
        "task_hash": _task_hash(card),
        "task_excerpt": _task_excerpt(card),
        "sources": {
            "obsidian_path": _obsidian_path(card),
            "gbrain_derived": {
                "strategy_id": strategy_id,
                "tier": tier,
                "rank": rank,
                "score": score,
            },
            "card_schema": CARD_SCHEMA,
            "autobuilt_path": str(autobuilt_path.relative_to(L2_ROOT)) + "/" if autobuilt_path.exists() else None,
            "openspec_path": str(openspec_path.relative_to(L2_ROOT)) + "/" if openspec_path.exists() else None,
            "content_path": str(content_path.relative_to(L2_ROOT)) + "/" if content_path.exists() else None,
        },
        "freshness": {
            "card_created_at": _card_created_at(card),
            "temporal_freshness": _temporal_freshness(card),
            "reverify_by": _reverify_by(card),
        },
        "gate_d_boundary": {
            "public_action_required": False,
            "proof_sealed": _proof_sealed(card),
            "council_verdict": _council_verdict(card),
        },
        "hydration_targets": HYDRATION_TARGETS,
        "readback_receipts": {},
        "memory_card": {
            "title": card.get("title", card_id),
            "summary": _summary_excerpt(card),
            "tags": card.get("tags", []),
            "strategy": strategy_id,
            "tier": tier,
        },
        "built_at": utc_now(),
    }

    # Run gates and attach results.
    text_blob = _text_blob_for_redaction(card)
    packet["gates"] = {
        "source_presence": {
            "passed": bool(
                packet["sources"]["obsidian_path"]
                and packet["sources"]["gbrain_derived"]
                and card_path.exists()
                and autobuilt_path.exists()
            ),
            "required_sources": [
                "obsidian_path",
                "gbrain_derived",
                "card_path",
                "autobuilt_path",
            ],
        },
        "freshness": {
            "passed": _is_fresh(card),
            "reverify_by": packet["freshness"]["reverify_by"],
        },
        "redaction": {
            "passed": not _has_secrets(text_blob),
            "secret_patterns_checked": len(_SECRET_PATTERNS),
        },
    }

    PACKETS_DIR.mkdir(parents=True, exist_ok=True)
    packet_path = PACKETS_DIR / f"{card_id}.json"
    atomic_json(packet_path, packet)
    return packet


def build_all_packets() -> dict[str, Any]:
    """Build L8 context packets for all V20 cards in L2_CARDS."""
    cards = sorted(L2_CARDS.glob("l2-*.json"))
    packets: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for card_path in cards:
        try:
            packet = build_packet(card_path)
            packets.append({"card_id": packet["card_id"], "path": str(L2_ROOT / "l8-packets" / f"{packet['card_id']}.json"), "gates": packet["gates"]})
        except Exception as exc:  # noqa: BLE001
            failed.append({"card_id": card_path.stem, "error": str(exc)})

    return {
        "schema": SCHEMA_L8,
        "generated_at": utc_now(),
        "total_cards": len(cards),
        "packet_count": len(packets),
        "failed_count": len(failed),
        "packets": packets,
        "failed": failed,
    }


def _verify_packet(packet_path: str | Path) -> dict[str, Any]:
    packet_path = Path(packet_path)
    if not packet_path.exists():
        return {"path": str(packet_path), "valid": False, "error": "packet not found"}

    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"path": str(packet_path), "valid": False, "error": f"invalid JSON: {exc}"}

    required = {
        "schema",
        "card_id",
        "task_hash",
        "task_excerpt",
        "sources",
        "freshness",
        "gate_d_boundary",
        "hydration_targets",
        "readback_receipts",
        "memory_card",
        "built_at",
        "gates",
    }
    missing = required - set(packet.keys())
    if missing:
        return {"path": str(packet_path), "valid": False, "error": f"missing fields: {sorted(missing)}"}

    gates = packet.get("gates", {})
    gate_results = {
        name: result.get("passed", False)
        for name, result in gates.items()
    }
    all_passed = all(gate_results.values())

    return {
        "path": str(packet_path),
        "valid": True,
        "card_id": packet.get("card_id"),
        "all_gates_passed": all_passed,
        "gates": gate_results,
    }


def _status() -> dict[str, Any]:
    cards = list(L2_CARDS.glob("l2-*.json"))
    packets = list(PACKETS_DIR.glob("l2-*.json"))
    return {
        "schema": SCHEMA_L8,
        "l2_root": str(L2_ROOT),
        "cards_dir": str(L2_CARDS),
        "packets_dir": str(PACKETS_DIR),
        "card_count": len(cards),
        "packet_count": len(packets),
    }


def _cmd_one(card_path: str) -> dict[str, Any]:
    return build_packet(card_path)


def _cmd_all() -> dict[str, Any]:
    return build_all_packets()


def _cmd_verify(packet_path: str) -> dict[str, Any]:
    return _verify_packet(packet_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniScout L8 Context Delivery Packet generator")
    sub = parser.add_subparsers(dest="command")

    one_p = sub.add_parser("one", help="Build a single L8 packet from a card path")
    one_p.add_argument("path", help="Path to a V20 build card JSON file")

    sub.add_parser("all", help="Build L8 packets for all V20 cards")
    sub.add_parser("status", help="Show L8 packet pipeline status")

    verify_p = sub.add_parser("verify", help="Verify an existing L8 packet")
    verify_p.add_argument("path", help="Path to an L8 packet JSON file")

    args = parser.parse_args(argv)

    if args.command == "one":
        out = _cmd_one(args.path)
    elif args.command == "all":
        out = _cmd_all()
    elif args.command == "status":
        out = _status()
    elif args.command == "verify":
        out = _cmd_verify(args.path)
    else:
        parser.print_help()
        return 1

    print(stable_json(out))
    return 0 if out.get("valid", True) is not False and out.get("failed_count", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
