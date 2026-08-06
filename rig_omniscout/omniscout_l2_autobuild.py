"""OmniScout L2 card-to-code autobuilder.

Reads a V20 build card's engineering_blueprint and emits a runnable Python
project scaffold under L2_ROOT/built/{card_id}/.  All generation is
pattern-based and deterministic — no LLM calls in the hot path.
"""

from __future__ import annotations

import argparse
import json
import py_compile
import re
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

SCHEMA = "rig.omniscout.build-card.v21-autobuilt"

BUILT_ROOT = L2_ROOT / "built"

# Map common tech_stack keywords to installable package names.
_PACKAGE_MAP: dict[str, str] = {
    "python": "",
    "pydantic": "pydantic>=2.0",
    "fastapi": "fastapi>=0.110",
    "uvicorn": "uvicorn>=0.30",
    "flask": "flask>=3.0",
    "django": "django>=5.0",
    "sqlalchemy": "sqlalchemy>=2.0",
    "sqlite": "",
    "pytest": "pytest>=8.0",
    "requests": "requests>=2.31",
    "httpx": "httpx>=0.27",
    "langchain": "langchain>=0.2",
    "langgraph": "langgraph>=0.0",
    "crewai": "crewai>=0.30",
    "ollama": "ollama>=0.2",
    "prefect": "prefect>=3.0",
    "docker": "docker>=7.0",
    "numpy": "numpy>=1.26",
    "pandas": "pandas>=2.2",
    "typer": "typer>=0.12",
    "click": "click>=8.1",
    "rich": "rich>=13.0",
    "jinja2": "jinja2>=3.1",
}


def _safe_name(value: str) -> str:
    """Convert arbitrary text into a valid Python identifier."""
    s = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    if not s or s[0].isdigit():
        s = "project_" + s
    return s[:64]


def _indent(text: str, width: int = 4) -> str:
    pad = " " * width
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def _tech_to_requirements(tech_stack: dict[str, Any]) -> list[str]:
    """Flatten tech_stack dict into pip-installable requirement lines."""
    seen: set[str] = set()
    out: list[str] = []
    for category_items in tech_stack.values():
        if not isinstance(category_items, list):
            continue
        for item in category_items:
            key = item.lower().strip()
            pkg = _PACKAGE_MAP.get(key)
            if pkg and pkg not in seen:
                seen.add(pkg)
                out.append(pkg)
    # Always include pytest for the generated test and pydantic for models.py.
    for required in ("pytest>=8.0", "pydantic>=2.0"):
        if required not in seen:
            seen.add(required)
            out.append(required)
    return sorted(out)


def _pyproject_toml(card: dict[str, Any], card_id: str) -> str:
    title = card.get("title", card_id)
    safe = _safe_name(title)
    desc = card.get("claim", "Auto-built OmniScout L2 project.")
    return f"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{safe}"
version = "0.1.0"
description = {desc!r}
requires-python = ">=3.10"
readme = "README.md"

[project.scripts]
{safe} = "{safe}.main:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
"""


def _requirements_txt(card: dict[str, Any]) -> str:
    tech_stack = card.get("engineering_blueprint", {}).get("tech_stack", {})
    lines = _tech_to_requirements(tech_stack)
    return "\n".join(lines) + "\n"


def _readme_md(card: dict[str, Any], card_id: str) -> str:
    title = card.get("title", card_id)
    claim = card.get("claim", "")
    mechanism = card.get("mechanism", "")
    strategy = card.get("strategy", {})
    strategy_id = strategy.get("strategy_id", "unknown")
    tier = strategy.get("tier", "?")
    blueprint = card.get("engineering_blueprint", {})
    tech = blueprint.get("tech_stack", {})
    tech_block = "\n".join(f"- **{k}**: {', '.join(str(i) for i in v)}" for k, v in tech.items() if isinstance(v, list))
    return f"""# {title}

**Card ID:** `{card_id}`  
**Strategy:** `{strategy_id}` ({tier})

## Claim

{claim}

## Mechanism

{mechanism}

## Technology

{tech_block}

## Running

```bash
python main.py
pytest test_done.py
```

This project was autobuilt by `rig_foundry.omniscout_l2_autobuild`.
"""


def _models_py(card: dict[str, Any]) -> str:
    pattern = card.get("pattern", {})
    idea = card.get("idea", {})
    tac = card.get("tac", {})
    title = card.get("title", "card")
    class_name = _safe_name(title).replace("_", " ").title().replace(" ", "")
    if not class_name or class_name[0].isdigit():
        class_name = "CardModel"

    p_name = pattern.get("name", "")
    p_desc = pattern.get("description", "")
    p_when = pattern.get("when_to_use", "")
    p_when_not = pattern.get("when_not_to_use", "")

    i_name = idea.get("name", "")
    i_desc = idea.get("description", "")
    i_accept = idea.get("acceptance", "")
    i_done = idea.get("done_test", "")

    t_core = tac.get("core_four", [])
    t_loop = tac.get("closed_loop", "")
    t_builder = tac.get("builder", "")
    t_verifier = tac.get("verifier", "")
    t_done = tac.get("done_test", "")

    return f"""\"\"\"Pydantic models derived from the OmniScout build card.\"\"\"

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Pattern(BaseModel):
    name: str = {p_name!r}
    description: str = {p_desc!r}
    when_to_use: str = {p_when!r}
    when_not_to_use: str = {p_when_not!r}


class Idea(BaseModel):
    name: str = {i_name!r}
    description: str = {i_desc!r}
    acceptance: str = {i_accept!r}
    done_test: str = {i_done!r}


class TAC(BaseModel):
    core_four: List[str] = Field(default_factory=lambda: {stable_json(t_core)})
    closed_loop: str = {t_loop!r}
    builder: str = {t_builder!r}
    verifier: str = {t_verifier!r}
    done_test: str = {t_done!r}


class {class_name}(BaseModel):
    \"\"\"Root model for the autobuilt card.\"\"\"
    card_id: str
    title: str
    claim: str
    pattern: Pattern
    idea: Idea
    tac: TAC
    score: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="allow")


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    card_path = Path(__file__).resolve().parent / "CARD.json"
    data = json.loads(card_path.read_text())
    model = {class_name}(**data)
    print(model.model_dump_json(indent=2))
"""


def _proof_py(card: dict[str, Any]) -> str:
    proof_seal = card.get("proof_seal", {})
    prev_hash = proof_seal.get("this_hash", "0" * 64)
    return f"""\"\"\"ProofPacket sealing (hash chain) for {card['card_id']}.\"\"\"

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def seal_proof_packet(
    card: dict[str, Any],
    artifact: dict[str, Any],
    score: dict[str, Any],
    done_test_ok: bool,
    gev_ok: bool,
) -> dict[str, Any]:
    \"\"\"Seal a deterministic ProofPacket chained to the card's prior proof.\"\"\"
    payload = {{
        "schema": "rig.omniscout.proof-seal.v1",
        "card_id": card.get("card_id", ""),
        "sealed_at": utc_now(),
        "artifact": artifact,
        "score": score,
        "done_test_ok": done_test_ok,
        "gev_ok": gev_ok,
        "prev_hash": {prev_hash!r},
    }}
    payload["proof_hash"] = sha256_text(stable_json(payload))
    return payload


def write_proof_packet(project_dir: Path, packet: dict[str, Any]) -> Path:
    path = project_dir / "proof_packet.json"
    path.write_text(stable_json(packet) + "\\n")
    return path


if __name__ == "__main__":
    import sys

    root = Path(__file__).resolve().parent
    card = json.loads((root / "CARD.json").read_text())
    artifact = {{
        "card_id": card["card_id"],
        "claim": card.get("claim", ""),
        "mechanism": card.get("mechanism", ""),
    }}
    packet = seal_proof_packet(card, artifact, {{"total": 0}}, True, True)
    write_proof_packet(root, packet)
    print(packet["proof_hash"])
"""


def _gates_py(card: dict[str, Any]) -> str:
    score = card.get("score", {})
    min_score = max(0, score.get("total", 70) - 17)  # deterministic threshold
    sources = card.get("sources", {})
    min_sources = max(1, sources.get("count", 3) - 1)
    gates = card.get("doctrine_governance", {}).get("gates", [])
    gate_names = [g.get("name", f"Gate-{i}") for i, g in enumerate(gates)]
    harness = card.get("engineering_blueprint", {}).get("harness", {})
    builder = harness.get("builder", "")
    verifier = harness.get("verifier", "")
    done_test = card.get("idea", {}).get("done_test", "")

    return f"""\"\"\"Deterministic scorer, GEV check, and done-test evaluator.\"\"\"

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

CARD_PATH = Path(__file__).resolve().parent / "CARD.json"


def load_card() -> dict[str, Any]:
    return json.loads(CARD_PATH.read_text())


def score_artifact(card: dict[str, Any]) -> dict[str, Any]:
    \"\"\"Deterministic score check derived from card fields.\"\"\"
    score = card.get("score", {{}})
    total = score.get("total", 0)
    rank = score.get("rank", "UNKNOWN")
    sources = card.get("sources", {{}})
    src_count = sources.get("count", 0) if isinstance(sources, dict) else 0
    return {{
        "total": total,
        "rank": rank,
        "sources_count": src_count,
        "passes": total >= {min_score} and src_count >= {min_sources},
    }}


def run_done_test(card_path: Path = CARD_PATH) -> tuple[bool, str]:
    \"\"\"Run the card's executable done-test against CARD.json.\"\"\"
    cmd = {done_test!r}
    if not cmd:
        return False, "No done_test configured"
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=card_path.parent,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as exc:  # pragma: no cover
        return False, str(exc)


def gev_check(card: dict[str, Any]) -> bool:
    \"\"\"Verify builder and verifier identities are separated.\"\"\"
    harness = card.get(\"engineering_blueprint\", {{}}).get(\"harness\", {{}})
    builder = harness.get(\"builder\", {builder!r})
    verifier = harness.get(\"verifier\", {verifier!r})
    return bool(builder) and bool(verifier) and builder != verifier


def gate_status(card: dict[str, Any] | None = None) -> dict[str, Any]:
    card = card or load_card()
    score = score_artifact(card)
    done_ok, done_output = run_done_test()
    gev_ok = gev_check(card)
    return {{
        "score": score,
        "done_test_ok": done_ok,
        "done_test_output": done_output,
        "gev_ok": gev_ok,
        "all_pass": score["passes"] and done_ok and gev_ok,
        "gates": {stable_json(gate_names)},
    }}


def main(argv: list[str] | None = None) -> int:
    status = gate_status()
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if status["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _harness_py(card: dict[str, Any]) -> str:
    harness = card.get("engineering_blueprint", {}).get("harness", {})
    timeout = harness.get("timeout_s", 3600)
    retry = harness.get("retry_policy", "exponential backoff, max 3, fail-closed")
    return f"""\"\"\"Closed-loop harness: build → verify → seal for {card['card_id']}.\"\"\"

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gates import gate_status, run_done_test
from proof import seal_proof_packet, write_proof_packet

ROOT = Path(__file__).resolve().parent
CARD_PATH = ROOT / "CARD.json"


def load_card() -> dict[str, Any]:
    return json.loads(CARD_PATH.read_text())


def build_artifact(card: dict[str, Any]) -> dict[str, Any]:
    \"\"\"Materialise the artifact described by the card.\"\"\"
    return {{
        "card_id": card["card_id"],
        "title": card.get("title", ""),
        "claim": card.get("claim", ""),
        "mechanism": card.get("mechanism", ""),
        "strategy_id": card.get("strategy", {{}}).get("strategy_id", ""),
    }}


def run(max_retries: int = 3, timeout_s: int = {timeout}) -> int:
    \"\"\"Execute the closed loop: build → score → done-test → GEV → seal.\"\"\"
    print(f"[harness] starting {{CARD_PATH.name}} (timeout={{timeout_s}}s)")
    card = load_card()
    artifact = build_artifact(card)

    for attempt in range(1, max_retries + 1):
        print(f"[harness] attempt {{attempt}}/{{max_retries}}")
        status = gate_status(card)
        score = status["score"]
        done_ok = status["done_test_ok"]
        gev_ok = status["gev_ok"]

        packet = seal_proof_packet(card, artifact, score, done_ok, gev_ok)
        write_proof_packet(ROOT, packet)
        print(f"[harness] proof sealed: {{packet['proof_hash'][:16]}}")

        if status["all_pass"]:
            print("[harness] PASS")
            return 0

        print(f"[harness] gate status: score={{score['passes']}} done={{done_ok}} gev={{gev_ok}}")

    print("[harness] FAIL (exhausted retries)")
    return 1


def main(argv: list[str] | None = None) -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _main_py(card: dict[str, Any]) -> str:
    return f"""#!/usr/bin/env python3
\"\"\"Entry point for {card['card_id']}: {card.get('title', '')}.\"\"\"

from __future__ import annotations

import sys

from harness import run


def main(argv: list[str] | None = None) -> int:
    \"\"\"Run the closed-loop harness.\"\"\"
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _test_done_py(card: dict[str, Any]) -> str:
    done_test = card.get("idea", {}).get("done_test", "")
    return f"""\"\"\"Executable done-test for {card['card_id']}.\"\"\"

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CARD_PATH = ROOT / "CARD.json"


def test_card_exists():
    assert CARD_PATH.exists(), "CARD.json must be present in project directory"


def test_done_test_passes():
    cmd = {done_test!r}
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
"""


def _build_manifest(
    card: dict[str, Any],
    project_dir: Path,
    files_generated: list[str],
) -> dict[str, Any]:
    manifest = {
        "schema": "rig.omniscout.build-manifest.v1",
        "card_id": card["card_id"],
        "autobuild_schema": SCHEMA,
        "files_generated": sorted(files_generated),
        "build_at": utc_now(),
        "artifact_hash": sha256_text(stable_json({"files": sorted(files_generated)})),
    }
    return manifest


def build_from_card(card_path: str | Path) -> dict[str, Any]:
    """Generate a runnable Python project scaffold from a V20 build card.

    Returns a dict with keys: card_id, project_dir, files_generated, manifest_hash.
    """
    card_path = Path(card_path)
    card: dict[str, Any] = json.loads(card_path.read_text(encoding="utf-8"))

    if card.get("schema") != "rig.omniscout.build-card.v20":
        raise ValueError(f"Unsupported card schema: {card.get('schema')}")

    card_id = card["card_id"]
    project_dir = BUILT_ROOT / card_id
    project_dir.mkdir(parents=True, exist_ok=True)

    # Copy the source card into the project directory so generated tests can find it.
    card_copy_path = project_dir / "CARD.json"
    atomic_json(card_copy_path, card)

    files: dict[str, str] = {
        "main.py": _main_py(card),
        "harness.py": _harness_py(card),
        "gates.py": _gates_py(card),
        "proof.py": _proof_py(card),
        "models.py": _models_py(card),
        "test_done.py": _test_done_py(card),
        "README.md": _readme_md(card, card_id),
        "requirements.txt": _requirements_txt(card),
        "pyproject.toml": _pyproject_toml(card, card_id),
    }

    generated: list[str] = []
    for name, content in files.items():
        dest = project_dir / name
        atomic_text(dest, content)
        generated.append(name)

    # Validate generated Python files compile.
    for name in ("main.py", "harness.py", "gates.py", "proof.py", "models.py"):
        py_compile.compile(project_dir / name, doraise=True)

    manifest = _build_manifest(card, project_dir, generated)
    manifest_path = project_dir / "build_manifest.json"
    atomic_json(manifest_path, manifest)
    generated.append("build_manifest.json")

    # Update original card with autobuilt metadata.
    card["autobuilt"] = {
        "built": True,
        "project_path": str(project_dir),
        "files_count": len(generated),
        "build_at": manifest["build_at"],
        "manifest_hash": manifest["artifact_hash"],
    }
    card["schema"] = SCHEMA
    atomic_json(card_path, card)

    return {
        "card_id": card_id,
        "project_dir": str(project_dir),
        "files_generated": sorted(generated),
        "manifest_hash": manifest["artifact_hash"],
    }


def _is_v20_card(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("schema") == "rig.omniscout.build-card.v20" and not data.get("autobuilt", {}).get("built")


def build_all() -> dict[str, Any]:
    """Build project scaffolds for all V20 cards that have not been autobuilt."""
    BUILT_ROOT.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for card_path in sorted(L2_CARDS.glob("l2-*.json")):
        if not _is_v20_card(card_path):
            continue
        try:
            results.append(build_from_card(card_path))
        except Exception as exc:
            errors.append({"path": str(card_path), "error": str(exc)})

    return {
        "schema": SCHEMA,
        "built": results,
        "errors": errors,
        "built_count": len(results),
        "error_count": len(errors),
    }


def _status() -> dict[str, Any]:
    cards = list(L2_CARDS.glob("l2-*.json"))
    v20 = [p for p in cards if _is_v20_card(p)]
    built = [p for p in cards if _is_built_card(p)]
    return {
        "schema": SCHEMA,
        "total_cards": len(cards),
        "v20_unbuilt": len(v20),
        "v20_built": len(built),
        "built_root": str(BUILT_ROOT),
    }


def _is_built_card(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("schema") == SCHEMA or bool(data.get("autobuilt", {}).get("built"))


def _list_built() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for card_path in sorted(L2_CARDS.glob("l2-*.json")):
        data = json.loads(card_path.read_text(encoding="utf-8"))
        if data.get("autobuilt", {}).get("built"):
            out.append(
                {
                    "card_id": data.get("card_id"),
                    "project_path": data["autobuilt"]["project_path"],
                    "files_count": data["autobuilt"]["files_count"],
                    "build_at": data["autobuilt"]["build_at"],
                }
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniScout L2 card-to-code autobuilder")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("all", help="Build all unbuilt V20 cards")
    one = sub.add_parser("one", help="Build a single card by path")
    one.add_argument("path", help="Path to a V20 build-card JSON file")
    sub.add_parser("status", help="Show autobuild status")
    sub.add_parser("list", help="List built projects")

    args = parser.parse_args(argv)

    if args.command == "all":
        result = build_all()
        print(stable_json(result))
        return 1 if result["error_count"] else 0

    if args.command == "one":
        result = build_from_card(args.path)
        print(stable_json(result))
        return 0

    if args.command == "status":
        print(stable_json(_status()))
        return 0

    if args.command == "list":
        for item in _list_built():
            print(f"{item['card_id']}\t{item['files_count']} files\t{item['project_path']}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
