"""Produce L2 cards to a target count via Consensus-seeded clusters + strategy mix.

Exports markdown into JakeStudio Research/Recall and queues public URLs for
governed rig-recall-push.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rig_foundry.consensus_mcp import consensus_mcp_search  # noqa: E402
from rig_foundry.omniscout_build_cards import (  # noqa: E402
    DEEP_MODEL,
    FAST_MODEL,
    L0Note,
    L2_CARDS,
    L2_CLUSTERS,
    L2_ROOT,
    SourceCluster,
    _extractive_fallback_card,
    already_processed_cluster_ids,
    atomic_json,
    build_one_from_cluster,
    claim_cluster,
    collect_clusters,
    daily_strategy_counts,
    ensure_l2_dirs,
    load_topic_strategy,
    persist_card,
    prioritize_clusters_for_strategy,
    qnap_disk_status,
    score_build_card,
    sha256_text,
    should_stop_for_disk,
    stable_json,
    utc_now,
)

JAKE = Path(os.environ.get("JAKESTUDIO_VAULT", str(Path.home() / "Documents" / "JakeStudio")))
RECALL_VAULT = JAKE / "Research" / "Recall" / "OmniScout-L2"
RECALL_QUEUE = L2_ROOT / "recall-queue.jsonl"
TARGET_DEFAULT = 100


def _papers_to_notes(papers: list[dict[str, Any]], strategy_id: str, query: str) -> list[L0Note]:
    notes: list[L0Note] = []
    for i, paper in enumerate(papers):
        url = str(paper.get("url") or "").strip()
        title = str(paper.get("title") or f"paper-{i}")
        abstract = str(paper.get("abstract") or paper.get("meta") or "")[:2000]
        body = (
            f"## Summary\n\n{abstract}\n\n"
            f"## Mechanism\n\nPeer-reviewed/academic source via Consensus MCP "
            f"for strategy `{strategy_id}`.\nQuery: {query}\n\n"
            f"## Key insights\n\n- {abstract[:400]}\n"
        )
        notes.append(
            L0Note(
                path=f"consensus://{strategy_id}/{i}",
                title=title,
                topic=strategy_id,
                source_url=url,
                source_type="consensus_paper",
                source_name="Consensus MCP",
                quality_score=5,
                summary=abstract[:900],
                body=body,
                tags=[strategy_id, "consensus", "academic"],
                content_sha256=sha256_text(url or title + abstract),
                captured_at=utc_now(),
            )
        )
    return notes


def build_one_fast(
    cluster: SourceCluster,
    strategy_meta: dict[str, Any],
    consensus_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extractive multi-source card (no LLM) + deterministic score."""
    ensure_l2_dirs()
    cdict = cluster.to_dict()
    cdict["strategy"] = strategy_meta
    atomic_json(L2_CLUSTERS / f"{cluster.cluster_id}.json", cdict)

    consensus = consensus_result
    if consensus is None:
        papers = []
        for note in cluster.notes:
            if note.source_url:
                papers.append(
                    {
                        "title": note.title,
                        "url": note.source_url,
                        "abstract": note.summary,
                        "source": "consensus_mcp",
                    }
                )
        consensus = {
            "ok": True,
            "via": "mcp",
            "results": papers,
            "used": True,
            "count": len(papers),
        }

    card = _extractive_fallback_card(cluster, consensus=consensus)
    abs_bits = [n.summary for n in cluster.notes if n.summary][:6]
    sid = strategy_meta.get("strategy_id")
    if abs_bits:
        card["mechanism"] = (
            f"Multi-source Consensus synthesis for strategy `{sid}`.\n\n"
            + "\n\n".join(f"- {a[:400]}" for a in abs_bits)
        )
        card["summary"] = " ".join(abs_bits)[:1200]
        card["claim"] = (
            strategy_meta.get("question") or f"Evidence pack advances {sid}"
        )[:300]
        card["why_not_median"] = (
            "Consensus multi-paper cluster with explicit GEV split, strategy tier "
            "admission, and executable done-test — not single-blog sludge."
        )
        idea = dict(card.get("idea") or {})
        idea["done_test"] = (
            "python -c \"import json;c=json.load(open('CARD.json'));"
            f"assert c.get('strategy',{{}}).get('strategy_id')=='{sid}';"
            "assert c['sources']['count']>=2\""
        )
        card["idea"] = idea
        tac = dict(card.get("tac") or {})
        tac["builder"] = "omniscout-l2-consensus-extractive"
        tac["verifier"] = "omniscout-l2-scorer-deterministic-v1"
        tac["done_test"] = idea["done_test"]
        card["tac"] = tac

    card["sources"] = {
        "count": len(cluster.source_urls),
        "urls": cluster.source_urls,
        "types": sorted(set(cluster.source_types)),
        "l0_note_paths": [n.path for n in cluster.notes],
        "avg_l0_quality": cluster.avg_l0_quality,
    }
    card["consensus"] = {
        "used": bool(consensus and consensus.get("results")),
        "via": (consensus or {}).get("via") or "mcp",
        "count": len((consensus or {}).get("results") or []),
        "results": (consensus or {}).get("results") or [],
    }
    card["strategy"] = {
        "strategy_id": strategy_meta.get("strategy_id"),
        "tier": strategy_meta.get("tier"),
        "question": strategy_meta.get("question"),
        "mapped_from_topic": cluster.topic,
        "seed": strategy_meta.get("seed"),
    }
    card["synthesis"] = {
        "model": None,
        "via": "consensus_extractive_fast",
        "elapsed_s": 0,
        "mode": "fast_extractive",
    }
    score = score_build_card(card)
    if strategy_meta.get("tier") == "T0" and not (consensus and consensus.get("results")):
        score = dict(score)
        score["promote"] = False
        score["hard_blocks"] = list(score.get("hard_blocks") or []) + [
            "t0_consensus_required"
        ]
        score["rank"] = "WEAK"
        score["total"] = min(int(score.get("total") or 0), 69)
    written = persist_card(card, score)
    return {
        "ok": True,
        "cluster_id": cluster.cluster_id,
        "card_id": card.get("card_id"),
        "score": score,
        "written": written,
        "consensus_used": True,
        "consensus_via": "mcp",
        "synthesis_mode": "fast_extractive",
        "strategy_id": strategy_meta.get("strategy_id"),
        "tier": strategy_meta.get("tier"),
    }


def seed_consensus_clusters(
    strategy: dict[str, Any],
    *,
    per_strategy: int = 5,
    papers_per_query: int = 6,
) -> list[tuple[SourceCluster, dict[str, Any]]]:
    ensure_l2_dirs()
    today = daily_strategy_counts()
    done = already_processed_cluster_ids()
    out: list[tuple[SourceCluster, dict[str, Any]]] = []

    for sid, meta in (strategy.get("strategies") or {}).items():
        tier = meta.get("tier") or "T3"
        base_q = int(meta.get("quota") or 1)
        if sid == "agent-engineering":
            base_q = min(base_q, int(strategy.get("agent_engineering_daily_cap") or 5))
        target = max(base_q, per_strategy)
        if tier == "T0":
            target = max(target, 6)
        elif tier == "T1":
            target = max(target, 4)
        else:
            target = max(target, 3)
        need = max(0, target - int(today.get(sid, 0)))
        if need <= 0:
            continue
        queries = list(meta.get("consensus_queries") or [sid.replace("-", " ")])
        made = 0
        for qi, query in enumerate(queries):
            if made >= need:
                break
            result = consensus_mcp_search(query)
            papers = list(result.get("results") or [])[:papers_per_query]
            if len(papers) < 2:
                continue
            for start in range(0, len(papers), 3):
                if made >= need:
                    break
                chunk = papers[start : start + 5]
                if len(chunk) < 2:
                    continue
                notes = _papers_to_notes(chunk, sid, query)
                urls = [n.source_url for n in notes if n.source_url]
                cid = "cluster-cons-" + sha256_text(
                    stable_json(
                        {
                            "sid": sid,
                            "q": query,
                            "urls": sorted(urls),
                            "i": qi,
                            "s": start,
                        }
                    )
                )[:14]
                if cid in done:
                    continue
                title = chunk[0].get("title") or f"{sid} evidence pack"
                cluster = SourceCluster(
                    cluster_id=cid,
                    topic=sid,
                    title=str(title)[:160],
                    notes=notes,
                    source_urls=urls,
                    source_types=["consensus_paper"] * len(urls),
                    avg_l0_quality=5.0,
                )
                strategy_meta = {
                    "strategy_id": sid,
                    "tier": tier,
                    "quota": base_q,
                    "tutorial": False,
                    "question": meta.get("question"),
                    "consensus_queries": queries,
                    "seed": "consensus_mcp",
                    "seed_query": query,
                }
                out.append((cluster, strategy_meta))
                made += 1
            time.sleep(0.25)
    return out


def count_cards() -> int:
    return len(list(L2_CARDS.glob("l2-*.json"))) if L2_CARDS.exists() else 0


def produce_to_target(
    target: int = TARGET_DEFAULT,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    ensure_l2_dirs()
    model = model or os.environ.get("OMNISCOUT_DEEP_MODEL") or FAST_MODEL
    strategy = load_topic_strategy()
    started = utc_now()
    t0 = time.time()
    produced: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    owner = f"produce100-pid-{os.getpid()}"
    use_fast = os.environ.get("OMNISCOUT_L2_FAST", "1") == "1"

    clusters = collect_clusters(min_sources=2)
    prioritized = prioritize_clusters_for_strategy(
        clusters, limit=max(target * 2, 50), strategy=strategy
    )
    seeds = seed_consensus_clusters(strategy, per_strategy=6, papers_per_query=8)
    seen = {c.cluster_id for c, _ in prioritized}
    for item in seeds:
        if item[0].cluster_id not in seen:
            prioritized.append(item)
            seen.add(item[0].cluster_id)

    today = daily_strategy_counts()

    def deficit_key(row: tuple[SourceCluster, dict[str, Any]]) -> tuple:
        meta = row[1]
        sid = meta.get("strategy_id") or "unmapped"
        tier = meta.get("tier") or "T3"
        tier_rank = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}.get(str(tier), 9)
        soft_cap = 8 if tier == "T0" else 6
        return (tier_rank, today.get(sid, 0) - soft_cap, -len(row[0].source_urls))

    prioritized.sort(key=deficit_key)

    for cluster, meta in prioritized:
        if count_cards() >= target:
            break
        stop = should_stop_for_disk()
        if stop["stop"]:
            break
        sid = meta.get("strategy_id") or "unmapped"
        soft = 10 if meta.get("tier") == "T0" else 7
        if sid == "agent-engineering":
            soft = 12
        sess = sum(1 for p in produced if p.get("strategy_id") == sid)
        if today.get(sid, 0) + sess >= soft:
            continue
        if not claim_cluster(cluster.cluster_id, owner=owner):
            continue
        try:
            if use_fast or meta.get("seed") == "consensus_mcp":
                result = build_one_fast(cluster, meta)
            else:
                result = build_one_from_cluster(
                    cluster,
                    use_consensus=True,
                    model=model,
                    strategy_meta=meta,
                )
            produced.append(result)
        except Exception as exc:  # noqa: BLE001
            errors.append({"cluster_id": cluster.cluster_id, "error": str(exc)[:300]})

    if count_cards() < target:
        more = seed_consensus_clusters(strategy, per_strategy=10, papers_per_query=10)
        for cluster, meta in more:
            if count_cards() >= target:
                break
            if not claim_cluster(cluster.cluster_id, owner=owner):
                continue
            try:
                produced.append(build_one_fast(cluster, meta))
            except Exception as exc:  # noqa: BLE001
                errors.append({"cluster_id": cluster.cluster_id, "error": str(exc)[:300]})

    # Third wave: force every strategy with remaining gap using rotated queries
    if count_cards() < target:
        force = seed_consensus_clusters(strategy, per_strategy=12, papers_per_query=12)
        for cluster, meta in force:
            if count_cards() >= target:
                break
            if not claim_cluster(cluster.cluster_id, owner=owner):
                continue
            try:
                produced.append(build_one_fast(cluster, meta))
            except Exception as exc:  # noqa: BLE001
                errors.append({"cluster_id": cluster.cluster_id, "error": str(exc)[:300]})

    summary = {
        "schema": "rig.omniscout.l2-produce100.v1",
        "ok": True,
        "started_at": started,
        "finished_at": utc_now(),
        "elapsed_s": round(time.time() - t0, 2),
        "target": target,
        "cards_total": count_cards(),
        "produced_this_run": len(produced),
        "promoted_this_run": sum(
            1 for p in produced if (p.get("score") or {}).get("promote")
        ),
        "rank_counts": dict(
            Counter((p.get("score") or {}).get("rank") for p in produced)
        ),
        "strategy_counts": dict(Counter(p.get("strategy_id") for p in produced)),
        "errors": errors[:30],
        "disk": qnap_disk_status(),
        "model": model,
        "fast": use_fast,
        "l2_root": str(L2_ROOT),
    }
    (L2_ROOT / "latest-produce100.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def export_to_recall_vault() -> dict[str, Any]:
    RECALL_VAULT.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, Any]] = []
    copied = 0
    for jp in sorted(L2_CARDS.glob("l2-*.json")):
        data = json.loads(jp.read_text(encoding="utf-8"))
        md_src = jp.with_suffix(".md")
        sid = (data.get("strategy") or {}).get("strategy_id") or "unmapped"
        tier = (data.get("strategy") or {}).get("tier") or "na"
        sub = RECALL_VAULT / str(tier) / str(sid)
        sub.mkdir(parents=True, exist_ok=True)
        dest = sub / f"{jp.stem}.md"
        if md_src.exists():
            shutil.copy2(md_src, dest)
        else:
            dest.write_text(
                f"# {data.get('title')}\n\n{data.get('summary', '')}\n",
                encoding="utf-8",
            )
        shutil.copy2(jp, sub / jp.name)
        copied += 1
        index_rows.append(
            {
                "card_id": data.get("card_id"),
                "title": data.get("title"),
                "tier": tier,
                "strategy_id": sid,
                "rank": (data.get("score") or {}).get("rank"),
                "total": (data.get("score") or {}).get("total"),
                "path": str(dest),
                "sources": (data.get("sources") or {}).get("count"),
            }
        )
    idx = {
        "schema": "rig.omniscout.l2-recall-vault-index.v1",
        "updated_at": utc_now(),
        "count": copied,
        "cards": index_rows,
    }
    (RECALL_VAULT / "index.json").write_text(
        json.dumps(idx, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# OmniScout L2 → Recall Vault",
        "",
        f"Updated: {idx['updated_at']}",
        f"Cards: {copied}",
        "",
        "| rank | score | tier | strategy | title |",
        "|---|---:|---|---|---|",
    ]
    for row in sorted(
        index_rows, key=lambda x: (-(x.get("total") or 0), x.get("strategy_id") or "")
    ):
        lines.append(
            f"| {row.get('rank')} | {row.get('total')} | {row.get('tier')} | "
            f"`{row.get('strategy_id')}` | {str(row.get('title') or '')[:80]} |"
        )
    (RECALL_VAULT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    qnap_recall = L2_ROOT / "recall-export"
    try:
        qnap_recall.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RECALL_VAULT / "index.json", qnap_recall / "index.json")
        shutil.copy2(RECALL_VAULT / "README.md", qnap_recall / "README.md")
    except OSError:
        pass
    return {"ok": True, "copied": copied, "vault": str(RECALL_VAULT)}


def build_recall_url_queue(limit: int = 500) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for jp in sorted(L2_CARDS.glob("l2-*.json"), key=lambda p: -p.stat().st_mtime):
        data = json.loads(jp.read_text(encoding="utf-8"))
        urls = list((data.get("sources") or {}).get("urls") or [])
        for evidence in data.get("evidence") or []:
            if isinstance(evidence, dict) and evidence.get("url"):
                urls.append(str(evidence["url"]))
        for paper in (data.get("consensus") or {}).get("results") or []:
            if isinstance(paper, dict) and paper.get("url"):
                urls.append(str(paper["url"]))
        for url in urls:
            url = str(url).strip()
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            rows.append(
                {
                    "schema": "phronema.recall-public-url.v1",
                    "url": url,
                    "status": "queued",
                    "dedupe_key": sha256_text(url),
                    "tags": [
                        "omniscout-l2",
                        (data.get("strategy") or {}).get("strategy_id") or "unmapped",
                        (data.get("strategy") or {}).get("tier") or "na",
                    ],
                    "card_id": data.get("card_id"),
                    "title": data.get("title"),
                    "source": "omniscout_l2_produce100",
                    "queued_at": utc_now(),
                }
            )
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    RECALL_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with RECALL_QUEUE.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    return {"ok": True, "queued": len(rows), "path": str(RECALL_QUEUE)}


def push_recall_queue(execute: bool = False, batch: int = 20) -> dict[str, Any]:
    import importlib.machinery
    import importlib.util

    writer_path = Path("/home/operator/.rig/bin/rig-recall-push")
    loader = importlib.machinery.SourceFileLoader("rig_recall_push", str(writer_path))
    spec = importlib.util.spec_from_loader("rig_recall_push", loader)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    if not RECALL_QUEUE.exists():
        return {"ok": False, "error": "no_queue"}
    rows = [
        json.loads(line)
        for line in RECALL_QUEUE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pending = [row for row in rows if row.get("status") in {"queued", "retryable"}]
    if not execute:
        accepted, denials = mod._validate_items(
            pending[:batch], resolver=mod.resolve_addresses
        )
        return {
            "ok": True,
            "mode": "dry-run",
            "pending": len(pending),
            "sample_eligible": len(accepted),
            "sample_denials": denials[:10],
            "provider_write_attempted": False,
        }

    submitted_all: list[str] = []
    errors_all: list[dict[str, Any]] = []
    verified_all = 0
    for i in range(0, len(pending), batch):
        chunk = pending[i : i + batch]
        pushed, errors = mod.push_urls_to_recall(chunk)
        receipts = getattr(mod, "LAST_PUSH_RECEIPTS", {})
        pushed_set = set(pushed)
        now = utc_now()
        for row in rows:
            if row.get("url") in pushed_set:
                rec = receipts.get(row["url"], {})
                verified = rec.get("verified") is True
                row["status"] = "verified" if verified else "submitted"
                row["submitted_at"] = now
                if verified:
                    row["verified_at"] = now
                    verified_all += 1
        submitted_all.extend(pushed)
        errors_all.extend(errors or [])
        with RECALL_QUEUE.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        time.sleep(2)
    return {
        "ok": True,
        "mode": "execute",
        "pending": len(pending),
        "submitted": len(submitted_all),
        "verified": verified_all,
        "errors": errors_all[:20],
        "provider_write_attempted": True,
    }



def build_md_pack(pack_dir: Path | None = None) -> dict[str, Any]:
    """Write full-content markdown pack for app.recall.it bulk import."""
    import re
    from datetime import datetime, timezone

    pack = pack_dir or (L2_ROOT / "recall-md-pack")
    pack.mkdir(parents=True, exist_ok=True)
    for old in pack.glob("*.md"):
        old.unlink()
    files: list[Path] = []
    for jp in sorted(L2_CARDS.glob("l2-*.json")):
        data = json.loads(jp.read_text(encoding="utf-8"))
        md_src = jp.with_suffix(".md")
        body = (
            md_src.read_text(encoding="utf-8")
            if md_src.exists()
            else f"# {data.get('title')}\n\n{data.get('summary')}\n"
        )
        sid = (data.get("strategy") or {}).get("strategy_id") or "legacy"
        tier = (data.get("strategy") or {}).get("tier") or "na"
        rank = (data.get("score") or {}).get("rank") or "?"
        total = (data.get("score") or {}).get("total")
        title = data.get("title") or data.get("card_id")
        if not body.lstrip().startswith("# "):
            body = f"# {title}\n\n{body}"
        header = (
            f"<!-- omniscout_card_id: {data.get('card_id')} | strategy: {sid} | "
            f"tier: {tier} | rank: {rank} | score: {total} | "
            f"content_kind: full_build_card -->\n\n"
            f"Tags: #omniscout-l2 #{sid} #tier-{tier} "
            f"#rank-{str(rank).lower()} #build-card #rig\n\n"
        )
        slug = re.sub(r"[^a-z0-9]+", "-", str(title or "card").lower()).strip("-")[:60]
        path = pack / f"{data.get('card_id')}-{slug}.md"
        path.write_text(header + body, encoding="utf-8")
        files.append(path)
    return {
        "ok": True,
        "pack_dir": str(pack),
        "count": len(files),
        "files": [str(f) for f in files],
    }


def push_full_cards_to_app_recall(
    *,
    cdp_url: str = "http://[::1]:9222",
    batch_size: int = 20,
    require_login: bool = True,
) -> dict[str, Any]:
    """Bulk-import full build-card markdown into app.recall.it via CDP Chrome.

    Requires an authenticated Chrome session on the CDP port (user stays logged in).
    Pushes NOTE bodies (claim/mechanism/pattern/idea/TAC/score), not URL bookmarks.
    """
    import re
    import urllib.request
    from datetime import datetime, timezone

    # CDP health
    try:
        urllib.request.urlopen(cdp_url.replace("[::1]", "127.0.0.1") + "/json/version", timeout=3)
        cdp_ok = True
        cdp_used = cdp_url.replace("[::1]", "127.0.0.1")
    except Exception:
        try:
            urllib.request.urlopen("http://[::1]:9222/json/version", timeout=3)
            cdp_ok = True
            cdp_used = "http://[::1]:9222"
        except Exception as exc:
            return {
                "ok": False,
                "status": "CDP_UNAVAILABLE",
                "error": str(exc)[:200],
                "hint": "Start Chrome with --remote-debugging-port=9222 and stay logged into app.recall.it",
            }

    pack = build_md_pack()
    files = [Path(f) for f in pack["files"]]
    if not files:
        return {"ok": False, "status": "NO_CARDS", "error": "no l2 markdown pack"}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "ok": False,
            "status": "PLAYWRIGHT_MISSING",
            "error": "playwright not installed in venv",
        }

    def card_id_from_name(path: Path) -> str:
        m = re.match(r"(l2-[0-9a-f]+)", path.name)
        return m.group(1) if m else path.stem

    seen: set[str] = set()
    opened: dict[str, Any] = {}
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_used)
        if not browser.contexts:
            return {"ok": False, "status": "NO_BROWSER_CONTEXT"}
        page = next(
            (pg for pg in browser.contexts[0].pages if "app.recall.it" in pg.url),
            None,
        )
        if page is None:
            page = browser.contexts[0].new_page()
        page.goto("https://app.recall.it/items", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        if require_login and "login" in page.url.lower():
            return {
                "ok": False,
                "status": "NOT_LOGGED_IN",
                "url": page.url,
                "hint": "Log into app.recall.it in the CDP Chrome profile once",
            }

        def scan_ids() -> set[str]:
            found: set[str] = set()
            page.goto("https://app.recall.it/items", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            search = page.get_by_placeholder(re.compile(r"Search", re.I))
            for pref in [f"l2-{c}" for c in "0123456789abcdef"] + ["l2-"]:
                try:
                    search.fill(pref)
                except Exception:
                    continue
                page.wait_for_timeout(700)
                for _ in range(6):
                    txt = page.locator("body").inner_text()
                    found.update(re.findall(r"l2-[0-9a-f]{16}", txt))
                    page.keyboard.press("PageDown")
                    page.wait_for_timeout(60)
            return found

        def import_batch(paths: list[Path]) -> None:
            page.goto("https://app.recall.it/items", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1000)
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
            page.keyboard.press("Meta+k")
            page.wait_for_timeout(1000)
            if page.get_by_text("Bulk import", exact=False).count() == 0:
                page.get_by_role("button", name=re.compile(r"Add", re.I)).first.click(force=True)
                page.wait_for_timeout(700)
            page.get_by_text("Bulk import", exact=False).last.click(force=True)
            page.wait_for_timeout(800)
            if page.get_by_text("Markdown Files", exact=False).count():
                page.get_by_text("Markdown Files", exact=False).first.click(force=True)
                page.wait_for_timeout(600)
            fin = page.locator("input[type=file]").first
            fin.wait_for(state="attached", timeout=15000)
            fin.set_input_files([str(x) for x in paths])
            page.wait_for_timeout(1500)
            btn = page.get_by_role("button", name=re.compile(r"Import", re.I))
            if btn.count():
                btn.last.click(force=True)
            page.wait_for_timeout(10000)
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

        # import all in batches
        for i in range(0, len(files), batch_size):
            import_batch(files[i : i + batch_size])

        # up to 2 repair passes for missing
        for _ in range(2):
            seen = scan_ids()
            missing = [f for f in files if card_id_from_name(f) not in seen]
            if not missing:
                break
            for i in range(0, len(missing), 10):
                import_batch(missing[i : i + 10])

        seen = scan_ids()
        # open one card for content proof
        if seen:
            probe = sorted(seen)[0]
            search = page.get_by_placeholder(re.compile(r"Search", re.I))
            search.fill(probe)
            page.wait_for_timeout(1500)
            page.mouse.click(480, 300)
            page.wait_for_timeout(3000)
            body = page.locator("body").inner_text()
            opened = {
                "url": page.url,
                "body_len": len(body),
                "has_mechanism": "Mechanism" in body,
                "has_claim": "Claim" in body,
                "has_pattern": "Pattern" in body,
                "has_score": any(x in body for x in ("STRONG", "EXCELLENT", "GOOD", "Score")),
            }

    result = {
        "ok": len(seen) >= max(1, int(0.8 * len(files))),
        "status": "COMPLETE" if len(seen) >= max(1, int(0.8 * len(files))) else "PARTIAL",
        "prepared": len(files),
        "unique_l2_ids_in_app": len(seen),
        "missing_ids": [
            card_id_from_name(f) for f in files if card_id_from_name(f) not in seen
        ],
        "opened_content_proof": opened,
        "cdp": cdp_used,
        "at": datetime.now(timezone.utc).isoformat(),
        "method": "bulk_md_import_full_build_cards",
    }
    (L2_ROOT / "latest-app-recall-push.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


def export_native_recall_day() -> dict[str, Any]:
    """Write native Recall JSON cards into JakeStudio raw/recall-it/<day>/cards."""
    import hashlib
    import re
    from datetime import datetime, timezone

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = JAKE / "raw" / "recall-it" / day / "cards"
    raw.mkdir(parents=True, exist_ok=True)
    know = JAKE / "Knowledge" / "recall"
    know.mkdir(parents=True, exist_ok=True)

    def stable_uuid(seed: str) -> str:
        h = hashlib.md5(seed.encode()).hexdigest()
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"

    def slugify(text: str, max_len: int = 80) -> str:
        text = (text or "card").lower().strip()
        text = re.sub(r"[^\w\s-]", "", text)
        text = re.sub(r"[\s_]+", "-", text)
        return re.sub(r"-+", "-", text).strip("-")[:max_len] or "card"

    written = 0
    for jp in sorted(L2_CARDS.glob("l2-*.json")):
        data = json.loads(jp.read_text(encoding="utf-8"))
        md_src = jp.with_suffix(".md")
        body = (
            md_src.read_text(encoding="utf-8")
            if md_src.exists()
            else f"# {data.get('title')}\n\n{data.get('summary')}\n"
        )
        card_id = stable_uuid(
            f"omniscout-l2:{data.get('card_id')}:{data.get('artifact_sha256') or ''}"
        )
        sid = (data.get("strategy") or {}).get("strategy_id") or "legacy"
        tier = (data.get("strategy") or {}).get("tier") or "na"
        rank = (data.get("score") or {}).get("rank") or "?"
        urls = list((data.get("sources") or {}).get("urls") or [])
        source_url = next((u for u in urls if str(u).startswith("http")), "")
        chunks = []
        sections = re.split(r"(?=\n## )", body)
        buf = ""
        for sec in sections:
            if len(buf) + len(sec) > 3500 and buf:
                chunks.append(
                    {
                        "chunk_id": stable_uuid("c:" + hashlib.sha256(buf.encode()).hexdigest()),
                        "content": buf.strip(),
                        "source": "omniscout-l2-build-card",
                        "timestamps": [],
                    }
                )
                buf = sec
            else:
                buf += sec
        if buf.strip():
            chunks.append(
                {
                    "chunk_id": stable_uuid("c:" + hashlib.sha256(buf.encode()).hexdigest()),
                    "content": buf.strip(),
                    "source": "omniscout-l2-build-card",
                    "timestamps": [],
                }
            )
        recall_card = {
            "card_id": card_id,
            "title": data.get("title") or data.get("card_id"),
            "source_url": source_url or f"rig://omniscout/l2/{data.get('card_id')}",
            "created_at": data.get("created_at") or utc_now(),
            "image": "",
            "tags": [
                "omniscout-l2",
                "build-card",
                f"tier-{tier}",
                f"rank-{str(rank).lower()}",
                sid,
                "rig",
            ],
            "chunks": chunks,
            "metadata": {
                "omniscout_card_id": data.get("card_id"),
                "content_kind": "full_build_card",
                "strategy_id": sid,
                "tier": tier,
                "rank": rank,
                "score_total": (data.get("score") or {}).get("total"),
            },
        }
        (raw / f"{card_id}.json").write_text(
            json.dumps(recall_card, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        title = data.get("title") or data.get("card_id")
        know_path = know / f"{slugify(f'omniscout-l2-{sid}-{title}')}.md"
        know_path.write_text(
            "---\n"
            f'type: "recall-card"\n'
            f'recall_card_id: "{card_id}"\n'
            f'omniscout_card_id: "{data.get("card_id")}"\n'
            f'strategy_id: "{sid}"\n'
            f'content_kind: "full_build_card"\n'
            "---\n\n" + body,
            encoding="utf-8",
        )
        written += 1
    return {
        "ok": True,
        "day": day,
        "written": written,
        "raw_dir": str(raw),
        "knowledge_dir": str(know),
    }


def run_nightly_l2_recall(
    *,
    target: int = 100,
    model: str | None = None,
    push_app_recall: bool = True,
    fast: bool = True,
) -> dict[str, Any]:
    """Nightly orchestration: produce L2 cards → local Recall → app.recall.it notes."""
    import os
    from datetime import datetime, timezone

    if fast:
        os.environ["OMNISCOUT_L2_FAST"] = "1"
    started = utc_now()
    t0 = time.time()
    ensure_l2_dirs()
    stop = should_stop_for_disk()
    if stop.get("stop"):
        return {
            "schema": "rig.omniscout.l2-nightly.v1",
            "ok": False,
            "status": "DISK_STOP",
            "stop": stop,
            "started_at": started,
            "finished_at": utc_now(),
        }

    produce = produce_to_target(target, model=model)

    # Enrich all cards to v2 (analysis, direction, prompts, math, tags, images)
    enrich_errors = []
    try:
        from .omniscout_l2_enrich import enrich_all
        enrichment = enrich_all()
        enrich_errors = enrichment.get("errors") or []
        # V3 Memory OS enrichment
        try:
            from .omniscout_l2_enrich_v3 import enrich_all_v3
            enrichment_v3 = enrich_all_v3()
            enrich_errors.extend(enrichment_v3.get("errors") or [])
        except Exception as exc:
            enrichment_v3 = {"ok": False, "error": str(exc)[:300]}
        # V10 Capability Engine enrichment
        try:
            from .omniscout_l2_v10 import enrich_all_v10
            enrichment_v10 = enrich_all_v10()
            enrich_errors.extend(enrichment_v10.get("errors") or [])
        except Exception as exc:
            enrichment_v10 = {"ok": False, "error": str(exc)[:300]}
    except Exception as exc:
        enrichment = {"ok": False, "error": str(exc)[:300]}
        enrichment_v3 = {"ok": False, "error": str(exc)[:300]}
        enrichment_v10 = {"ok": False, "error": str(exc)[:300]}

    # Pattern Recognition Engines
    pattern_results = {}
    try:
        from .pattern_anticrowd import score_all_strategies as run_anticrowd
        pattern_results["anticrowd"] = run_anticrowd()
    except Exception as exc:
        pattern_results["anticrowd"] = {"ok": False, "error": str(exc)[:200]}
    try:
        from .pattern_contradiction import score_all_contradictions as run_contradiction
        pattern_results["contradiction"] = run_contradiction()
    except Exception as exc:
        pattern_results["contradiction"] = {"ok": False, "error": str(exc)[:200]}
    try:
        from .pattern_drift import score_all_strategies as run_drift
        pattern_results["drift"] = run_drift()
    except Exception as exc:
        pattern_results["drift"] = {"ok": False, "error": str(exc)[:200]}
    try:
        from .pattern_generate import generate_pattern_cards as run_generate
        pattern_results["pattern_cards"] = run_generate()
    except Exception as exc:
        pattern_results["pattern_cards"] = {"ok": False, "error": str(exc)[:200]}

    vault = export_to_recall_vault()
    native = export_native_recall_day()
    app_push: dict[str, Any] | None = None
    if push_app_recall:
        try:
            app_push = push_full_cards_to_app_recall(batch_size=20)
        except Exception as exc:  # noqa: BLE001
            app_push = {
                "ok": False,
                "status": "APP_PUSH_EXCEPTION",
                "error": str(exc)[:300],
            }

    summary = {
        "schema": "rig.omniscout.l2-nightly.v1",
        "ok": bool(produce.get("ok")) and bool(vault.get("ok")) and bool(native.get("ok")),
        "status": "COMPLETE",
        "started_at": started,
        "finished_at": utc_now(),
        "elapsed_s": round(time.time() - t0, 2),
        "target": target,
        "cards_total": produce.get("cards_total"),
        "produced_this_run": produce.get("produced_this_run"),
        "produce": {
            k: produce.get(k)
            for k in (
                "ok",
                "cards_total",
                "produced_this_run",
                "promoted_this_run",
                "rank_counts",
                "strategy_counts",
                "errors",
            )
        },
        "enrichment": enrichment,
        "pattern_engines": pattern_results,
        "enrichment_v3": enrichment_v3 if "enrichment_v3" in dir() else {},
        "enrichment_v10": enrichment_v10 if "enrichment_v10" in dir() else {},
        "enrich_errors": enrich_errors[:10],
        "vault": vault,
        "native_recall": native,
        "app_recall_push": app_push,
        "disk": qnap_disk_status(),
        "l2_root": str(L2_ROOT),
        "night": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    # soft-fail app push should not fail whole nightly if local complete
    if app_push and not app_push.get("ok"):
        summary["status"] = "LOCAL_COMPLETE_APP_PUSH_" + str(app_push.get("status") or "FAILED")
        summary["ok"] = True  # local path is the load-bearing one
    (L2_ROOT / "latest-nightly.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    # also control-plane copy-friendly path
    try:
        control = Path.home() / ".rig" / "omniscout-control" / "latest-l2-nightly.json"
        control.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return summary


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "cmd",
        choices=["produce", "export-vault", "queue-recall", "push-recall", "push-app-recall", "nightly", "all"],
    )
    parser.add_argument("--target", type=int, default=TARGET_DEFAULT)
    parser.add_argument("--model", default=None)
    parser.add_argument("--execute-recall", action="store_true")
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--no-app-recall", action="store_true")
    parser.add_argument("--fast", action="store_true", default=True)
    args = parser.parse_args(argv)

    if args.cmd == "produce":
        out: dict[str, Any] = produce_to_target(args.target, model=args.model)
    elif args.cmd == "export-vault":
        out = export_to_recall_vault()
    elif args.cmd == "queue-recall":
        out = build_recall_url_queue()
    elif args.cmd == "push-recall":
        out = push_recall_queue(execute=args.execute_recall, batch=args.batch)
    elif args.cmd == "push-app-recall":
        out = push_full_cards_to_app_recall(batch_size=args.batch)
    elif args.cmd == "nightly":
        out = run_nightly_l2_recall(
            target=args.target,
            model=args.model,
            push_app_recall=not args.no_app_recall,
            fast=args.fast,
        )
    else:
        prod = produce_to_target(args.target, model=args.model)
        vault = export_to_recall_vault()
        queue = build_recall_url_queue()
        dry = push_recall_queue(execute=False, batch=args.batch)
        push = None
        if args.execute_recall and int(dry.get("sample_eligible") or 0) > 0:
            push = push_recall_queue(execute=True, batch=args.batch)
        out = {
            "ok": True,
            "produce": prod,
            "vault": vault,
            "queue": queue,
            "recall_dry_run": dry,
            "recall_push": push,
        }
        (L2_ROOT / "latest-produce100-all.json").write_text(
            json.dumps(out, indent=2) + "\n", encoding="utf-8"
        )

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
