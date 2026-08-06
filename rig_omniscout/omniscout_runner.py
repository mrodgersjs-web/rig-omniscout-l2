"""Persistent Prefect runner for OmniScout schedules."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from prefect import serve
from prefect.deployments.runner import RunnerDeployment
from prefect.schedules import Cron

from .prefect_flows import (
    omniscout_collection,
    omniscout_daily_briefing,
    omniscout_direction,
    omniscout_fleet_learning_convergence,
    omniscout_l2_build_cards,
    omniscout_l2_nightly,
    omniscout_recall_corpus,
    omniscout_self_heal_watchdog,
    omniscout_youtube_corpus,
)


async def _await_deployment(
    deployment: Awaitable[RunnerDeployment],
) -> RunnerDeployment:
    return await deployment


def _resolve_deployment(
    deployment: RunnerDeployment | Awaitable[RunnerDeployment],
) -> RunnerDeployment:
    if isinstance(deployment, RunnerDeployment):
        return deployment
    return asyncio.run(_await_deployment(deployment))


def main() -> None:
    deployments: list[RunnerDeployment] = [
        _resolve_deployment(omniscout_collection.to_deployment(
            name="rig-36gb-quarter-hour",
            interval=900,
            concurrency_limit=1,
            tags=["rig", "omniscout", "36gb", "collection"],
        )),
        _resolve_deployment(omniscout_direction.to_deployment(
            name="rig-topic-rotation-six-hour",
            schedule=Cron("17 */6 * * *", timezone="America/Denver"),
            concurrency_limit=1,
            tags=["rig", "omniscout", "direction"],
        )),
        _resolve_deployment(omniscout_daily_briefing.to_deployment(
            name="rig-daily-top-ten",
            schedule=Cron("30 6 * * *", timezone="America/Denver"),
            concurrency_limit=1,
            tags=["rig", "omniscout", "briefing"],
        )),
        _resolve_deployment(omniscout_self_heal_watchdog.to_deployment(
            name="rig-five-minute-watchdog",
            interval=300,
            concurrency_limit=1,
            tags=["rig", "omniscout", "self-heal"],
        )),
        _resolve_deployment(omniscout_youtube_corpus.to_deployment(
            name="rig-youtube-corpus-quarter-hour",
            parameters={"limit": 5},
            interval=900,
            concurrency_limit=1,
            tags=["rig", "omniscout", "youtube", "transcripts"],
        )),
        _resolve_deployment(omniscout_fleet_learning_convergence.to_deployment(
            name="rig-fleet-learning-hourly",
            interval=3600,
            concurrency_limit=1,
            tags=["rig", "omniscout", "fleet", "skills", "doctrine"],
        )),
        _resolve_deployment(omniscout_l2_build_cards.to_deployment(
            name="rig-l2-build-cards-half-hour",
            parameters={"limit": 2, "model": None},
            interval=1800,
            concurrency_limit=1,
            tags=["rig", "omniscout", "l2", "build-cards", "consensus"],
        )),
        _resolve_deployment(omniscout_l2_nightly.to_deployment(
            name="rig-l2-nightly-midnight",
            parameters={"target": 100, "model": None, "push_app_recall": True},
            schedule=Cron("5 0 * * *", timezone="America/Denver"),
            concurrency_limit=1,
            tags=["rig", "omniscout", "l2", "nightly", "recall", "build-cards"],
        )),
        _resolve_deployment(omniscout_recall_corpus.to_deployment(
            name="rig-recall-corpus-quarter-hour",
            parameters={"limit": 10},
            interval=900,
            concurrency_limit=1,
            tags=["rig", "omniscout", "recall", "derived-index"],
        )),
    ]
    # Keep long transcript and browser-ingest lanes from starving the watchdog,
    # collection, or fleet-convergence control lanes. Per-deployment limits and
    # the Recall filesystem lease still enforce one writer per mutable surface.
    serve(*deployments, pause_on_shutdown=False, limit=4)


if __name__ == "__main__":
    main()
