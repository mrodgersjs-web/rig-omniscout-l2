"""OmniScout L2 content engine — deterministic marketing assets from V20 build cards."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from rig_foundry.omniscout_build_cards import (
    DOCTRINE_DOMAINS,
    L2_CARDS,
    L2_ROOT,
    atomic_json,
    atomic_text,
    score_build_card,
    sha256_text,
    slugify,
    stable_json,
    utc_now,
)


# ---------------------------------------------------------------------------
# Card loading / safe accessors
# ---------------------------------------------------------------------------


def _load_card(card_path: str | Path) -> dict[str, Any]:
    return json.loads(Path(card_path).read_text(encoding="utf-8"))


def _card_id(card: dict[str, Any], path: Path | None = None) -> str:
    return card.get("card_id") or (path.stem if path else "unknown")


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(x) for x in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _first_line(text: str | list[str] | Any) -> str:
    text = _as_text(text)
    if not text:
        return ""
    return text.strip().splitlines()[0].strip()


def _sentences(text: str | list[str] | Any, n: int | None = None) -> list[str]:
    text = _as_text(text)
    if not text:
        return []
    splits = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 15]
    if n is not None:
        return splits[:n]
    return splits


def _bullet_points(text: str | list[str] | Any, n: int = 3) -> list[str]:
    """Extract bullet-sized statements from mechanism/summary text."""
    text = _as_text(text)
    if not text:
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bullets = [
        ln.lstrip("-•* ").strip()
        for ln in lines
        if ln.startswith(("-", "•", "*")) or (len(ln) > 30 and ":" in ln)
    ]
    if len(bullets) < n:
        sents = _sentences(text, n * 2)
        bullets += sents
    seen: set[str] = set()
    out: list[str] = []
    for b in bullets:
        key = b[:80].lower()
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out[:n]


def _entity_names(card: dict[str, Any]) -> list[str]:
    entities = card.get("entities", {})
    if isinstance(entities, dict):
        items = entities.get("entities", [])
    elif isinstance(entities, list):
        items = entities
    else:
        items = []
    return [e["name"] for e in items if isinstance(e, dict) and "name" in e]


def _consensus_titles(card: dict[str, Any]) -> list[str]:
    consensus = card.get("consensus", {})
    if not isinstance(consensus, dict):
        return []
    return [r.get("title", "") for r in consensus.get("results", []) if isinstance(r, dict)]


def _source_count(card: dict[str, Any]) -> int:
    sources = card.get("sources", {})
    if isinstance(sources, dict):
        return sources.get("count", 0) or len(sources.get("urls", []))
    return 0


def _impl_steps(card: dict[str, Any]) -> list[str]:
    bp = card.get("engineering_blueprint", {})
    if not isinstance(bp, dict):
        return []
    steps = bp.get("implementation_steps", [])
    out: list[str] = []
    for i, s in enumerate(steps):
        if isinstance(s, str):
            out.append(f"Step {i + 1}: {s}")
            continue
        if not isinstance(s, dict):
            continue
        action = s.get("action", "")
        gate = s.get("gate", "")
        line = f"Step {s.get('step', i + 1)}: {action}"
        if gate:
            line += f" (gate: {gate})"
        out.append(line)
    return out


def _architecture_components(card: dict[str, Any]) -> list[str]:
    bp = card.get("engineering_blueprint", {})
    if not isinstance(bp, dict):
        return []
    return [str(c) for c in bp.get("architecture_components", [])]


def _testing_strategy(card: dict[str, Any]) -> dict[str, Any]:
    bp = card.get("engineering_blueprint", {})
    if not isinstance(bp, dict):
        return {}
    ts = bp.get("testing_strategy", {})
    return ts if isinstance(ts, dict) else {}


def _revenue_ideas(card: dict[str, Any]) -> list[str]:
    bi = card.get("business_intelligence", {})
    if not isinstance(bi, dict):
        return []
    return [str(x) for x in bi.get("revenue_ideas", [])]


def _competitive_moat(card: dict[str, Any]) -> list[str]:
    bi = card.get("business_intelligence", {})
    if not isinstance(bi, dict):
        return []
    return [str(x) for x in bi.get("competitive_moat", [])]


def _differentiation(card: dict[str, Any]) -> list[str]:
    gtm = card.get("gtm_strategy", {})
    if not isinstance(gtm, dict):
        return []
    return [str(x) for x in gtm.get("differentiation", [])]


def _gtm_positioning(card: dict[str, Any]) -> str:
    gtm = card.get("gtm_strategy", {})
    if isinstance(gtm, dict):
        pos = gtm.get("positioning", "")
        if pos:
            return pos
    return card.get("claim", "")


def _gtm_channels(card: dict[str, Any]) -> list[str]:
    gtm = card.get("gtm_strategy", {})
    if not isinstance(gtm, dict):
        return []
    return [str(c) for c in gtm.get("channels", [])]


def _price_range(card: dict[str, Any]) -> str:
    bi = card.get("business_intelligence", {})
    if isinstance(bi, dict):
        pr = bi.get("price_range", "")
        if pr:
            return pr
    return "Contact us"


def _score_rank(card: dict[str, Any]) -> str:
    score = card.get("score", {})
    if isinstance(score, dict):
        rank = score.get("rank", "")
        if rank:
            return f"{rank} ({score.get('total', '')})"
        return str(score.get("total", ""))
    return ""


def _council_verdict(card: dict[str, Any]) -> str:
    verdict = card.get("council_verdict", "")
    if verdict:
        return verdict
    council = card.get("council", {})
    if isinstance(council, dict):
        synth = council.get("synthesis", {})
        if isinstance(synth, dict):
            return synth.get("overall_verdict", "")
    return ""


def _analysis(card: dict[str, Any]) -> dict[str, Any]:
    a = card.get("analysis", {})
    return a if isinstance(a, dict) else {}


def _direction(card: dict[str, Any]) -> dict[str, Any]:
    d = card.get("direction", {})
    return d if isinstance(d, dict) else {}


def _math_formulas(card: dict[str, Any]) -> list[dict[str, Any]]:
    math = card.get("math", [])
    return [m for m in math if isinstance(m, dict)]


def _idea_name(card: dict[str, Any]) -> str:
    idea = card.get("idea", {})
    if isinstance(idea, dict):
        return idea.get("name") or card.get("title", "")
    return card.get("title", "")


def _idea_description(card: dict[str, Any]) -> str:
    idea = card.get("idea", {})
    if isinstance(idea, dict):
        return idea.get("description") or idea.get("name") or card.get("summary", "")[:300]
    return str(idea)


def _hashtags(card: dict[str, Any], max_tags: int = 6) -> list[str]:
    tags = card.get("tags", [])
    out: list[str] = []
    for t in tags[:max_tags]:
        if not t:
            continue
        h = re.sub(r"[^a-z0-9_-]", "", t.lower().replace(":", "-").replace(" ", "-").replace("/", "-"))
        h = h.strip("-")
        if h:
            out.append(f"#{h}")
    return out


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


# ---------------------------------------------------------------------------
# Expansion helpers (deterministic padding without LLMs)
# ---------------------------------------------------------------------------


def _expand_sentence(sentence: str) -> str:
    """Turn a single sentence into a short paragraph."""
    sentence = sentence.strip()
    if not sentence:
        return ""
    return (
        f"{sentence} This matters because it changes how teams translate research into shipped capability. "
        "By encoding the insight into a reproducible build card, RIG can execute the same pattern repeatedly without losing fidelity. "
        "Each new source either reinforces the mechanism or flags a contradiction that must be resolved before promotion to doctrine."
    )


def _expand_tradeoff(tradeoff: str) -> str:
    tradeoff = tradeoff.strip()
    if not tradeoff:
        return ""
    return (
        f"One tension to manage: {tradeoff} Teams should treat this as a hypothesis to be retired as soon as real load data arrives, "
        "not as a permanent constraint. The deterministic scorer will downgrade the card if the trade-off turns into a hard block."
    )


def _build_expansion(card: dict[str, Any], needed: int) -> str:
    """Deterministic filler to hit a target word count when source material is thin."""
    paras: list[str] = []
    title = card.get("title", "")
    topic = card.get("topic", "")
    score = _score_rank(card)
    tags = card.get("tags", [])
    rank_tag = next((t for t in tags if t.startswith("rank:")), "")
    tier_tag = next((t for t in tags if t.startswith("tier:")), "")
    strategy = card.get("strategy", {})
    strategy_id = strategy.get("strategy_id", "") if isinstance(strategy, dict) else ""
    if needed <= 0:
        return ""
    paras.append(
        f"Looking at the broader arc for '{title}', the card sits inside RIG's '{topic}' intelligence stream "
        f"with strategy '{strategy_id}'. The deterministic scorer rated it {score}, which places it in the "
        f"{rank_tag or 'scored'} band at {tier_tag or 'an unnamed tier'}. That score is not a vanity metric: every point is "
        "backed by source counts, mechanism density, and executable actionability."
    )
    paras.append(
        "The real value of this card is not the idea itself, but the machinery around it. A clear claim, "
        "a verifiable mechanism, a council verdict, and a done-test give any builder a fair shot at reproducing the result. "
        "That reproducibility is what separates a build card from a blog post."
    )
    paras.append(
        "For operators, the next move is to treat this card as a living artifact. As new evidence lands, "
        "the score, confidence, and direction sections should update. If the claim weakens, the card should be quarantined or killed. "
        "If it strengthens, it earns a place in the doctrine queue and eventually becomes an agent-loadable rule."
    )
    text = "\n\n".join(paras)
    while _word_count(text) < needed and len(paras) < 20:
        paras.append(
            "RIG's production system depends on this honesty loop. A green score that cannot be driven red is theater, "
            "so every promoted card carries a regression test or a planted failure that proves the gate actually fires."
        )
        text = "\n\n".join(paras)
    return text


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def generate_linkedin(card_path: str | Path) -> dict[str, Any]:
    card = _load_card(card_path)
    hook = _first_line(card.get("claim", "")) or card.get("title", "")
    source_text = _as_text(card.get("mechanism", "") or card.get("summary", ""))
    bullets = _bullet_points(source_text, 3)
    if len(bullets) < 3:
        bullets += _sentences(card.get("summary", ""), 3 - len(bullets))
    bullets = bullets[:3]

    entities = _entity_names(card)
    titles = _consensus_titles(card)
    source_count = _source_count(card)
    score = _score_rank(card)

    proof_parts: list[str] = []
    if entities:
        proof_parts.append(
            f"This card draws on {len(entities)} extracted concepts—including {', '.join(entities[:5])}."
        )
    if titles:
        proof_parts.append(
            f"It is anchored by {source_count} consensus source(s): {', '.join(titles[:3])}."
        )
    if not proof_parts and source_count:
        proof_parts.append(
            f"Grounded in {source_count} independent source(s) and RIG's deterministic scoring ({score})."
        )
    if not proof_parts:
        proof_parts.append(
            "Grounded in RIG's deterministic build-card scoring and cross-session entity graph."
        )
    proof = " ".join(proof_parts)

    cta = _gtm_positioning(card) or "Learn how RIG turns research into shipped capability."
    tags = _hashtags(card, 6)

    body = "\n\n".join(f"• {b}" for b in bullets)
    content = f"{hook}\n\n{body}\n\n{proof}\n\n{cta}\n\n{' '.join(tags)}"

    wc = _word_count(content)
    if wc < 200:
        # Pad with real summary sentences until we reach the target band.
        summary_sents = _sentences(card.get("summary", ""), 5)
        extra_parts: list[str] = []
        for s in summary_sents:
            if wc >= 200:
                break
            extra_parts.append(s)
            wc = _word_count(
                f"{hook}\n\n{body}\n\n{proof} {' '.join(extra_parts)}\n\n{cta}\n\n{' '.join(tags)}"
            )
        if extra_parts:
            content = f"{hook}\n\n{body}\n\n{proof} {' '.join(extra_parts)}\n\n{cta}\n\n{' '.join(tags)}"

    wc = _word_count(content)
    return {"content": content, "word_count": wc, "hashtags": tags}


def generate_youtube_script(card_path: str | Path) -> dict[str, Any]:
    card = _load_card(card_path)
    title = card.get("title", "Untitled")
    hook = _first_line(card.get("claim", "")) or card.get("summary", "")[:200]
    steps = _impl_steps(card)
    if not steps:
        steps = ["Step 1: Scope the capability and write the done-test", "Step 2: Build the smallest working slice", "Step 3: Run deterministic verification"]
    demo = _idea_description(card)
    positioning = _gtm_positioning(card)
    channels = _gtm_channels(card) or ["LinkedIn", "YouTube", "GitHub"]

    lines: list[str] = [f"# {title}\n"]
    lines.append("[00:00] NARRATOR: " + hook + " Welcome to the RIG build breakdown.")
    lines.append("[VISUAL: Title card with '" + title.replace("'", "'") + "' and RIG branding]\n")

    base_time = 30
    for i, step in enumerate(steps):
        ts = base_time + i * 30
        mm, ss = divmod(ts, 60)
        visual_detail = step.split(":", 1)[1].strip() if ":" in step else step
        lines.append(f"[{mm:02d}:{ss:02d}] NARRATOR: {step}")
        lines.append(f"[VISUAL: On-screen diagram / code walkthrough for: {visual_detail[:80]}]\n")

    lines.append("[03:30] NARRATOR: Let's see it in action. " + demo)
    lines.append("[VISUAL: Screen-capture demo or architecture diagram]\n")

    lines.append(
        "[04:30] NARRATOR: "
        + (positioning or "Want this in production? Let's talk.")
        + " Follow RIG on "
        + ", ".join(channels)
        + ", or grab the build card and run it yourself."
    )
    lines.append("[VISUAL: CTA slide with contact links and card QR code]\n")
    lines.append("[05:00] NARRATOR: Thanks for watching — ship with proof.\n")

    return {"script": "\n".join(lines), "duration_estimate": "5:00"}


def generate_substack(card_path: str | Path) -> dict[str, Any]:
    card = _load_card(card_path)
    title = card.get("title", "Untitled Build Card")
    claim = _as_text(card.get("claim", ""))
    summary = _as_text(card.get("summary", ""))
    mechanism = _as_text(card.get("mechanism", ""))
    analysis = _analysis(card)
    direction = _direction(card)
    math = _math_formulas(card)
    tags = card.get("tags", [])

    lines: list[str] = [f"# {title}\n"]

    lines.append("## Thesis\n")
    if claim:
        lines.append(claim + "\n")
        lines.append(f"> {claim}\n")
    else:
        lines.append(f"> {title}\n")

    lines.append("## Evidence\n")
    sum_sents = _sentences(summary, 8)
    for s in sum_sents:
        lines.append(_expand_sentence(s) + "\n")

    mech_bullets = _bullet_points(mechanism, 5)
    for b in mech_bullets:
        lines.append(f"- {b}")
    if mech_bullets:
        lines.append("")

    entities = _entity_names(card)
    sources = _source_count(card)
    if entities:
        lines.append(
            f"This card maps **{len(entities)}** key entities: {', '.join(entities[:8])}. "
            "These anchors let RIG agents reason across sessions without re-deriving the same ontology."
        )
    if sources:
        lines.append(
            f"The synthesis rests on **{sources}** independent sources, routed through Consensus MCP and scored deterministically."
        )
    if tags:
        lines.append(f"Tags: {', '.join(tags[:10])}.")
    lines.append("")

    lines.append("## Analysis\n")
    conf = analysis.get("confidence", "")
    rationale = analysis.get("confidence_rationale", "")
    if conf:
        lines.append(f"Confidence level: **{conf}**. {rationale}\n")
    tradeoffs = analysis.get("tradeoffs", []) if isinstance(analysis.get("tradeoffs", []), list) else []
    for to in tradeoffs:
        lines.append(_expand_tradeoff(_as_text(to)) + "\n")
    if not tradeoffs:
        lines.append(
            "The trade-off landscape is still forming. The next review cycle will surface hidden costs once a prototype is running against real load.\n"
        )

    lines.append("## Direction\n")
    leads_to = _as_text(direction.get("leads_to", ""))
    if leads_to:
        lines.append(_expand_sentence(leads_to) + "\n")
    build_next = direction.get("build_next", [])
    if isinstance(build_next, list):
        for item in build_next[:6]:
            lines.append(_expand_sentence(_as_text(item)) + "\n")
    if not leads_to and not build_next:
        lines.append("Next actions will emerge once the build slice is scoped and a real done-test is locked.\n")

    lines.append("## Math Sidebar\n")
    if math:
        for f in math:
            name = f.get("name", "")
            latex = f.get("latex", "")
            desc = f.get("description", "")
            if name:
                lines.append(f"### {name}\n")
            if latex:
                lines.append(f"$$\n{latex}\n$$\n")
            if desc:
                lines.append(desc + "\n")
    else:
        lines.append("No formal models are attached to this card yet.\n")

    lines.append("## Conclusion\n")
    lines.append(
        f"{_gtm_positioning(card) or title}. If you are building in this space, start with the executable done-test in the engineering blueprint "
        "and iterate until the deterministic scorer agrees. The goal is not a perfect plan; it is a plan that can be proven wrong quickly."
    )

    essay = "\n".join(lines)
    wc = _word_count(essay)
    if wc < 1400:
        essay += "\n\n" + _build_expansion(card, 1500 - wc)
        wc = _word_count(essay)

    return {"essay": essay, "word_count": wc}


def generate_sales_slide(card_path: str | Path) -> dict[str, Any]:
    card = _load_card(card_path)
    headline = _gtm_positioning(card) or card.get("title", "RIG Capability")
    ideas = _revenue_ideas(card)
    if len(ideas) < 3:
        ideas += _competitive_moat(card)
    if len(ideas) < 3:
        ideas += _differentiation(card)
    ideas = ideas[:3]
    if not ideas:
        ideas = ["Turn the build card into a shipped capability", "Open-source the core and sell managed support", "License the pattern to partners"]

    slide = f"# {headline}\n\n"
    for idea in ideas:
        slide += f"- {idea}\n"
    slide += (
        f"\n---\n\n"
        f"**Proof:** Build-card score {_score_rank(card)} | "
        f"Council verdict: {_council_verdict(card) or 'Pending'} | "
        f"Pricing: {_price_range(card)}"
    )
    return {"slide": slide}


def generate_github_readme(card_path: str | Path) -> dict[str, Any]:
    card = _load_card(card_path)
    name = _idea_name(card)
    description = _as_text(card.get("summary", ""))[:600]
    components = _architecture_components(card)
    steps = _impl_steps(card)
    testing = _testing_strategy(card)

    lines: list[str] = [f"# {name}\n", f"{description}\n"]

    lines.append("## Architecture\n")
    if components:
        for c in components:
            lines.append(f"- {c}")
    else:
        lines.append("- Core Python service")
        lines.append("- SQLite / deterministic scorer")
        lines.append("- Prefect / Docker scheduler")
    lines.append("")

    lines.append("## Quick Start\n")
    if steps:
        for step in steps:
            lines.append(f"1. {step}")
    else:
        lines.append("1. Clone the repo and install dependencies.")
        lines.append("2. Run the deterministic scorer against the build card.")
        lines.append("3. Verify the done-test and seal the ProofPacket.")
    lines.append("")

    lines.append("## Testing\n")
    if testing:
        for k, v in testing.items():
            lines.append(f"- **{k}**: {v}")
    else:
        lines.append("- Unit tests with pytest")
        lines.append("- Integration tests against real artifacts on disk")
        lines.append("- Non-vacuity tests: plant a failure, confirm RED, restore")
    lines.append("")

    lines.append("## License\n")
    lines.append("MIT\n")

    return {"readme": "\n".join(lines), "project_name": name}


def generate_all(card_path: str | Path) -> dict[str, Any]:
    card = _load_card(card_path)
    return {
        "card_id": card.get("card_id") or Path(card_path).stem,
        "generated_at": utc_now(),
        "linkedin": generate_linkedin(card_path),
        "youtube": generate_youtube_script(card_path),
        "substack": generate_substack(card_path),
        "slide": generate_sales_slide(card_path),
        "readme": generate_github_readme(card_path),
    }


# ---------------------------------------------------------------------------
# Batch / status
# ---------------------------------------------------------------------------


def _content_dir(card_id: str) -> Path:
    return L2_ROOT / "content" / card_id


def batch_generate() -> dict[str, Any]:
    cards = sorted(L2_CARDS.glob("l2-*.json"))
    out: dict[str, Any] = {"cards": 0, "files": [], "errors": []}
    for path in cards:
        try:
            card = _load_card(path)
            cid = _card_id(card, path)
            generated = generate_all(path)
            base = _content_dir(cid)
            writes = {
                "linkedin.md": generated["linkedin"].get("content", ""),
                "youtube.md": generated["youtube"].get("script", ""),
                "substack.md": generated["substack"].get("essay", ""),
                "slide.md": generated["slide"].get("slide", ""),
                "readme.md": generated["readme"].get("readme", ""),
            }
            for fname, text in writes.items():
                fpath = base / fname
                atomic_text(fpath, text)
                out["files"].append(str(fpath))
            out["cards"] += 1
        except Exception as exc:
            out["errors"].append({str(path): repr(exc)})
    return out


def status() -> dict[str, Any]:
    cards = list(L2_CARDS.glob("l2-*.json"))
    content_root = L2_ROOT / "content"
    files = list(content_root.rglob("*.md")) if content_root.exists() else []
    return {
        "cards": len(cards),
        "content_files": len(files),
        "content_dir": str(content_root),
        "scanned_at": utc_now(),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OmniScout L2 content engine")
    sub = parser.add_subparsers(dest="cmd")

    for cmd in ("linkedin", "youtube", "substack", "slide", "readme", "all"):
        p = sub.add_parser(cmd)
        p.add_argument("card_path", help="Path to a V20 build-card JSON file")

    sub.add_parser("batch", help="Generate all content types for all V20 cards")
    sub.add_parser("status", help="Show card and content-file counts")

    args = parser.parse_args(argv)

    dispatch = {
        "linkedin": generate_linkedin,
        "youtube": generate_youtube_script,
        "substack": generate_substack,
        "slide": generate_sales_slide,
        "readme": generate_github_readme,
        "all": generate_all,
    }

    if args.cmd in dispatch:
        result = dispatch[args.cmd](args.card_path)
        print(stable_json(result))
    elif args.cmd == "batch":
        print(stable_json(batch_generate()))
    elif args.cmd == "status":
        print(stable_json(status()))
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
