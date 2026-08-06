"""Consensus.app MCP client — uses Claude/Hermes OAuth via mcp-remote.

No CONSENSUS_API_KEY required. Reuses the existing mcp-remote OAuth session
that Claude Desktop / Hermes already authorized against https://mcp.consensus.app/mcp.

Tool surface (probed 2026-08-05):
  search(query, year_min?, year_max?, domain?, study_types?, ...)
  → paper titles, authors, abstracts, citations, journal scores, URLs
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


NPX = os.environ.get(
    "CONSENSUS_MCP_NPX",
    "/home/operator/.hermes/node/bin/npx",
)
MCP_URL = os.environ.get("CONSENSUS_MCP_URL", "https://mcp.consensus.app/mcp")
DEFAULT_TIMEOUT_S = float(os.environ.get("CONSENSUS_MCP_TIMEOUT_S", "45"))


class ConsensusMCPError(RuntimeError):
    pass


class ConsensusMCPClient:
    """Long-lived stdio MCP session to Consensus via mcp-remote."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._out: list[str] = []
        self._err: list[str] = []
        self._lock = threading.Lock()
        self._next_id = 1
        self._ready = False

    def start(self, *, timeout: float = 20.0) -> None:
        if self._ready and self._proc and self._proc.poll() is None:
            return
        self.close()
        env = {
            **os.environ,
            "PATH": f"/home/operator/.hermes/node/bin:{os.environ.get('PATH', '')}",
        }
        self._proc = subprocess.Popen(
            [NPX, "-y", "mcp-remote", MCP_URL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            bufsize=1,
        )
        self._out = []
        self._err = []

        def _read_out() -> None:
            assert self._proc and self._proc.stdout
            try:
                for line in self._proc.stdout:
                    self._out.append(line.rstrip("\n"))
            except Exception:  # noqa: BLE001
                return

        def _read_err() -> None:
            assert self._proc and self._proc.stderr
            try:
                for line in self._proc.stderr:
                    self._err.append(line.rstrip("\n"))
            except Exception:  # noqa: BLE001
                return

        threading.Thread(target=_read_out, daemon=True).start()
        threading.Thread(target=_read_err, daemon=True).start()

        init_id = self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "rig-omniscout-l2", "version": "1.0"},
            },
            timeout=timeout,
        )
        if not init_id:
            raise ConsensusMCPError(
                f"MCP initialize failed. stderr_tail={self._err[-8:]}"
            )
        self._notify("notifications/initialized")
        self._ready = True

    def close(self) -> None:
        self._ready = False
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass
        self._proc = None

    def __enter__(self) -> "ConsensusMCPClient":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _send(self, obj: dict[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            raise ConsensusMCPError("MCP process not running")
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any] | None:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            msg: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
            }
            if params is not None:
                msg["params"] = params
            before = len(self._out)
            self._send(msg)
            deadline = time.time() + timeout
            while time.time() < deadline:
                for line in self._out[before:]:
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if parsed.get("id") == req_id:
                        if "error" in parsed:
                            raise ConsensusMCPError(str(parsed["error"])[:500])
                        return parsed.get("result")
                if self._proc and self._proc.poll() is not None:
                    raise ConsensusMCPError(
                        f"MCP process exited. stderr_tail={self._err[-8:]}"
                    )
                time.sleep(0.05)
            return None

    def tools_list(self) -> list[dict[str, Any]]:
        self.start()
        result = self._rpc("tools/list") or {}
        return list(result.get("tools") or [])

    def search(
        self,
        query: str,
        *,
        year_min: int | None = None,
        domain: str | None = None,
        exclude_preprints: bool | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Call Consensus MCP `search`. Returns normalized papers list."""
        self.start()
        args: dict[str, Any] = {"query": query[:500]}
        if year_min is not None:
            args["year_min"] = year_min
        if domain is not None:
            args["domain"] = domain
        if exclude_preprints is not None:
            args["exclude_preprints"] = exclude_preprints

        result = self._rpc(
            "tools/call",
            {"name": "search", "arguments": args},
            timeout=timeout,
        )
        if result is None:
            return {
                "ok": False,
                "enabled": True,
                "via": "mcp",
                "reason": "timeout",
                "results": [],
                "raw": "",
            }
        if result.get("isError"):
            text = _content_text(result)
            return {
                "ok": False,
                "enabled": True,
                "via": "mcp",
                "reason": text[:300],
                "results": [],
                "raw": text,
            }
        text = _content_text(result)
        papers = parse_consensus_search_text(text)
        return {
            "ok": True,
            "enabled": True,
            "via": "mcp",
            "count": len(papers),
            "results": papers,
            "raw": text[:8000],
            "query": query,
        }


def _content_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


_PAPER_RE = re.compile(
    r"\[(\d+)\]\s+\[([^\]]+)\]\((https?://[^)]+)\)\s*"
    r"(?:\(([^)]*)\))?\s*"
    r"(?:\n\s*(.+?)(?=\n\[|\Z))?",
    re.S,
)


def parse_consensus_search_text(text: str) -> list[dict[str, Any]]:
    """Parse the markdown-ish paper list returned by Consensus MCP search."""
    papers: list[dict[str, Any]] = []
    if not text:
        return papers
    # Split on numbered entries
    chunks = re.split(r"\n(?=\[\d+\])", text.strip())
    for chunk in chunks:
        m = re.match(
            r"\[(\d+)\]\s+\[([^\]]+)\]\((https?://[^)]+)\)\s*(?:\(([^)]*)\))?\s*(.*)",
            chunk.strip(),
            re.S,
        )
        if not m:
            continue
        meta = m.group(4) or ""
        abstract = (m.group(5) or "").strip()
        year = None
        ym = re.search(r"\b(19|20)\d{2}\b", meta)
        if ym:
            year = int(ym.group(0))
        citations = None
        cm = re.search(r"(\d+)\s+citations?", meta, re.I)
        if cm:
            citations = int(cm.group(1))
        authors = ""
        if "," in meta:
            authors = meta.split(",")[0].strip()
        papers.append(
            {
                "title": m.group(2).strip(),
                "url": m.group(3).strip(),
                "year": year,
                "abstract": abstract[:1200],
                "citation_count": citations,
                "meta": meta.strip(),
                "authors": authors,
                "source": "consensus_mcp",
            }
        )
    return papers


# Module-level shared client (lazy)
_SHARED: ConsensusMCPClient | None = None
_SHARED_LOCK = threading.Lock()


def get_shared_client() -> ConsensusMCPClient:
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = ConsensusMCPClient()
        return _SHARED


def consensus_mcp_search(query: str, **kwargs: Any) -> dict[str, Any]:
    """Convenience: search via shared MCP client, soft-fail on errors."""
    try:
        client = get_shared_client()
        return client.search(query, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "enabled": True,
            "via": "mcp",
            "reason": str(exc)[:300],
            "results": [],
        }


def consensus_mcp_smoke() -> dict[str, Any]:
    """One search to prove OAuth + tool path."""
    t0 = time.time()
    result = consensus_mcp_search(
        "agentic coding verification gates multi-agent",
    )
    result["elapsed_s"] = round(time.time() - t0, 2)
    return result
