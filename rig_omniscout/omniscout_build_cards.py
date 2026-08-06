"""OmniScout L2 true build-card pipeline.

Multi-source clusters → deep synthesis → deterministic TAC/RIG doctrine score.
Consensus evidence via MCP (Claude/Hermes OAuth) — no API key required.
Runs until QNAP free-space pressure or STOP file.

Quality ranks (composite 0-100, A1 scorer — no LLM in rank path):
  REJECT    <55   sludge / single-source / no mechanism
  WEAK    55-69   multi-source but thin
  GOOD    70-84   promotion-eligible feeder (default floor)
  STRONG  85-93   doctrine-candidate
  EXCELLENT 94+   public / ProofPacket grade

Dimensions (weights = 100):
  multi_source 18 | mechanism_density 18 | evidence_anchoring 14
  tac_structure 12 | doctrine_fit 12 | novelty_pattern 10
  actionability 8 | gev_separation 8
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------

HOME = Path.home()
CONTROL = Path(os.environ.get("OMNISCOUT_CONTROL", str(HOME / ".rig" / "omniscout-control")))
JAKESTUDIO = Path(os.environ.get("JAKESTUDIO_VAULT", str(HOME / "Documents" / "JakeStudio")))
L0_RESEARCH = JAKESTUDIO / "Research"
def _default_l2_root() -> Path:
    """Prefer QNAP when writable; else control-plane local store."""
    env = os.environ.get("OMNISCOUT_L2_ROOT")
    if env:
        return Path(env)
    qnap = Path("/Volumes/RIGLake/RIG/omniscout/build-cards")
    try:
        qnap.mkdir(parents=True, exist_ok=True)
        probe = qnap / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return qnap
    except OSError:
        local = Path.home() / ".rig" / "omniscout-control" / "build-cards"
        local.mkdir(parents=True, exist_ok=True)
        return local


L2_ROOT = _default_l2_root()
L2_CARDS = L2_ROOT / "cards"
L2_CLUSTERS = L2_ROOT / "clusters"
L2_QUARANTINE = L2_ROOT / "quarantine"
L2_RUNS = L2_ROOT / "runs"
L2_LEDGER = L2_ROOT / "ledger.jsonl"
L2_STATE = L2_ROOT / "state.json"
L2_INDEX = L2_ROOT / "index.json"
L2_STOP = L2_ROOT / "STOP"
SCORE_RUBRIC_PATH = L2_ROOT / "SCORE_RUBRIC.md"

REMOTE_HOST = os.environ.get("OMNISCOUT_REMOTE_HOST", "rig36gb")
REMOTE_L0 = os.environ.get(
    "OMNISCOUT_REMOTE_L0", "/Users/rig36gb/Documents/JakeStudio/Research"
)
REMOTE_OLLAMA = os.environ.get("OMNISCOUT_REMOTE_OLLAMA", "http://127.0.0.1:11434")
DEEP_MODEL = os.environ.get("OMNISCOUT_DEEP_MODEL", "qwen3-coder:30b")
FAST_MODEL = os.environ.get("OMNISCOUT_FAST_MODEL", "qwen3:8b")

QNAP_MIN_FREE_GIB = float(os.environ.get("OMNISCOUT_QNAP_MIN_FREE_GIB", "100"))
QNAP_MAX_USED_PCT = float(os.environ.get("OMNISCOUT_QNAP_MAX_USED_PCT", "95"))
TARGET_L2_PER_DAY = int(os.environ.get("OMNISCOUT_TARGET_L2_PER_DAY", "60"))
MIN_SOURCES = int(os.environ.get("OMNISCOUT_MIN_SOURCES", "3"))
GOOD_FLOOR = int(os.environ.get("OMNISCOUT_GOOD_FLOOR", "70"))
CLUSTER_LOOKBACK_DAYS = int(os.environ.get("OMNISCOUT_CLUSTER_LOOKBACK_DAYS", "14"))

SCHEMA_CARD = "rig.omniscout.build-card.v1"
SCHEMA_CLUSTER = "rig.omniscout.source-cluster.v1"
SCHEMA_RUN = "rig.omniscout.l2-run.v1"
SCHEMA_SCORE = "rig.omniscout.build-card-score.v1"
SCHEMA_CAMPAIGN = "rig.omniscout.l2-campaign.v1"

DOCTRINE_DOMAINS: dict[str, list[str]] = {
    "engineering-capability": [
        "proof", "done", "gate", "gev", "test", "harness", "agent", "determin",
        "false-done", "verification", "regression",
    ],
    "app-building": [
        "build-card", "slice", "frontend", "backend", "ux", "release", "factory",
        "scaffold", "app",
    ],
    "linkedin-studio": ["linkedin", "content", "voice", "claim map", "calendar", "post"],
    "communications": ["messaging", "clarity", "channel", "audience", "follow-up"],
    "rig-mesh": ["fleet", "mesh", "lan", "node", "qnap", "worker", "routing", "ollama"],
    "rig-ide": ["session", "command", "doctrine", "repo-specific", "coding agent"],
    "leadership": ["decision", "accountab", "team", "sustain", "operator"],
    "forecasting": ["forecast", "calibration", "brier", "experiment", "causal"],
    "competitive-intel": ["market", "competitor", "positioning", "strategy", "gtm", "icp"],
    "founder-performance": ["deep work", "adhd", "habit", "energy", "cognitive"],
    "ai-business-models": ["saas", "pricing", "vertical", "unit economic", "monetiz"],
    "scraping-intelligence": ["scrape", "crawl", "rss", "transcript", "corpus", "ingest"],
    "knowledge-memory": ["memory", "rag", "vector", "obsidian", "gbrain", "recall"],
    "agentic-coding": [
        "tac", "agentic", "tool use", "subagent", "worktree", "loop",
        "builder", "verifier", "closed-loop",
    ],
}

TAC_MARKERS = [
    "done-test", "done test", "proofpacket", "proof packet", "closed loop",
    "closed-loop", "builder", "verifier", "core four", "scout", "worker",
    "synthesizer", "executable", "regression", "fail closed", "fail-closed",
    "artifact", "mechanism", "gate",
]

MECHANISM_MARKERS = [
    "because", "mechanism", "how it works", "pipeline", "step", "algorithm",
    "invariant", "contract", "when x", "if ", "then ", "causes", "leads to",
    "tradeoff", "constraint",
]

SCORE_RUBRIC_MD = """# OmniScout L2 Build-Card Score Rubric

**Schema:** `rig.omniscout.build-card-score.v1`
**Scorer:** deterministic A1 (no LLM in rank decision) — TAC v2 + RIG doctrines
**GEV:** synthesizer builds; `omniscout-l2-scorer-deterministic-v1` grades

## Ranks

| Rank | Score | Meaning |
|------|------:|---------|
| REJECT | < 55 | sludge / single-source / no mechanism |
| WEAK | 55–69 | multi-source but thin |
| GOOD | 70–84 | promotion-eligible feeder (default floor) |
| STRONG | 85–93 | doctrine-candidate grade |
| EXCELLENT | 94+ | public / ProofPacket grade |

## Dimensions (weights = 100)

| Dim | Wt | What good looks like |
|-----|---:|----------------------|
| multi_source | 18 | ≥3 independent source domains (Consensus MCP boosts) |
| mechanism_density | 18 | how-it-works, steps, invariants — not adjectives |
| evidence_anchoring | 14 | URLs + numbers + quotes |
| tac_structure | 12 | Core Four / closed-loop / done-test shape |
| doctrine_fit | 12 | maps to RIG doctrine domains |
| novelty_pattern | 10 | named pattern + anti-trigger + why-not-median |
| actionability | 8 | idea slice + next actions + acceptance |
| gev_separation | 8 | builder ≠ verifier identities |

## Hard blocks

- `<2 independent sources` → cap 54 (REJECT)
- mechanism missing/thin (<40 chars) → cap 54
- done-test missing → cap 69 (WEAK max)

## Promote rule

`rank in {GOOD,STRONG,EXCELLENT}` AND no hard block on sources/mechanism.

## Consensus path

- **MCP** (`mcp-remote` → `https://mcp.consensus.app/mcp`) — OAuth session shared with Claude/Hermes
- Tool: `search(query)` only (no limit arg)
- Used to enrich multi-source clusters with peer-reviewed papers

## TAC v2 closed loop

L0 collect → cluster (≥3 sources) → L2 synthesize (36GB deep model) → deterministic score → promote/quarantine → ProofPacket daily
"""


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(stable_json(row) + "\n")


def slugify(value: str, max_len: int = 64) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (s or "topic")[:max_len]


def ensure_l2_dirs() -> None:
    for p in (L2_ROOT, L2_CARDS, L2_CLUSTERS, L2_QUARANTINE, L2_RUNS):
        p.mkdir(parents=True, exist_ok=True)
    if not SCORE_RUBRIC_PATH.exists():
        atomic_text(SCORE_RUBRIC_PATH, SCORE_RUBRIC_MD)


# ---------------------------------------------------------------------------
# QNAP free-space gate
# ---------------------------------------------------------------------------


def qnap_disk_status(path: Path | None = None) -> dict[str, Any]:
    target = path or L2_ROOT
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.exists():
        return {
            "ok": False,
            "reason": "path_missing",
            "path": str(target),
            "free_gib": 0.0,
            "used_pct": 100.0,
            "stop": True,
        }
    try:
        st = os.statvfs(str(probe))
    except OSError as exc:
        return {
            "ok": False,
            "reason": f"statvfs_error:{exc}",
            "path": str(probe),
            "free_gib": 0.0,
            "used_pct": 100.0,
            "stop": True,
        }
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - free
    free_gib = free / (1024**3)
    used_pct = (used / total * 100.0) if total else 100.0
    stop = free_gib < QNAP_MIN_FREE_GIB or used_pct >= QNAP_MAX_USED_PCT
    return {
        "ok": True,
        "path": str(probe),
        "total_gib": round(total / (1024**3), 2),
        "free_gib": round(free_gib, 2),
        "used_pct": round(used_pct, 2),
        "min_free_gib": QNAP_MIN_FREE_GIB,
        "max_used_pct": QNAP_MAX_USED_PCT,
        "stop": stop,
        "reason": "pressure" if stop else "ok",
    }


def should_stop_for_disk() -> dict[str, Any]:
    if L2_STOP.exists():
        return {"stop": True, "reason": "manual_STOP_file", "disk": qnap_disk_status()}
    disk = qnap_disk_status()
    return {"stop": bool(disk.get("stop")), "reason": disk.get("reason"), "disk": disk}


# ---------------------------------------------------------------------------
# L0 notes
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
_KV_RE = re.compile(r"^([A-Za-z0-9_\-]+):\s*(.*)$", re.M)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in m.group(1).splitlines():
        if line.strip().startswith("- ") and current_list_key:
            out.setdefault(current_list_key, [])
            if isinstance(out[current_list_key], list):
                out[current_list_key].append(line.strip()[2:].strip().strip('"'))
            continue
        km = _KV_RE.match(line)
        if not km:
            continue
        key, raw = km.group(1), km.group(2).strip()
        current_list_key = None
        if raw in {"", "|"}:
            current_list_key = key
            out[key] = []
            continue
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            out[key] = raw[1:-1]
            continue
        try:
            out[key] = int(raw)
        except ValueError:
            try:
                out[key] = float(raw)
            except ValueError:
                if raw.lower() in {"true", "false"}:
                    out[key] = raw.lower() == "true"
                else:
                    out[key] = raw
    return out


@dataclass
class L0Note:
    path: str
    title: str
    topic: str
    source_url: str
    source_type: str
    source_name: str
    quality_score: int
    summary: str
    body: str
    tags: list[str] = field(default_factory=list)
    content_sha256: str = ""
    captured_at: str = ""


def load_l0_notes(
    *,
    roots: list[Path] | None = None,
    lookback_days: int = CLUSTER_LOOKBACK_DAYS,
    min_quality: int = 0,
) -> list[L0Note]:
    roots = roots or [L0_RESEARCH]
    cutoff = time.time() - lookback_days * 86400
    notes: list[L0Note] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) < 80:
                continue
            fm = _parse_frontmatter(text)
            body = _FRONTMATTER_RE.sub("", text, count=1).strip()
            sm = re.search(r"##\s+Summary\s*\n+(.+?)(?:\n##|\Z)", body, re.S | re.I)
            summary = (sm.group(1).strip() if sm else body)[:1200]
            try:
                q = int(fm.get("quality_score", fm.get("quality", 0)) or 0)
            except (TypeError, ValueError):
                q = 0
            if q < min_quality:
                continue
            csha = str(fm.get("content_sha256") or sha256_text(text))
            if csha in seen:
                continue
            seen.add(csha)
            tags = fm.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            notes.append(
                L0Note(
                    path=str(path),
                    title=str(fm.get("title") or path.stem).strip(),
                    topic=str(
                        fm.get("focus_topic") or fm.get("topic") or path.parent.name
                    ).strip(),
                    source_url=str(fm.get("source_url") or "").strip(),
                    source_type=str(fm.get("source_type") or "article"),
                    source_name=str(fm.get("source_name") or ""),
                    quality_score=q,
                    summary=summary,
                    body=body[:6000],
                    tags=[str(t) for t in tags][:20],
                    content_sha256=csha,
                    captured_at=str(fm.get("captured_at") or ""),
                )
            )
    return notes


def _pull_remote_l0_index() -> list[dict[str, Any]]:
    script = r"""
import json, re, time
from pathlib import Path
root = Path(%r)
cutoff = time.time() - %d * 86400
fm_re = re.compile(r'^---\n(.*?)\n---\n', re.S)
kv_re = re.compile(r'^([A-Za-z0-9_\-]+):\s*(.*)$', re.M)
out = []
for p in root.rglob('*.md'):
    try:
        if p.stat().st_mtime < cutoff: continue
        t = p.read_text(encoding='utf-8', errors='replace')
    except Exception:
        continue
    m = fm_re.match(t)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            km = kv_re.match(line)
            if km:
                k,v = km.group(1), km.group(2).strip().strip('"').strip("'")
                fm[k]=v
    body = fm_re.sub('', t, count=1).strip()
    sm = re.search(r'##\s+Summary\s*\n+(.+?)(?:\n##|\Z)', body, re.S|re.I)
    summary = (sm.group(1).strip() if sm else body)[:900]
    try:
        q = int(fm.get('quality_score') or fm.get('quality') or 0)
    except Exception:
        q = 0
    out.append({
        'path': str(p),
        'title': fm.get('title') or p.stem,
        'topic': fm.get('focus_topic') or fm.get('topic') or p.parent.name,
        'source_url': fm.get('source_url') or '',
        'source_type': fm.get('source_type') or 'article',
        'source_name': fm.get('source_name') or '',
        'quality_score': q,
        'summary': summary,
        'body': body[:4000],
        'tags': [],
        'content_sha256': fm.get('content_sha256') or '',
        'captured_at': fm.get('captured_at') or '',
    })
print(json.dumps(out))
""" % (REMOTE_L0, CLUSTER_LOOKBACK_DAYS)
    try:
        r = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
                REMOTE_HOST, "python3", "-c", script,
            ],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return [{"_error": str(exc)}]
    if r.returncode != 0:
        return [{"_error": (r.stderr or r.stdout)[:400]}]
    try:
        data = json.loads(r.stdout)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return [{"_error": "bad_json"}]


def merge_remote_notes(local: list[L0Note]) -> list[L0Note]:
    remote = _pull_remote_l0_index()
    if remote and isinstance(remote[0], dict) and remote[0].get("_error"):
        return local
    seen = {n.content_sha256 or n.source_url or n.path for n in local}
    for row in remote:
        if not isinstance(row, dict):
            continue
        key = row.get("content_sha256") or row.get("source_url") or row.get("path")
        if not key or key in seen:
            continue
        seen.add(str(key))
        try:
            q = int(row.get("quality_score") or 0)
        except (TypeError, ValueError):
            q = 0
        local.append(
            L0Note(
                path=str(row.get("path") or ""),
                title=str(row.get("title") or ""),
                topic=str(row.get("topic") or ""),
                source_url=str(row.get("source_url") or ""),
                source_type=str(row.get("source_type") or "article"),
                source_name=str(row.get("source_name") or ""),
                quality_score=q,
                summary=str(row.get("summary") or ""),
                body=str(row.get("body") or ""),
                tags=list(row.get("tags") or []),
                content_sha256=str(row.get("content_sha256") or ""),
                captured_at=str(row.get("captured_at") or ""),
            )
        )
    return local


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with",
        "is", "are", "this", "that", "from", "by", "as", "at", "be", "it", "its",
        "into", "about", "how", "what", "when", "your", "you", "we", "our",
        "their", "can", "will", "using", "use", "used",
    }
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower()) if t not in stop}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


@dataclass
class SourceCluster:
    cluster_id: str
    topic: str
    title: str
    notes: list[L0Note]
    source_urls: list[str]
    source_types: list[str]
    avg_l0_quality: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_CLUSTER,
            "cluster_id": self.cluster_id,
            "topic": self.topic,
            "title": self.title,
            "source_count": len(self.source_urls),
            "source_urls": self.source_urls,
            "source_types": sorted(set(self.source_types)),
            "avg_l0_quality": self.avg_l0_quality,
            "note_paths": [n.path for n in self.notes],
            "note_titles": [n.title for n in self.notes],
        }


def cluster_notes(
    notes: list[L0Note],
    *,
    min_sources: int = MIN_SOURCES,
    similarity: float = 0.18,
) -> list[SourceCluster]:
    by_topic: dict[str, list[L0Note]] = defaultdict(list)
    for n in notes:
        by_topic[slugify(n.topic or "untagged")].append(n)

    clusters: list[SourceCluster] = []
    for topic_key, group in by_topic.items():
        by_url: dict[str, L0Note] = {}
        no_url: list[L0Note] = []
        for n in sorted(group, key=lambda x: -x.quality_score):
            if n.source_url:
                by_url.setdefault(n.source_url, n)
            else:
                no_url.append(n)
        unique = list(by_url.values()) + no_url
        if not unique:
            continue
        tokens = [_tokenize(f"{n.title} {n.summary} {' '.join(n.tags)}") for n in unique]
        assigned = [False] * len(unique)
        for i, n in enumerate(unique):
            if assigned[i]:
                continue
            members = [n]
            assigned[i] = True
            for j in range(i + 1, len(unique)):
                if assigned[j]:
                    continue
                sim = _jaccard(tokens[i], tokens[j])
                if sim + 0.12 >= similarity or (
                    unique[j].topic == n.topic and len(members) < min_sources
                ):
                    if unique[j].source_url and unique[j].source_url in {
                        m.source_url for m in members if m.source_url
                    }:
                        assigned[j] = True
                        continue
                    members.append(unique[j])
                    assigned[j] = True
            urls = [m.source_url for m in members if m.source_url]
            domains = set()
            for u in urls:
                try:
                    domains.add(urllib.parse.urlparse(u).netloc.lower())
                except Exception:
                    pass
            independent = max(len(domains), len(urls))
            avg_q = sum(m.quality_score for m in members) / max(1, len(members))
            if independent < min_sources and not (
                independent >= 2 and avg_q >= 3
            ):
                if independent < 2:
                    continue
            title = max(members, key=lambda m: (m.quality_score, len(m.summary))).title
            cid = "cluster-" + sha256_text(
                stable_json(
                    {
                        "topic": topic_key,
                        "urls": sorted(urls),
                        "titles": sorted(m.title for m in members),
                    }
                )
            )[:16]
            clusters.append(
                SourceCluster(
                    cluster_id=cid,
                    topic=members[0].topic or topic_key,
                    title=title,
                    notes=members,
                    source_urls=urls,
                    source_types=[m.source_type for m in members],
                    avg_l0_quality=round(avg_q, 2),
                )
            )
    clusters.sort(key=lambda c: (-len(c.source_urls), -c.avg_l0_quality, c.topic))
    return clusters


# ---------------------------------------------------------------------------
# Consensus (MCP first, API key fallback)
# ---------------------------------------------------------------------------


def consensus_search(query: str, *, limit_hint: int = 5) -> dict[str, Any]:
    """Prefer Consensus MCP (OAuth). Fall back to REST if CONSENSUS_API_KEY set."""
    # MCP path
    try:
        from .consensus_mcp import consensus_mcp_search

        result = consensus_mcp_search(query)
        if result.get("ok") and result.get("results"):
            # trim to limit_hint for card size
            result = dict(result)
            result["results"] = list(result["results"])[:limit_hint]
            result["count"] = len(result["results"])
            return result
        if result.get("ok") is False and result.get("reason") not in {
            "timeout",
        }:
            # still return so caller sees reason; try API next
            mcp_result = result
        else:
            mcp_result = result
    except Exception as exc:  # noqa: BLE001
        mcp_result = {
            "ok": False,
            "enabled": True,
            "via": "mcp",
            "reason": str(exc)[:200],
            "results": [],
        }

    # REST fallback
    api_key = os.environ.get("CONSENSUS_API_KEY") or ""
    if not api_key:
        return mcp_result if mcp_result.get("results") is not None else {
            "ok": False,
            "enabled": bool(mcp_result.get("enabled")),
            "via": mcp_result.get("via") or "none",
            "reason": mcp_result.get("reason") or "no_mcp_results_no_api_key",
            "results": [],
        }

    import urllib.error
    import urllib.request

    url = (
        "https://api.consensus.app/papers/search?"
        + urllib.parse.urlencode({"query": query, "size": limit_hint, "page": 1})
    )
    try:
        req = urllib.request.Request(
            url,
            headers={
                "x-api-key": api_key,
                "Accept": "application/json",
                "User-Agent": "rig-omniscout-l2/1.0",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        raw_papers = data.get("papers") or data.get("results") or data.get("data") or []
        papers = []
        for item in raw_papers[:limit_hint]:
            if not isinstance(item, dict):
                continue
            papers.append(
                {
                    "title": item.get("title") or "",
                    "url": item.get("url") or item.get("doi_url") or "",
                    "year": item.get("year"),
                    "abstract": (item.get("abstract") or "")[:1200],
                    "citation_count": item.get("citation_count") or item.get("citations"),
                    "source": "consensus_api",
                }
            )
        return {
            "ok": True,
            "enabled": True,
            "via": "api",
            "count": len(papers),
            "results": papers,
        }
    except Exception as exc:  # noqa: BLE001
        if mcp_result.get("results"):
            return mcp_result
        return {
            "ok": False,
            "enabled": True,
            "via": "api",
            "reason": str(exc)[:200],
            "results": [],
        }


# ---------------------------------------------------------------------------
# Deep synthesis (36GB Ollama)
# ---------------------------------------------------------------------------


def _ollama_generate(
    prompt: str,
    *,
    model: str = DEEP_MODEL,
    num_predict: int = 1800,
    temperature: float = 0.2,
    timeout: int = 600,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": num_predict},
    }
    body = json.dumps(payload)
    local_base = os.environ.get("OMNISCOUT_OLLAMA_URL")
    if local_base:
        import urllib.request

        try:
            req = urllib.request.Request(
                local_base.rstrip("/") + "/api/generate",
                data=body.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return {
                "ok": True,
                "response": data.get("response") or "",
                "model": model,
                "via": "local",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"local_ollama:{exc}", "response": ""}

    remote_cmd = (
        f"curl -s -m {max(30, timeout - 10)} {REMOTE_OLLAMA}/api/generate "
        "-H 'Content-Type: application/json' --data-binary @-"
    )
    try:
        r = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
                REMOTE_HOST, remote_cmd,
            ],
            input=body, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "error": f"ssh:{exc}", "response": ""}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or r.stdout)[:300], "response": ""}
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad_json", "response": r.stdout[:300]}
    return {
        "ok": True,
        "response": data.get("response") or "",
        "model": model,
        "via": "ssh",
        "eval_count": data.get("eval_count"),
    }


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            return val
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        chunk = text[start : end + 1]
        try:
            val = json.loads(chunk)
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            fixed = re.sub(r",\s*}", "}", chunk)
            fixed = re.sub(r",\s*]", "]", fixed)
            try:
                val = json.loads(fixed)
                if isinstance(val, dict):
                    return val
            except json.JSONDecodeError:
                return None
    return None


def _infer_doctrine_domains(text: str) -> list[str]:
    low = text.lower()
    hits = []
    for domain, kws in DOCTRINE_DOMAINS.items():
        score = sum(1 for k in kws if k in low)
        if score >= 1:
            hits.append((score, domain))
    hits.sort(reverse=True)
    return [d for _, d in hits[:4]] or ["engineering-capability"]


def synthesize_build_card(
    cluster: SourceCluster,
    *,
    consensus: dict[str, Any] | None = None,
    model: str = DEEP_MODEL,
) -> dict[str, Any]:
    sources_block = []
    for i, n in enumerate(cluster.notes[:8], 1):
        sources_block.append(
            f"SOURCE {i}\n"
            f"title: {n.title}\n"
            f"url: {n.source_url}\n"
            f"type: {n.source_type}\n"
            f"l0_quality: {n.quality_score}\n"
            f"summary: {n.summary[:700]}\n"
            f"body_excerpt: {n.body[:1200]}\n"
        )
    consensus_block = ""
    if consensus and consensus.get("results"):
        lines = []
        for i, r in enumerate(consensus["results"][:5], 1):
            lines.append(
                f"PAPER {i}: {r.get('title')} ({r.get('year')}) {r.get('url')}\n"
                f"abstract: {r.get('abstract')}"
            )
        consensus_block = "CONSENSUS PAPERS\n" + "\n".join(lines)

    prompt = f"""You are the RIG OmniScout L2 synthesizer.
Build ONE true multi-source BUILD CARD from the sources below.

Hard rules (TAC v2 + RIG doctrine):
- Mechanism over adjectives. Explain HOW it works.
- Separate builder work from verifier work (GEV).
- Include an executable done-test (real command/check, not "looks good").
- Extract a reusable PATTERN and a concrete IDEA / build slice.
- Cite sources by URL. No invented citations.
- Reject generic LLM sludge. Prefer non-median, operator-usable claims.
- Map to RIG doctrine domains when fit is real.

Return ONLY valid JSON with this schema:
{{
  "title": "string",
  "topic": "string",
  "claim": "one sharp falsifiable claim",
  "summary": "150-250 words, multi-source synthesis",
  "mechanism": "how it works, steps/invariants",
  "pattern": {{
    "name": "short pattern name",
    "description": "reusable pattern",
    "when_to_use": "trigger conditions",
    "when_not_to_use": "anti-triggers"
  }},
  "idea": {{
    "name": "build slice name",
    "description": "what to build",
    "acceptance": "observable acceptance",
    "done_test": "executable check"
  }},
  "tac": {{
    "core_four": ["intent","context","tools","loop"],
    "closed_loop": "builder -> verifier flow",
    "builder": "who builds",
    "verifier": "who verifies (must differ)",
    "done_test": "executable"
  }},
  "doctrine_domains": ["engineering-capability"],
  "evidence": [
    {{"url":"","quote_or_fact":"","source_type":""}}
  ],
  "risks": ["..."],
  "assumptions": ["..."],
  "next_actions": ["..."],
  "kill_criteria": ["..."],
  "why_not_median": "what makes this non-generic"
}}

TOPIC: {cluster.topic}
SEED TITLE: {cluster.title}

{chr(10).join(sources_block)}

{consensus_block}
"""
    t0 = time.time()
    gen = _ollama_generate(prompt, model=model, num_predict=2200, temperature=0.15)
    elapsed = round(time.time() - t0, 2)
    if not gen.get("ok"):
        return _extractive_fallback_card(cluster, consensus=consensus, error=gen.get("error"))

    parsed = _extract_json_object(gen.get("response") or "")
    if not parsed:
        gen2 = _ollama_generate(
            prompt + "\n\nIMPORTANT: Output JSON only. No prose before or after.",
            model=FAST_MODEL,
            num_predict=1600,
            temperature=0.1,
            timeout=300,
        )
        elapsed = round(time.time() - t0, 2)
        parsed = _extract_json_object(gen2.get("response") or "")
        gen = gen2 if gen2.get("ok") else gen
        if not parsed:
            return _extractive_fallback_card(
                cluster,
                consensus=consensus,
                error="json_parse_failed",
                raw=(gen.get("response") or "")[:1500],
            )

    return {
        "schema": SCHEMA_CARD,
        "card_id": "l2-" + sha256_text(cluster.cluster_id + utc_now())[:16],
        "cluster_id": cluster.cluster_id,
        "created_at": utc_now(),
        "topic": parsed.get("topic") or cluster.topic,
        "title": parsed.get("title") or cluster.title,
        "claim": parsed.get("claim") or "",
        "summary": parsed.get("summary") or "",
        "mechanism": parsed.get("mechanism") or "",
        "pattern": parsed.get("pattern") or {},
        "idea": parsed.get("idea") or {},
        "tac": parsed.get("tac") or {},
        "doctrine_domains": parsed.get("doctrine_domains") or [],
        "evidence": parsed.get("evidence") or [],
        "risks": parsed.get("risks") or [],
        "assumptions": parsed.get("assumptions") or [],
        "next_actions": parsed.get("next_actions") or [],
        "kill_criteria": parsed.get("kill_criteria") or [],
        "why_not_median": parsed.get("why_not_median") or "",
        "sources": {
            "count": len(cluster.source_urls),
            "urls": cluster.source_urls,
            "types": sorted(set(cluster.source_types)),
            "l0_note_paths": [n.path for n in cluster.notes],
            "avg_l0_quality": cluster.avg_l0_quality,
        },
        "consensus": {
            "used": bool(consensus and consensus.get("results")),
            "via": (consensus or {}).get("via"),
            "count": len((consensus or {}).get("results") or []),
            "results": (consensus or {}).get("results") or [],
        },
        "synthesis": {
            "model": gen.get("model") or model,
            "via": gen.get("via"),
            "elapsed_s": elapsed,
            "mode": "llm",
        },
    }


def _extractive_fallback_card(
    cluster: SourceCluster,
    *,
    consensus: dict[str, Any] | None = None,
    error: str | None = None,
    raw: str | None = None,
) -> dict[str, Any]:
    tops = cluster.notes[:5]
    evidence = []
    for n in tops:
        if n.source_url:
            evidence.append(
                {
                    "url": n.source_url,
                    "quote_or_fact": (n.summary or n.title)[:280],
                    "source_type": n.source_type,
                }
            )
    for r in (consensus or {}).get("results") or []:
        if r.get("url"):
            evidence.append(
                {
                    "url": r["url"],
                    "quote_or_fact": (r.get("abstract") or r.get("title") or "")[:280],
                    "source_type": "consensus_paper",
                }
            )
    mechanism_bits = []
    for n in tops:
        for line in (n.body or "").splitlines():
            low = line.lower()
            if any(m in low for m in ("step", "because", "pipeline", "how ")):
                mechanism_bits.append(line.strip())
            if len(mechanism_bits) >= 8:
                break
    summary = " ".join((n.summary or n.title) for n in tops)[:1200]
    return {
        "schema": SCHEMA_CARD,
        "card_id": "l2-" + sha256_text(cluster.cluster_id + "fallback" + utc_now())[:16],
        "cluster_id": cluster.cluster_id,
        "created_at": utc_now(),
        "topic": cluster.topic,
        "title": cluster.title,
        "claim": f"Multi-source cluster on '{cluster.topic}' yields an actionable operator pattern.",
        "summary": summary,
        "mechanism": "\n".join(mechanism_bits) or summary[:500],
        "pattern": {
            "name": f"{slugify(cluster.topic)}-multi-source-synthesis",
            "description": "Cluster ≥3 independent sources on one RIG topic; extract mechanism + done-test.",
            "when_to_use": "When L0 notes share a topic and at least two domains/URLs.",
            "when_not_to_use": "Single blog sludge or uncited opinion.",
        },
        "idea": {
            "name": f"build-{slugify(cluster.title)[:40]}",
            "description": f"Turn the '{cluster.title}' cluster into a governed build slice with proof.",
            "acceptance": "Card has ≥3 sources, mechanism, pattern, idea, and executable done-test.",
            "done_test": (
                "python -c \"import json;c=json.load(open('CARD.json'));"
                "assert c['sources']['count']>=2\""
            ),
        },
        "tac": {
            "core_four": ["intent", "context", "tools", "loop"],
            "closed_loop": (
                "L0 collector builds notes; L2 synthesizer drafts card; "
                "independent scorer grades artifact."
            ),
            "builder": "omniscout-l2-synthesizer",
            "verifier": "omniscout-l2-scorer (deterministic, separate identity)",
            "done_test": "score.rank in {GOOD,STRONG,EXCELLENT} AND sources.count >= 2",
        },
        "doctrine_domains": _infer_doctrine_domains(
            f"{cluster.topic} {cluster.title} {summary}"
        ),
        "evidence": evidence,
        "risks": [
            "LLM synthesis unavailable; extractive fallback may under-specify mechanism."
        ],
        "assumptions": ["Source notes are accurate enough to cluster."],
        "next_actions": [
            "Re-run with deep model when Ollama slot free.",
            "Confirm Consensus MCP papers attached.",
        ],
        "kill_criteria": ["If independent sources drop below 2, quarantine."],
        "why_not_median": "Multi-source + explicit GEV split + executable done-test required.",
        "sources": {
            "count": len(cluster.source_urls),
            "urls": cluster.source_urls,
            "types": sorted(set(cluster.source_types)),
            "l0_note_paths": [n.path for n in cluster.notes],
            "avg_l0_quality": cluster.avg_l0_quality,
        },
        "consensus": {
            "used": bool(consensus and consensus.get("results")),
            "via": (consensus or {}).get("via"),
            "count": len((consensus or {}).get("results") or []),
            "results": (consensus or {}).get("results") or [],
        },
        "synthesis": {
            "model": None,
            "via": "extractive_fallback",
            "elapsed_s": 0,
            "mode": "fallback",
            "error": error,
            "raw_preview": (raw or "")[:500],
        },
    }


# ---------------------------------------------------------------------------
# Deterministic scorer (A1 — no LLM)
# ---------------------------------------------------------------------------


def _text_blob(card: dict[str, Any]) -> str:
    parts = [
        str(card.get("title") or ""),
        str(card.get("claim") or ""),
        str(card.get("summary") or ""),
        str(card.get("mechanism") or ""),
        str(card.get("why_not_median") or ""),
        json.dumps(card.get("pattern") or {}, ensure_ascii=False),
        json.dumps(card.get("idea") or {}, ensure_ascii=False),
        json.dumps(card.get("tac") or {}, ensure_ascii=False),
        json.dumps(card.get("evidence") or [], ensure_ascii=False),
        " ".join(card.get("next_actions") or []),
        " ".join(str(d) for d in (card.get("doctrine_domains") or [])),
    ]
    return "\n".join(parts).lower()


def score_build_card(card: dict[str, Any]) -> dict[str, Any]:
    blob = _text_blob(card)
    sources = card.get("sources") or {}
    source_count = int(sources.get("count") or len(sources.get("urls") or []) or 0)
    urls = list(sources.get("urls") or [])
    domains: set[str] = set()
    for u in urls:
        try:
            domains.add(urllib.parse.urlparse(u).netloc.lower())
        except Exception:
            pass
    independent = max(len(domains), source_count)
    evidence = card.get("evidence") or []
    consensus_used = bool((card.get("consensus") or {}).get("used"))
    tac = card.get("tac") or {}
    idea = card.get("idea") or {}
    pattern = card.get("pattern") or {}
    mechanism = str(card.get("mechanism") or "")
    doctrine_domains = card.get("doctrine_domains") or []
    breakdown: dict[str, dict[str, Any]] = {}

    # multi_source (18)
    if independent >= 4 or (independent >= 3 and consensus_used):
        ms = 18
    elif independent >= 3:
        ms = 15
    elif independent == 2:
        ms = 9
    elif independent == 1:
        ms = 3
    else:
        ms = 0
    breakdown["multi_source"] = {
        "weight": 18, "score": ms,
        "independent_sources": independent, "consensus_used": consensus_used,
    }

    # mechanism_density (18)
    mech_hits = sum(1 for m in MECHANISM_MARKERS if m in mechanism.lower() or m in blob)
    mech_len = len(mechanism.strip())
    if mech_len >= 400 and mech_hits >= 4:
        md = 18
    elif mech_len >= 200 and mech_hits >= 3:
        md = 14
    elif mech_len >= 100 and mech_hits >= 2:
        md = 10
    elif mech_len >= 40:
        md = 5
    else:
        md = 0
    sludge = len(
        re.findall(
            r"\b(revolutionary|seamless|cutting-edge|robust|leverage|synergy)\b",
            blob,
        )
    )
    if sludge >= 3 and mech_hits < 2:
        md = max(0, md - 6)
    breakdown["mechanism_density"] = {
        "weight": 18, "score": md,
        "mechanism_chars": mech_len, "marker_hits": mech_hits, "sludge_hits": sludge,
    }

    # evidence_anchoring (14)
    ev_urls = sum(1 for e in evidence if isinstance(e, dict) and e.get("url"))
    has_numbers = bool(re.search(r"\b\d+(\.\d+)?%?\b", blob))
    quote_hits = blob.count('"') // 2
    if (ev_urls >= 3 or (ev_urls >= 2 and consensus_used)) and has_numbers:
        ea = 14
    elif ev_urls >= 2:
        ea = 11
    elif ev_urls >= 1 and has_numbers:
        ea = 8
    elif ev_urls >= 1 or has_numbers:
        ea = 5
    else:
        ea = 0
    if quote_hits >= 2:
        ea = min(14, ea + 1)
    breakdown["evidence_anchoring"] = {
        "weight": 14, "score": ea,
        "evidence_urls": ev_urls, "has_numbers": has_numbers,
    }

    # tac_structure (12)
    tac_hits = sum(1 for m in TAC_MARKERS if m in blob)
    done_raw = str(idea.get("done_test") or tac.get("done_test") or "")
    has_done = len(done_raw) >= 8
    has_builder = bool(tac.get("builder"))
    has_verifier = bool(tac.get("verifier"))
    split = (
        has_builder
        and has_verifier
        and str(tac.get("builder")).lower() != str(tac.get("verifier")).lower()
    )
    ts = 0
    if has_done:
        ts += 4
    if split:
        ts += 4
    elif has_builder or has_verifier:
        ts += 1
    if tac_hits >= 4:
        ts += 4
    elif tac_hits >= 2:
        ts += 2
    ts = min(12, ts)
    breakdown["tac_structure"] = {
        "weight": 12, "score": ts,
        "tac_marker_hits": tac_hits, "has_done_test": has_done, "gev_split": split,
    }

    # doctrine_fit (12)
    inferred = _infer_doctrine_domains(blob)
    declared = [str(d) for d in doctrine_domains]
    if declared and inferred:
        df = 12 if (set(inferred) & set(declared) or declared[0] in DOCTRINE_DOMAINS) else 9
    elif declared or inferred:
        df = 7
    else:
        df = 2
    breakdown["doctrine_fit"] = {
        "weight": 12, "score": df, "declared": declared, "inferred": inferred,
    }

    # novelty_pattern (10)
    has_pattern = bool(pattern.get("name") and pattern.get("description"))
    has_anti = bool(pattern.get("when_not_to_use"))
    why = str(card.get("why_not_median") or "")
    np_ = 0
    if has_pattern:
        np_ += 5
    if has_anti:
        np_ += 2
    if len(why) >= 40:
        np_ += 3
    elif len(why) >= 15:
        np_ += 1
    np_ = min(10, np_)
    breakdown["novelty_pattern"] = {
        "weight": 10, "score": np_,
        "has_pattern": has_pattern, "has_anti_trigger": has_anti,
        "why_not_median_chars": len(why),
    }

    # actionability (8)
    actions = card.get("next_actions") or []
    has_idea = bool(idea.get("name") and (idea.get("description") or idea.get("acceptance")))
    act = 0
    if has_idea:
        act += 4
    if isinstance(actions, list) and len(actions) >= 2:
        act += 3
    elif isinstance(actions, list) and len(actions) == 1:
        act += 1
    if idea.get("acceptance"):
        act += 1
    act = min(8, act)
    breakdown["actionability"] = {
        "weight": 8, "score": act,
        "has_idea": has_idea,
        "action_count": len(actions) if isinstance(actions, list) else 0,
    }

    # gev_separation (8)
    if split:
        gev = 8
    elif has_builder or has_verifier:
        gev = 3
    else:
        gev = 0
    if has_builder and has_verifier and not split:
        gev = 0
    breakdown["gev_separation"] = {
        "weight": 8, "score": gev, "independent": split,
        "builder": tac.get("builder"), "verifier": tac.get("verifier"),
    }

    total = sum(v["score"] for v in breakdown.values())
    hard_blocks: list[str] = []
    if independent < 2:
        hard_blocks.append("fewer_than_2_independent_sources")
        total = min(total, 54)
    if not mechanism.strip() or len(mechanism.strip()) < 40:
        hard_blocks.append("mechanism_missing_or_thin")
        total = min(total, 54)
    if not has_done:
        hard_blocks.append("done_test_missing")
        total = min(total, 69)

    if total >= 94:
        rank = "EXCELLENT"
    elif total >= 85:
        rank = "STRONG"
    elif total >= GOOD_FLOOR:
        rank = "GOOD"
    elif total >= 55:
        rank = "WEAK"
    else:
        rank = "REJECT"

    promote = rank in {"GOOD", "STRONG", "EXCELLENT"} and not any(
        b in hard_blocks
        for b in ("fewer_than_2_independent_sources", "mechanism_missing_or_thin")
    )

    return {
        "schema": SCHEMA_SCORE,
        "card_id": card.get("card_id"),
        "scored_at": utc_now(),
        "total": total,
        "rank": rank,
        "promote": promote,
        "good_floor": GOOD_FLOOR,
        "hard_blocks": hard_blocks,
        "breakdown": breakdown,
        "thresholds": {
            "REJECT": "<55",
            "WEAK": "55-69",
            "GOOD": f"{GOOD_FLOOR}-84",
            "STRONG": "85-93",
            "EXCELLENT": "94+",
        },
        "scorer": "omniscout-l2-scorer-deterministic-v1",
        "gev": {
            "execution_owner": "omniscout-l2-synthesizer",
            "terminal_verifier": "omniscout-l2-scorer-deterministic-v1",
            "independent": True,
        },
    }


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


def _card_to_markdown(card: dict[str, Any], score: dict[str, Any]) -> str:
    lines = [
        f"# {card.get('title')}",
        "",
        f"- **card_id:** `{card.get('card_id')}`",
        f"- **rank:** **{score.get('rank')}** ({score.get('total')}/100)",
        f"- **topic:** {card.get('topic')}",
        f"- **sources:** {(card.get('sources') or {}).get('count')}",
        f"- **consensus:** {(card.get('consensus') or {}).get('via')} "
        f"n={(card.get('consensus') or {}).get('count')}",
        f"- **promote:** {score.get('promote')}",
        f"- **created:** {card.get('created_at')}",
        "",
        "## Claim",
        str(card.get("claim") or ""),
        "",
        "## Summary",
        str(card.get("summary") or ""),
        "",
        "## Mechanism",
        str(card.get("mechanism") or ""),
        "",
        "## Pattern",
        f"```json\n{json.dumps(card.get('pattern') or {}, indent=2)}\n```",
        "",
        "## Idea / Build slice",
        f"```json\n{json.dumps(card.get('idea') or {}, indent=2)}\n```",
        "",
        "## TAC",
        f"```json\n{json.dumps(card.get('tac') or {}, indent=2)}\n```",
        "",
        "## Score breakdown",
        f"```json\n{json.dumps(score.get('breakdown') or {}, indent=2)}\n```",
        "",
        "## Evidence",
    ]
    for e in card.get("evidence") or []:
        if isinstance(e, dict):
            lines.append(f"- {e.get('url')}: {e.get('quote_or_fact')}")
    lines += [
        "",
        "## Sources",
        *[f"- {u}" for u in (card.get("sources") or {}).get("urls") or []],
        "",
        "## Next actions",
        *[f"- {a}" for a in card.get("next_actions") or []],
        "",
        "## Hard blocks",
        *([f"- {b}" for b in score.get("hard_blocks") or []] or ["- none"]),
        "",
    ]
    return "\n".join(lines) + "\n"


def _count_ranks(cards: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for c in cards:
        out[str(c.get("rank") or "UNKNOWN")] += 1
        out["TOTAL"] += 1
    return dict(out)


def _update_index(card: dict[str, Any], score: dict[str, Any], path: Path) -> None:
    if L2_INDEX.exists():
        try:
            idx = json.loads(L2_INDEX.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            idx = {"schema": "rig.omniscout.l2-index.v1", "cards": []}
    else:
        idx = {"schema": "rig.omniscout.l2-index.v1", "cards": []}
    cards = [c for c in idx.get("cards") or [] if c.get("card_id") != card.get("card_id")]
    cards.insert(
        0,
        {
            "card_id": card.get("card_id"),
            "title": card.get("title"),
            "topic": card.get("topic"),
            "rank": score.get("rank"),
            "total": score.get("total"),
            "promote": score.get("promote"),
            "source_count": (card.get("sources") or {}).get("count"),
            "consensus_via": (card.get("consensus") or {}).get("via"),
            "path": str(path),
            "created_at": card.get("created_at"),
            "artifact_sha256": card.get("artifact_sha256"),
        },
    )
    idx["cards"] = cards[:5000]
    idx["updated_at"] = utc_now()
    idx["counts"] = _count_ranks(cards)
    atomic_json(L2_INDEX, idx)


def persist_card(card: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    ensure_l2_dirs()
    card = dict(card)
    card["score"] = score
    card["card_sha256"] = sha256_text(
        stable_json({k: v for k, v in card.items() if k not in {"card_sha256", "score"}})
    )
    card["artifact_sha256"] = sha256_text(stable_json(card))
    dest_dir = L2_CARDS if score.get("promote") else L2_QUARANTINE
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{card['card_id']}.json"
    atomic_json(path, card)
    atomic_text(path.with_suffix(".md"), _card_to_markdown(card, score))
    append_jsonl(
        L2_LEDGER,
        {
            "ts": utc_now(),
            "event": "card_written",
            "card_id": card["card_id"],
            "cluster_id": card.get("cluster_id"),
            "rank": score.get("rank"),
            "total": score.get("total"),
            "promote": score.get("promote"),
            "path": str(path),
            "artifact_sha256": card["artifact_sha256"],
            "source_count": (card.get("sources") or {}).get("count"),
            "consensus_via": (card.get("consensus") or {}).get("via"),
            "topic": card.get("topic"),
        },
    )
    _update_index(card, score, path)
    return {
        "path": str(path),
        "rank": score.get("rank"),
        "total": score.get("total"),
        "promote": score.get("promote"),
    }


# ---------------------------------------------------------------------------
# Topic strategy (tiered allocation)
# ---------------------------------------------------------------------------


def load_topic_strategy() -> dict[str, Any]:
    """Load TOPIC_STRATEGY.json from L2 root or control mirror."""
    candidates = [
        L2_ROOT / "TOPIC_STRATEGY.json",
        Path.home() / ".rig" / "omniscout-control" / "build-cards" / "TOPIC_STRATEGY.json",
        Path(__file__).resolve().parents[2]
        / "docs"
        / "handoffs"
        / "TOPIC_STRATEGY.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("strategies"):
                    return data
            except (OSError, json.JSONDecodeError):
                continue
    return {
        "schema": "rig.omniscout.l2-topic-strategy.v1",
        "daily_budget": TARGET_L2_PER_DAY,
        "block_tutorial_l2": True,
        "agent_engineering_daily_cap": 5,
        "strategies": {},
        "tiers": {
            "T0": {"quota": 28},
            "T1": {"quota": 20},
            "T2": {"quota": 8},
            "T3": {"quota": 4},
        },
    }


def _is_tutorial_label(label: str, strategy: dict[str, Any]) -> bool:
    low = (label or "").lower()
    for s in strategy.get("tutorial_label_substrings") or ["tutorial"]:
        if s.lower() in low:
            return True
    return False


def map_label_to_strategy(label: str, text_blob: str = "", strategy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map an L0 topic label to strategy_id + tier."""
    strategy = strategy or load_topic_strategy()
    low = f"{label} {text_blob}".lower()
    # BI bag split
    if "business intelligence and strategy" in (label or "").lower():
        split = strategy.get("bi_strategy_keyword_split") or {}
        for sid, kws in split.items():
            if sid == "default":
                continue
            if any(k in low for k in kws):
                meta = (strategy.get("strategies") or {}).get(sid) or {}
                return {
                    "strategy_id": sid,
                    "tier": meta.get("tier") or "T1",
                    "quota": int(meta.get("quota") or 1),
                    "tutorial": False,
                }
        default = split.get("default") or "strategy-decision-routing"
        meta = (strategy.get("strategies") or {}).get(default) or {}
        return {
            "strategy_id": default,
            "tier": meta.get("tier") or "T1",
            "quota": int(meta.get("quota") or 1),
            "tutorial": False,
        }

    tutorial = _is_tutorial_label(label, strategy)
    best = None
    best_hits = 0
    for sid, meta in (strategy.get("strategies") or {}).items():
        hits = 0
        for m in meta.get("label_match") or []:
            if m.lower() in low:
                hits += 1 + (2 if m.lower() == (label or "").lower() else 0)
        if hits > best_hits:
            best_hits = hits
            best = (sid, meta)
    if best is None:
        # unmatched
        if tutorial and strategy.get("block_tutorial_l2", True):
            return {
                "strategy_id": "blocked-tutorial",
                "tier": "BLOCK",
                "quota": 0,
                "tutorial": True,
            }
        return {
            "strategy_id": "unmapped",
            "tier": "T3",
            "quota": 1,
            "tutorial": tutorial,
        }
    sid, meta = best
    tier = meta.get("tier") or "T3"
    if tutorial and strategy.get("block_tutorial_l2", True) and tier not in {"T0", "T1"}:
        # tutorials only allowed if they mapped hard into T0/T1
        return {
            "strategy_id": "blocked-tutorial",
            "tier": "BLOCK",
            "quota": 0,
            "tutorial": True,
        }
    return {
        "strategy_id": sid,
        "tier": tier,
        "quota": int(meta.get("quota") or 1),
        "tutorial": tutorial,
        "question": meta.get("question"),
        "consensus_queries": list(meta.get("consensus_queries") or []),
    }


def daily_strategy_counts() -> dict[str, int]:
    """Count promoted cards per strategy_id for UTC day."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    counts: dict[str, int] = defaultdict(int)
    for folder in (L2_CARDS, L2_QUARANTINE):
        if not folder.exists():
            continue
        for path in folder.glob("l2-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            created = str(data.get("created_at") or "")
            if not created.startswith(day):
                continue
            sid = (data.get("strategy") or {}).get("strategy_id") or "unmapped"
            counts[sid] += 1
    return dict(counts)


def annotate_cluster_strategy(cluster: "SourceCluster", strategy: dict[str, Any] | None = None) -> dict[str, Any]:
    blob = " ".join(n.summary for n in cluster.notes[:3])
    return map_label_to_strategy(cluster.topic, blob, strategy)


def prioritize_clusters_for_strategy(
    clusters: list["SourceCluster"],
    *,
    limit: int,
    strategy: dict[str, Any] | None = None,
) -> list[tuple["SourceCluster", dict[str, Any]]]:
    """Order clusters by tier deficit, then sources, then L0 quality."""
    strategy = strategy or load_topic_strategy()
    done = already_processed_cluster_ids()
    today = daily_strategy_counts()
    tier_order = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}
    tier_quota = {
        t: int((strategy.get("tiers") or {}).get(t, {}).get("quota") or 0)
        for t in ("T0", "T1", "T2", "T3")
    }
    tier_filled = defaultdict(int)
    for sid, n in today.items():
        meta = (strategy.get("strategies") or {}).get(sid) or {}
        tier_filled[meta.get("tier") or "T3"] += n

    scored: list[tuple[tuple, SourceCluster, dict[str, Any]]] = []
    for cluster in clusters:
        if cluster.cluster_id in done:
            continue
        meta = annotate_cluster_strategy(cluster, strategy)
        if meta.get("tier") == "BLOCK":
            continue
        sid = meta["strategy_id"]
        # per-strategy daily cap
        cap = int(meta.get("quota") or 1)
        if sid == "agent-engineering":
            cap = min(cap, int(strategy.get("agent_engineering_daily_cap") or 5))
        if today.get(sid, 0) >= cap:
            continue
        # independent sources
        domains = set()
        for u in cluster.source_urls:
            try:
                domains.add(urllib.parse.urlparse(u).netloc.lower())
            except Exception:
                pass
        indep = max(len(domains), len(cluster.source_urls))
        min_src = int(
            (strategy.get("tiers") or {})
            .get(meta["tier"], {})
            .get("min_sources")
            or MIN_SOURCES
        )
        if indep < min_src and meta["tier"] in {"T0", "T1"}:
            # still allow if consensus will fill — keep but lower priority
            pass
        strategy_deficit = cap - today.get(sid, 0)
        tier_deficit = tier_quota.get(meta["tier"], 0) - tier_filled.get(meta["tier"], 0)
        key = (
            tier_order.get(meta["tier"], 9),
            -strategy_deficit,
            -tier_deficit,
            -indep,
            -cluster.avg_l0_quality,
        )
        scored.append((key, cluster, meta))

    scored.sort(key=lambda row: row[0])
    # build output respecting remaining tier budgets roughly
    out: list[tuple[SourceCluster, dict[str, Any]]] = []
    chosen_sid: dict[str, int] = defaultdict(int)
    for _, cluster, meta in scored:
        if len(out) >= limit:
            break
        sid = meta["strategy_id"]
        cap = int(meta.get("quota") or 1)
        if sid == "agent-engineering":
            cap = min(cap, int(strategy.get("agent_engineering_daily_cap") or 5))
        if today.get(sid, 0) + chosen_sid[sid] >= cap:
            continue
        out.append((cluster, meta))
        chosen_sid[sid] += 1
    return out


def consensus_query_for_meta(meta: dict[str, Any], cluster: "SourceCluster") -> str:
    qs = list(meta.get("consensus_queries") or [])
    if not qs:
        return f"{cluster.topic} {cluster.title}"[:200]
    # rotate by cluster id
    idx = int(sha256_text(cluster.cluster_id)[:8], 16) % len(qs)
    return qs[idx]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def already_processed_cluster_ids() -> set[str]:
    done: set[str] = set()
    if L2_LEDGER.exists():
        for line in L2_LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("cluster_id"):
                done.add(row["cluster_id"])
    for folder in (L2_CARDS, L2_QUARANTINE):
        if not folder.exists():
            continue
        for p in folder.glob("l2-*.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if d.get("cluster_id"):
                    done.add(d["cluster_id"])
            except (OSError, json.JSONDecodeError):
                continue
    return done


def collect_clusters(*, min_sources: int = MIN_SOURCES) -> list[SourceCluster]:
    notes = load_l0_notes(roots=[L0_RESEARCH], min_quality=0)
    notes = merge_remote_notes(notes)
    rich = [n for n in notes if n.quality_score >= 2 or n.source_url]
    return cluster_notes(rich, min_sources=min_sources)



def _claims_path() -> Path:
    return L2_ROOT / "claimed-clusters.json"


def load_claims() -> dict[str, Any]:
    path = _claims_path()
    if not path.exists():
        return {"schema": "rig.omniscout.l2-claims.v1", "claims": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("claims", {})
            return data
    except json.JSONDecodeError:
        pass
    return {"schema": "rig.omniscout.l2-claims.v1", "claims": {}}


def claim_cluster(cluster_id: str, *, owner: str) -> bool:
    """Atomically claim a cluster. Returns False if already claimed/processed."""
    ensure_l2_dirs()
    if cluster_id in already_processed_cluster_ids():
        return False
    lock_path = L2_ROOT / "claims.lock"
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        data = load_claims()
        claims = data.setdefault("claims", {})
        if cluster_id in claims:
            return False
        if cluster_id in already_processed_cluster_ids():
            return False
        claims[cluster_id] = {"owner": owner, "claimed_at": utc_now()}
        data["updated_at"] = utc_now()
        atomic_json(_claims_path(), data)
        return True


def campaign_lock(blocking: bool = False):
    """Context manager: exclusive campaign lock so only one runner produces cards."""
    import fcntl
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        ensure_l2_dirs()
        path = L2_ROOT / "campaign.lock"
        fh = path.open("a+", encoding="utf-8")
        try:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(fh.fileno(), flags)
            except BlockingIOError as exc:
                raise RuntimeError("another L2 campaign holds the lock") from exc
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} at={utc_now()}\n")
            fh.flush()
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()

    return _cm()


def build_one_from_cluster(
    cluster: SourceCluster,
    *,
    use_consensus: bool = True,
    model: str = DEEP_MODEL,
    strategy_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_l2_dirs()
    strategy_meta = strategy_meta or annotate_cluster_strategy(cluster)
    cdict = cluster.to_dict()
    cdict["strategy"] = strategy_meta
    atomic_json(L2_CLUSTERS / f"{cluster.cluster_id}.json", cdict)

    consensus: dict[str, Any] | None = None
    if use_consensus:
        q = consensus_query_for_meta(strategy_meta, cluster)
        consensus = consensus_search(q, limit_hint=5)
        if consensus and consensus.get("results"):
            for r in consensus["results"]:
                u = r.get("url")
                if u and u not in cluster.source_urls:
                    cluster.source_urls.append(u)
                    cluster.source_types.append("consensus_paper")

    card = synthesize_build_card(cluster, consensus=consensus, model=model)
    # refresh source count after consensus merge
    card["sources"] = {
        **(card.get("sources") or {}),
        "count": len(cluster.source_urls),
        "urls": cluster.source_urls,
        "types": sorted(set(cluster.source_types)),
    }
    card["strategy"] = {
        "strategy_id": strategy_meta.get("strategy_id"),
        "tier": strategy_meta.get("tier"),
        "question": strategy_meta.get("question"),
        "mapped_from_topic": cluster.topic,
    }
    score = score_build_card(card)
    # T0: Consensus required for promote (tier admission)
    if (
        strategy_meta.get("tier") == "T0"
        and score.get("promote")
        and not (consensus and consensus.get("results"))
    ):
        score = dict(score)
        score["promote"] = False
        score["hard_blocks"] = list(score.get("hard_blocks") or []) + [
            "t0_consensus_required"
        ]
        if score.get("rank") in {"GOOD", "STRONG", "EXCELLENT"}:
            score["rank"] = "WEAK"
            score["total"] = min(int(score.get("total") or 0), 69)
        score["tier_gate"] = "T0_requires_consensus_mcp"
    written = persist_card(card, score)
    return {
        "ok": True,
        "cluster_id": cluster.cluster_id,
        "card_id": card.get("card_id"),
        "score": score,
        "written": written,
        "consensus_used": bool(consensus and consensus.get("results")),
        "consensus_via": (consensus or {}).get("via"),
        "synthesis_mode": (card.get("synthesis") or {}).get("mode"),
        "strategy_id": strategy_meta.get("strategy_id"),
        "tier": strategy_meta.get("tier"),
    }


def _tally_ranks(produced: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for p in produced:
        out[str((p.get("score") or {}).get("rank") or p.get("rank") or "UNKNOWN")] += 1
    return dict(out)


def _update_state(summary: dict[str, Any]) -> None:
    if L2_STATE.exists():
        try:
            state = json.loads(L2_STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}
    state["schema"] = "rig.omniscout.l2-state.v1"
    state["updated_at"] = utc_now()
    state["last_run"] = {
        "at": summary.get("at"),
        "produced_count": summary.get("produced_count"),
        "promoted_count": summary.get("promoted_count"),
        "rank_counts": summary.get("rank_counts"),
    }
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = state.setdefault("daily", {})
    bucket = daily.setdefault(day, {"produced": 0, "promoted": 0, "by_rank": {}})
    bucket["produced"] = int(bucket.get("produced") or 0) + int(
        summary.get("produced_count") or 0
    )
    bucket["promoted"] = int(bucket.get("promoted") or 0) + int(
        summary.get("promoted_count") or 0
    )
    for k, v in (summary.get("rank_counts") or {}).items():
        bucket["by_rank"][k] = int(bucket["by_rank"].get(k) or 0) + int(v)
    state["disk"] = summary.get("disk")
    atomic_json(L2_STATE, state)


def run_l2_batch(
    *,
    limit: int = 3,
    min_sources: int = MIN_SOURCES,
    use_consensus: bool = True,
    model: str | None = None,
) -> dict[str, Any]:
    ensure_l2_dirs()
    stop = should_stop_for_disk()
    if stop["stop"]:
        return {
            "schema": SCHEMA_RUN,
            "ok": False,
            "stopped": True,
            "reason": stop["reason"],
            "disk": stop["disk"],
            "produced": [],
            "at": utc_now(),
        }

    model = model or DEEP_MODEL
    topic_strategy = load_topic_strategy()
    clusters = collect_clusters(min_sources=min_sources)
    if not clusters and min_sources > 2:
        clusters = collect_clusters(min_sources=2)
    prioritized = prioritize_clusters_for_strategy(
        clusters, limit=max(limit * 4, limit), strategy=topic_strategy
    )
    # fallback: if strategy filters everything, allow non-blocked pending
    if not prioritized:
        done = already_processed_cluster_ids()
        prioritized = [
            (c, annotate_cluster_strategy(c, topic_strategy))
            for c in clusters
            if c.cluster_id not in done
            and annotate_cluster_strategy(c, topic_strategy).get("tier") != "BLOCK"
        ][: max(limit * 3, limit)]

    produced: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    owner = f"pid-{os.getpid()}"
    for cluster, strategy_meta in prioritized:
        if len(produced) >= limit:
            break
        stop = should_stop_for_disk()
        if stop["stop"]:
            break
        if not claim_cluster(cluster.cluster_id, owner=owner):
            continue
        try:
            produced.append(
                build_one_from_cluster(
                    cluster,
                    use_consensus=use_consensus,
                    model=model,
                    strategy_meta=strategy_meta,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"cluster_id": cluster.cluster_id, "error": str(exc)[:300]})

    summary = {
        "schema": SCHEMA_RUN,
        "ok": True,
        "at": utc_now(),
        "model": model,
        "clusters_seen": len(clusters),
        "clusters_pending": len(prioritized),
        "produced_count": len(produced),
        "promoted_count": sum(
            1 for p in produced if (p.get("score") or {}).get("promote")
        ),
        "rank_counts": _tally_ranks(produced),
        "errors": errors,
        "disk": qnap_disk_status(),
        "target_l2_per_day": TARGET_L2_PER_DAY,
        "produced": [
            {
                "card_id": p.get("card_id"),
                "rank": (p.get("score") or {}).get("rank"),
                "total": (p.get("score") or {}).get("total"),
                "path": (p.get("written") or {}).get("path"),
                "synthesis_mode": p.get("synthesis_mode"),
                "consensus_via": p.get("consensus_via"),
                "strategy_id": p.get("strategy_id"),
                "tier": p.get("tier"),
            }
            for p in produced
        ],
        "topic_strategy_version": topic_strategy.get("version"),
        "daily_strategy_counts": daily_strategy_counts(),
    }
    atomic_json(
        L2_RUNS
        / f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json",
        summary,
    )
    atomic_json(L2_ROOT / "latest-run.json", summary)
    _update_state(summary)
    return summary


def run_until_qnap_pressure(
    *,
    batch_size: int = 2,
    sleep_s: int = 30,
    max_cards: int | None = None,
    model: str | None = None,
    use_consensus: bool = True,
) -> dict[str, Any]:
    ensure_l2_dirs()
    # Exclusive campaign lock (non-blocking). Second runner exits cleanly.
    try:
        import fcntl
        _camp_lock_fh = (L2_ROOT / "campaign.lock").open("a+", encoding="utf-8")
        fcntl.flock(_camp_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _camp_lock_fh.seek(0); _camp_lock_fh.truncate()
        _camp_lock_fh.write(f"pid={os.getpid()} at={utc_now()}\n"); _camp_lock_fh.flush()
    except BlockingIOError:
        return {
            "schema": SCHEMA_CAMPAIGN,
            "ok": False,
            "error": "another_campaign_running",
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "produced_count": 0,
        }
    started = utc_now()
    t0 = time.time()
    all_produced: list[dict[str, Any]] = []
    batches = 0
    stop_reason = None

    while True:
        stop = should_stop_for_disk()
        if stop["stop"]:
            stop_reason = stop["reason"]
            break
        if max_cards is not None and len(all_produced) >= max_cards:
            stop_reason = "max_cards"
            break

        remaining = None if max_cards is None else max_cards - len(all_produced)
        limit = batch_size if remaining is None else min(batch_size, remaining)

        summary = run_l2_batch(
            limit=limit, use_consensus=use_consensus, model=model
        )
        batches += 1
        if summary.get("stopped"):
            stop_reason = summary.get("reason")
            break
        batch_rows = summary.get("produced") or []
        all_produced.extend(batch_rows)

        if not batch_rows:
            summary2 = run_l2_batch(
                limit=limit,
                min_sources=2,
                use_consensus=use_consensus,
                model=model or FAST_MODEL,
            )
            batches += 1
            batch_rows = summary2.get("produced") or []
            all_produced.extend(batch_rows)
            if not batch_rows:
                stop_reason = "no_pending_clusters"
                break

        time.sleep(max(1, sleep_s))

    final = {
        "schema": SCHEMA_CAMPAIGN,
        "ok": True,
        "started_at": started,
        "finished_at": utc_now(),
        "elapsed_s": round(time.time() - t0, 2),
        "batches": batches,
        "produced_count": len(all_produced),
        "promoted_count": sum(
            1
            for p in all_produced
            if p.get("rank") in {"GOOD", "STRONG", "EXCELLENT"}
        ),
        "rank_counts": _count_ranks(all_produced),
        "stop_reason": stop_reason,
        "disk": qnap_disk_status(),
        "cards": all_produced,
        "l2_root": str(L2_ROOT),
        "target_l2_per_day": TARGET_L2_PER_DAY,
    }
    atomic_json(L2_ROOT / "latest-campaign.json", final)
    append_jsonl(
        L2_LEDGER,
        {
            "ts": utc_now(),
            "event": "campaign_finished",
            "produced_count": final["produced_count"],
            "promoted_count": final["promoted_count"],
            "stop_reason": stop_reason,
            "elapsed_s": final["elapsed_s"],
        },
    )
    return final


def smoke_test() -> dict[str, Any]:
    ensure_l2_dirs()
    disk = qnap_disk_status()
    # Prove Consensus MCP first
    try:
        from .consensus_mcp import consensus_mcp_smoke

        consensus_proof = consensus_mcp_smoke()
    except Exception as exc:  # noqa: BLE001
        consensus_proof = {"ok": False, "reason": str(exc)[:200]}

    notes = load_l0_notes(min_quality=0)
    notes = merge_remote_notes(notes)
    clusters = cluster_notes(notes, min_sources=2)
    if not clusters:
        if len(notes) >= 2:
            tops = sorted(notes, key=lambda n: -n.quality_score)[:3]
            urls = [n.source_url for n in tops if n.source_url]
            clusters = [
                SourceCluster(
                    cluster_id="cluster-smoke-" + sha256_text(utc_now())[:10],
                    topic=tops[0].topic or "smoke",
                    title=tops[0].title,
                    notes=tops,
                    source_urls=urls or [f"file://{n.path}" for n in tops],
                    source_types=[n.source_type for n in tops],
                    avg_l0_quality=sum(n.quality_score for n in tops) / len(tops),
                )
            ]
        else:
            return {
                "ok": False,
                "error": "no_l0_notes",
                "notes": len(notes),
                "disk": disk,
                "consensus": consensus_proof,
            }

    cluster = clusters[0]
    result = build_one_from_cluster(cluster, use_consensus=True, model=FAST_MODEL)
    path = Path((result.get("written") or {}).get("path") or "")
    proof = {
        "ok": bool(path.exists()),
        "path": str(path),
        "path_exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else 0,
        "rank": (result.get("score") or {}).get("rank"),
        "total": (result.get("score") or {}).get("total"),
        "promote": (result.get("score") or {}).get("promote"),
        "breakdown": (result.get("score") or {}).get("breakdown"),
        "hard_blocks": (result.get("score") or {}).get("hard_blocks"),
        "card_id": result.get("card_id"),
        "cluster_id": cluster.cluster_id,
        "source_count": len(cluster.source_urls),
        "notes_loaded": len(notes),
        "clusters_found": len(clusters),
        "disk": disk,
        "consensus": {
            "mcp_ok": bool(consensus_proof.get("ok")),
            "mcp_count": consensus_proof.get("count"),
            "via": result.get("consensus_via"),
            "used": result.get("consensus_used"),
        },
        "synthesis_mode": result.get("synthesis_mode"),
        "rubric": str(SCORE_RUBRIC_PATH),
        "l2_root": str(L2_ROOT),
    }
    atomic_json(L2_ROOT / "latest-smoke.json", proof)
    return proof


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="OmniScout L2 build-card engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("smoke", help="one-card end-to-end smoke")
    p_batch = sub.add_parser("batch", help="process N clusters")
    p_batch.add_argument("--limit", type=int, default=3)
    p_batch.add_argument("--model", default=None)
    p_batch.add_argument("--min-sources", type=int, default=MIN_SOURCES)
    p_run = sub.add_parser("run", help="run until QNAP pressure / STOP / max")
    p_run.add_argument("--batch-size", type=int, default=2)
    p_run.add_argument("--sleep", type=int, default=20)
    p_run.add_argument("--max-cards", type=int, default=None)
    p_run.add_argument("--model", default=None)
    p_score = sub.add_parser("score-file", help="score an existing card JSON")
    p_score.add_argument("path")
    sub.add_parser("disk", help="show QNAP disk gate")
    p_clusters = sub.add_parser("clusters", help="list clusters")
    p_clusters.add_argument("--min-sources", type=int, default=2)
    sub.add_parser("consensus-smoke", help="prove Consensus MCP search")
    sub.add_parser("strategy", help="show topic strategy + daily counts")

    args = parser.parse_args(argv)

    if args.cmd == "smoke":
        out = smoke_test()
    elif args.cmd == "batch":
        out = run_l2_batch(
            limit=args.limit, model=args.model, min_sources=args.min_sources
        )
    elif args.cmd == "run":
        out = run_until_qnap_pressure(
            batch_size=args.batch_size,
            sleep_s=args.sleep,
            max_cards=args.max_cards,
            model=args.model,
        )
    elif args.cmd == "score-file":
        card = json.loads(Path(args.path).read_text(encoding="utf-8"))
        out = score_build_card(card)
    elif args.cmd == "disk":
        out = should_stop_for_disk()
    elif args.cmd == "clusters":
        cs = collect_clusters(min_sources=args.min_sources)
        out = {
            "ok": True,
            "count": len(cs),
            "clusters": [
                {
                    "id": c.cluster_id,
                    "topic": c.topic,
                    "sources": len(c.source_urls),
                    "avg_q": c.avg_l0_quality,
                    "title": c.title[:80],
                }
                for c in cs[:30]
            ],
        }
    elif args.cmd == "consensus-smoke":
        from .consensus_mcp import consensus_mcp_smoke

        out = consensus_mcp_smoke()
    elif args.cmd == "strategy":
        strat = load_topic_strategy()
        out = {
            "ok": True,
            "version": strat.get("version"),
            "daily_budget": strat.get("daily_budget"),
            "tiers": strat.get("tiers"),
            "daily_counts": daily_strategy_counts(),
            "strategy_ids": list((strat.get("strategies") or {}).keys()),
        }
    else:
        parser.error("unknown command")
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok", True) is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
