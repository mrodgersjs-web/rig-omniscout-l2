"""Local Prefect flows for Automation Foundry shadow operation.

These flows are deliberately local-only. They make Prefect visible as the
cadence/flow surface without admitting outward effects or mutating providers.
"""

from __future__ import annotations

import sys
import time
from datetime import date, datetime, timezone
from typing import Any

from prefect import flow, get_run_logger, task
from prefect.context import get_run_context

from .fleet_health import run_fleet_health_shadow as collect_fleet_health_shadow
from .gtm_commercial_v2 import (
    ClaimEvidence,
    GtmCommercialRequest,
    GtmDecisionPolicy,
    OfferEvidence,
    OutcomeEvidence,
    OutcomeKind,
    TargetEvidence,
    VerifiedGtmSource,
    load_verified_gtm_source,
    run_gtm_commercial_strategy,
)
from .models import stable_hash
from .shadow_campaign import (
    DEFAULT_SHADOW_LEDGER,
    linkedin_workflow_contracts,
    record_shadow_slot,
    resolve_shadow_ledger_path,
    shadow_status,
)

LOCAL_PREFECT_FLOW_NAME = "foundry-commercial-shadow-minute"
_DIGEST = "a" * 64


def build_shadow_request(run_id: str) -> GtmCommercialRequest:
    """Build a deterministic no-effect sample request for local shadow proof."""

    return GtmCommercialRequest(
        run_id=run_id,
        observed_at=datetime(2026, 8, 5, 12, tzinfo=timezone.utc),
        claims=(
            ClaimEvidence(
                claim_id="claim-1",
                text="Named operator has a sourced market trigger.",
                source_ref="obsidian-gbrain-context:claim-1",
                source_hash=_DIGEST,
                verified=True,
            ),
        ),
        targets=(
            TargetEvidence(
                person_id="mike-approved-local-persona",
                firm_id="rig-local-shadow-firm",
                icp_band="A",
                person_fit=90,
                offer_fit={"req-read": 80},
                market_trigger_ref="local-shadow-trigger",
                evidence_refs=("obsidian-gbrain-context:claim-1",),
            ),
        ),
        offers=(
            OfferEvidence(
                offer_id="req-read",
                rung=0,
                active=True,
                local_artifact_ref="local://commercial-shadow/req-read",
                verified_claim_ids=("claim-1",),
            ),
        ),
        outcomes=(
            OutcomeEvidence(
                outcome_id="staged-0",
                kind=OutcomeKind.STAGED_REPLY,
                source_ref="local-shadow:staged-only",
                source_hash=_DIGEST,
                verified=True,
            ),
        ),
    )


@task(name="verify-commercial-source-hashes", retries=0)
def verify_commercial_source_hashes() -> dict[str, Any]:
    source = load_verified_gtm_source()
    return source.model_dump(mode="json")


@task(name="run-local-gtm-commercial-replay", retries=0)
def run_local_gtm_commercial_replay(
    *, run_id: str, source_payload: dict[str, Any]
) -> dict[str, Any]:
    source = VerifiedGtmSource.model_validate(source_payload)
    request = build_shadow_request(run_id)
    policy = GtmDecisionPolicy(decision_deadline=date(2026, 8, 6))
    receipt = run_gtm_commercial_strategy(request, policy, source)
    return receipt.model_dump(mode="json")


@task(name="project-visible-layer-state", retries=0)
def project_visible_layer_state(receipt: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    layers = {
        "mission_control": "READ_ONLY_PROJECTION",
        "hermes_conductor": "TYPED_ROUTE_DECLARED_NOT_AUTHORITY",
        "prefect": "LOCAL_FLOW_RUN_VISIBLE",
        "temporal": "NOT_INVOKED_NO_DURABLE_WAIT_IN_SAMPLE",
        "runtime_adapters": "LOCAL_PYTHON_ONLY_NO_PROVIDER_EFFECTS",
        "gev": "EXECUTOR_AND_VERIFIER_SEPARATED",
        "proofpacket": "SIGNED_LOCAL_PROOF_PRESENT",
        "postgres_minio": "TARGET_AUTHORITY_NOT_CONNECTED_IN_LOCAL_SAMPLE",
        "otel_grafana": "TARGET_OBSERVABILITY_NOT_CONNECTED_IN_LOCAL_SAMPLE",
    }
    return {
        "schema_version": "rig.foundry.prefect-shadow-minute.v1",
        "flow_name": LOCAL_PREFECT_FLOW_NAME,
        "production_admissible": False,
        "effect_attempts": receipt["effect_attempts"],
        "recommendation": receipt["decision"]["recommendation"],
        "source_artifact_hash": source["artifact_hash"],
        "receipt_hash": stable_hash(receipt),
        "layers": layers,
        "blocked_effects": [
            "send",
            "post",
            "direct_message",
            "ad_spend",
            "crm_write",
            "provider_write",
            "public_exposure",
        ],
    }


def _current_prefect_flow_run_id() -> str | None:
    try:
        context = get_run_context()
    except Exception:
        return None
    flow_run = getattr(context, "flow_run", None)
    flow_run_id = getattr(flow_run, "id", None)
    return str(flow_run_id) if flow_run_id else None


def _current_prefect_expected_start_time() -> str | None:
    """Return Prefect's logical scheduled time for the active flow run."""

    try:
        context = get_run_context()
    except Exception:
        return None
    flow_run = getattr(context, "flow_run", None)
    expected = getattr(flow_run, "expected_start_time", None)
    if expected is None:
        return None
    if isinstance(expected, datetime):
        return expected.astimezone(timezone.utc).isoformat()
    return str(expected)


def _wait_until_logical_due_time(due_at: str | None, *, max_wait_seconds: float = 5.0) -> None:
    """Prevent Prefect prefetch from evaluating a slot before it is due."""

    if not due_at:
        return
    scheduled = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    delay = (scheduled.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    if delay <= 0:
        return
    if delay > max_wait_seconds:
        raise RuntimeError("logical due time is outside the bounded Prefect prefetch window")
    time.sleep(delay)


@task(name="foundry-record-minute-shadow-slot", retries=0)
def record_minute_shadow_slot(
    ledger_path: str = str(DEFAULT_SHADOW_LEDGER),
    observed_at: str | None = None,
    due_at: str | None = None,
    prefect_flow_run_id: str | None = None,
) -> dict[str, Any]:
    resolved_ledger_path = resolve_shadow_ledger_path(ledger_path)
    receipt = record_shadow_slot(
        ledger_path=resolved_ledger_path,
        observed_at=observed_at,
        due_at=due_at,
        prefect_flow_run_id=prefect_flow_run_id or _current_prefect_flow_run_id(),
    )
    return receipt.model_dump(mode="json")


@task(name="foundry-read-shadow-status", retries=0)
def read_shadow_status(ledger_path: str = str(DEFAULT_SHADOW_LEDGER)) -> dict[str, Any]:
    return shadow_status(resolve_shadow_ledger_path(ledger_path))


@task(name="foundry-route-commercial-readiness-local-shadow", retries=0)
def route_commercial_readiness_local_shadow(
    *,
    evidence_root: str | None = None,
    observed_at: str | None = None,
    due_at: str | None = None,
    prefect_flow_run_id: str | None = None,
) -> dict[str, Any]:
    from .commercial_workflows import run_and_persist_cached_commercial_readiness

    run_id = "commercial-readiness-" + stable_hash(
        {
            "due_at": due_at,
            "observed_at": observed_at,
            "prefect_flow_run_id": prefect_flow_run_id,
        }
    )[:16]
    return run_and_persist_cached_commercial_readiness(
        evidence_root=evidence_root,
        run_id=run_id,
        due_at=due_at,
        observed_at=observed_at,
        prefect_flow_run_id=prefect_flow_run_id,
    )


@flow(name="foundry-minute-evaluation-shadow", log_prints=False)
def foundry_minute_evaluation_shadow(
    ledger_path: str = str(DEFAULT_SHADOW_LEDGER),
    observed_at: str | None = None,
    evidence_root: str | None = None,
) -> dict[str, Any]:
    logger = get_run_logger()
    prefect_flow_run_id = _current_prefect_flow_run_id()
    due_at = observed_at or _current_prefect_expected_start_time()
    if observed_at is None:
        _wait_until_logical_due_time(due_at)
    receipt = record_minute_shadow_slot(
        ledger_path=ledger_path,
        observed_at=observed_at,
        due_at=due_at,
        prefect_flow_run_id=prefect_flow_run_id,
    )
    status = read_shadow_status(ledger_path=ledger_path)
    if receipt["status"] == "DUPLICATE_SKIPPED":
        commercial_readiness = {
            "schema_version": "rig.foundry.commercial-readiness-route.v1",
            "route_status": "DUPLICATE_SKIPPED",
            "effect_attempts": 0,
            "production_admissible": False,
        }
    else:
        commercial_readiness = route_commercial_readiness_local_shadow(
            evidence_root=evidence_root,
            observed_at=observed_at,
            due_at=due_at,
            prefect_flow_run_id=prefect_flow_run_id,
        )
    logger.info(
        "Foundry shadow slot %s recorded as %s (%s/%s)",
        receipt["due_slot"],
        receipt["status"],
        status["unique_shadow_slots"],
        status["required_shadow_slots"],
    )
    return {
        "schema_version": "rig.foundry.prefect-shadow-flow-result.v1",
        "receipt": receipt,
        "status": status,
        "commercial_readiness": commercial_readiness,
        "linkedin_workflows": [
            item.model_dump(mode="json") for item in linkedin_workflow_contracts()
        ],
        "production_admissible": False,
    }


@flow(name=LOCAL_PREFECT_FLOW_NAME, log_prints=True)
def run_foundry_commercial_shadow_minute(
    run_id: str = "local-prefect-shadow-minute",
) -> dict[str, Any]:
    """Run one visible, effect-free commercial shadow minute under Prefect."""

    source = verify_commercial_source_hashes()
    receipt = run_local_gtm_commercial_replay(run_id=run_id, source_payload=source)
    projection = project_visible_layer_state(receipt, source)
    print(
        f"{LOCAL_PREFECT_FLOW_NAME}: {projection['recommendation']} "
        f"effects={projection['effect_attempts']}"
    )
    return projection


@task(name="foundry-collect-fleet-health-shadow", retries=0, timeout_seconds=45)
def collect_fleet_health_observation(
    *,
    evidence_root: str = "evidence/fleet/live",
    allow_tailscale_fallback: bool = False,
) -> dict[str, Any]:
    """Collect bounded cached fleet evidence; never execute workload payloads."""

    return collect_fleet_health_shadow(
        evidence_root=evidence_root,
        allow_tailscale_fallback=allow_tailscale_fallback,
        timeout_seconds=4,
    )


@flow(name="foundry-fleet-health-shadow", log_prints=False)
def foundry_fleet_health_shadow(
    evidence_root: str = "evidence/fleet/live",
    allow_tailscale_fallback: bool = False,
) -> dict[str, Any]:
    """Prefect-owned cadence for a read-only LAN-primary fleet observation."""

    logger = get_run_logger()
    receipt = collect_fleet_health_observation(
        evidence_root=evidence_root,
        allow_tailscale_fallback=allow_tailscale_fallback,
    )
    logger.info(
        "Fleet observation %s: %s/%s healthy; effects=%s",
        receipt["status"],
        receipt["healthy_nodes"],
        receipt["total_nodes"],
        receipt["effect_attempts"],
    )
    return receipt


def main() -> None:
    import json

    payload = run_foundry_commercial_shadow_minute()
    print(json.dumps(payload, indent=2, sort_keys=True))


@task(
    name="omniscout-run-bounded-stage",
    retries=3,
    retry_delay_seconds=[30, 120, 300],
    timeout_seconds=1800,
)
def run_omniscout_bounded_stage(stage: str) -> dict[str, Any]:
    """Execute one allowlisted OmniScout stage with durable receipts."""

    if stage not in {"collection", "direction", "briefing", "watchdog"}:
        raise ValueError("unknown OmniScout stage")
    # OmniScout is an optional commercial adapter. Keep its dependency outside
    # the control-plane import path so the minute evaluator remains available
    # when a client installs only the neutral/base runtime.
    from .omniscout_intelligence import run_stage

    return run_stage(stage)  # type: ignore[arg-type]


@task(
    name="omniscout-run-youtube-corpus-batch",
    retries=2,
    retry_delay_seconds=[120, 600],
    timeout_seconds=1800,
)
def run_youtube_corpus_batch(limit: int = 20) -> dict[str, Any]:
    from .omniscout_intelligence import youtube_corpus_batch

    return youtube_corpus_batch(limit)


@task(
    name="omniscout-converge-fleet-learning-release",
    retries=1,
    retry_delay_seconds=[300],
    timeout_seconds=900,
)
def run_fleet_learning_convergence() -> dict[str, Any]:
    from .omniscout_intelligence import (
        STATE_ROOT,
        append_ledger,
        atomic_json,
        parse_json_output,
        run_command,
        temporal_observe,
    )

    result = run_command(
        [sys.executable, "-m", "rig_foundry.fleet_learning_convergence"],
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"fleet learning convergence failed with exit {result.returncode}: "
            f"{result.stderr[-500:]}"
        )
    convergence = parse_json_output(result.stdout)
    payload = {"stage": "fleet-learning", "ok": convergence["status"] == "PASS", "convergence": convergence}
    record = append_ledger(payload)
    observation = temporal_observe("fleet-learning", record)
    output = {**payload, "record_hash": record["record_hash"], "temporal": observation}
    atomic_json(STATE_ROOT / "latest-fleet-learning-observation.json", output)
    return output


@task(
    name="omniscout-run-recall-corpus-batch",
    retries=0,
    timeout_seconds=1800,
)
def run_recall_corpus_batch(limit: int = 10) -> dict[str, Any]:
    from .omniscout_intelligence import recall_corpus_batch

    return recall_corpus_batch(limit)


@flow(name="omniscout-collection", log_prints=True)
def omniscout_collection() -> dict[str, Any]:
    return run_omniscout_bounded_stage("collection")


@flow(name="omniscout-direction", log_prints=True)
def omniscout_direction() -> dict[str, Any]:
    return run_omniscout_bounded_stage("direction")


@flow(name="omniscout-daily-briefing", log_prints=True)
def omniscout_daily_briefing() -> dict[str, Any]:
    return run_omniscout_bounded_stage("briefing")


@flow(name="omniscout-self-heal-watchdog", log_prints=True)
def omniscout_self_heal_watchdog() -> dict[str, Any]:
    return run_omniscout_bounded_stage("watchdog")


@flow(name="omniscout-youtube-corpus", log_prints=True)
def omniscout_youtube_corpus(limit: int = 20) -> dict[str, Any]:
    return run_youtube_corpus_batch(limit)


@flow(name="omniscout-fleet-learning-convergence", log_prints=True)
def omniscout_fleet_learning_convergence() -> dict[str, Any]:
    return run_fleet_learning_convergence()


@flow(name="omniscout-recall-corpus", log_prints=True)
def omniscout_recall_corpus(limit: int = 10) -> dict[str, Any]:
    return run_recall_corpus_batch(limit)



@task(
    name="omniscout-run-l2-build-cards",
    retries=0,
    timeout_seconds=3600,
)
def run_l2_build_cards(limit: int = 2, model: str | None = None) -> dict[str, Any]:
    from .omniscout_build_cards import run_l2_batch

    return run_l2_batch(limit=limit, model=model, use_consensus=True)


@flow(name="omniscout-l2-build-cards", log_prints=True)
def omniscout_l2_build_cards(limit: int = 2, model: str | None = None) -> dict[str, Any]:
    """Multi-source L2 build cards with TAC/RIG scoring + Consensus MCP."""
    return run_l2_build_cards(limit=limit, model=model)


@task(
    name="omniscout-run-l2-nightly",
    retries=0,
    timeout_seconds=7200,
)
def run_l2_nightly_pipeline(
    target: int = 100,
    model: str | None = None,
    push_app_recall: bool = True,
) -> dict[str, Any]:
    """Produce L2 build cards and push full content into Recall (local + app)."""
    import os
    from pathlib import Path

    # Prefer durable local control store (QNAP may be sandbox/permission flaky).
    os.environ.setdefault(
        "OMNISCOUT_L2_ROOT",
        str(Path.home() / ".rig" / "omniscout-control" / "build-cards"),
    )
    os.environ.setdefault("OMNISCOUT_L2_FAST", "1")
    os.environ.setdefault(
        "PATH",
        "/home/operator/.hermes/node/bin:/home/operator/.local/bin:"
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    )
    from .omniscout_l2_produce100 import run_nightly_l2_recall

    return run_nightly_l2_recall(
        target=target,
        model=model,
        push_app_recall=push_app_recall,
        fast=True,
    )


@flow(name="omniscout-l2-nightly", log_prints=True)
def omniscout_l2_nightly(
    target: int = 100,
    model: str | None = None,
    push_app_recall: bool = True,
) -> dict[str, Any]:
    """Midnight L2 build-card + Recall full-content pipeline (America/Denver)."""
    return run_l2_nightly_pipeline(
        target=target, model=model, push_app_recall=push_app_recall
    )

if __name__ == "__main__":
    main()
