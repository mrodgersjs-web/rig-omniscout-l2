# RIG OmniScout L2 — Build Card Intelligence Engine

The complete RIG capability intelligence system: research → enrichment → pattern recognition → auto-build → content → nightly automation.

## Architecture

```
PRODUCE → ENRICH (V2→V30) → AUTOBUILD → PATTERN ENGINES → CONTENT → NIGHTLY
```

## Modules (21 files, 15K+ LOC)

### Core Pipeline
| Module | Purpose | LOC |
|---|---|---:|
| `omniscout_build_cards.py` | Card producer + clusterer + scorer + Consensus MCP | ~2300 |
| `consensus_mcp.py` | Consensus.app MCP client (OAuth, 200M papers) | ~400 |
| `omniscout_l2_produce100.py` | Batch producer + nightly orchestrator | ~1100 |

### Enrichment Layers (V2→V30)
| Module | Version | What it adds |
|---|---|---|
| `omniscout_l2_enrich.py` | V2 | Analysis, direction, prompts, math formulas, tags, SVG images |
| `omniscout_l2_enrich_v3.py` | V3 | Entity graph, semantic links, contradictions, memory layer, proof seal |
| `omniscout_l2_v10.py` | V10 | Business intel, engineering blueprint, agent team, doctrine, GTM, OSS |
| `omniscout_l2_v20.py` | V20 | 6-perspective Council (business, marketing, competitive, product, AI, client) |
| `omniscout_l2_v30.py` | V30 | Deep sections: engineering, business, GTM, agents, research, risk, testing, ops |

### Auto-Build + Doctrine
| Module | Purpose |
|---|---|
| `omniscout_l2_autobuild.py` | Card → Python project (harness, gates, proof, tests) |
| `omniscout_l2_tac.py` | TAC v2 closed-loop wiring (CLAUDE.md, tac_loop.py, PITER.yaml) |
| `omniscout_l2_openspec.py` | OpenSpec BDD spec generation (Given/When/Then) |
| `omniscout_l2_l8.py` | L8 context delivery packets (fleet hydration) |
| `omniscout_l2_l10.py` | L10 verification (refutation, property tests, adherence, taste) |

### Pattern Recognition (1000x engines)
| Module | Purpose |
|---|---|
| `pattern_anticrowd.py` | Anti-Crowd Score — where to build where competitors can't |
| `pattern_contradiction.py` | Contradiction Arbitrage Rank — breakthrough prediction |
| `pattern_drift.py` | Epistemic drift — leading indicator detection |
| `pattern_generate.py` | Pattern card generator from engine signals |
| `pattern_dashboard.py` | HTML dashboard generator |

### Outcome + Feedback
| Module | Purpose |
|---|---|
| `omniscout_l2_outcomes.py` | Outcome tracking + Brier score computation |
| `omniscout_l2_feedback.py` | Adaptive scorer that learns from outcomes |
| `omniscout_l2_meta.py` | Meta-card synthesis + gap analysis |
| `omniscout_l2_content.py` | Content engine (LinkedIn, YouTube, Substack, sales, README) |
| `omniscout_l2_factory.py` | Factory/SSSF bridge (card → mission queue) |

## Quality Standards
- **Done bar:** ≥80/100 (deterministic scorer, no LLM in decision path)
- **Good bar:** ≥88/100
- **GEV separation:** Builder ≠ Verifier (always)
- **ProofPacket:** Hash-chained proof on every artifact
- **Non-vacuity:** Plant failure → confirm RED → restore → regression

## Council Perspectives (V20)
Every card gets 6 expert analyses:
1. Business Strategist — revenue, pricing, market timing
2. Marketing Director — positioning, content, channels
3. Competitive Intelligence — differentiation, moats, threats
4. Product Developer — MVP scope, feature priority, timeline
5. AI Architect — model selection, agent design, eval
6. Potential Client — pain point, willingness-to-pay, decision criteria

## Pattern Engine Formulas

### Anti-Crowd Score (ACS)
```
ACS = (E^1.5 × M × R × U) / (1 + C_risk)
E = emptiness, M = market size, R = regulatory barrier, U = RIG advantage
```

### Contradiction Arbitrage Rank (CAR)
```
CAR = (D × E × C × V) / (A + S)
D = density, E = evidence tension, C = centrality, V = velocity
```

### Epistemic Drift Score
```
Drift = frontier_ratio × 0.3 + bridge_score × 0.3 + velocity × 0.2 + cross_domain × 0.2
```

## Nightly Flow (Prefect)
```
00:05 MT → produce 100 cards → enrich V2→V30 → autobuild → 
  pattern engines → content → export → Obsidian + Memory OS + dashboard
```

## Requirements
- Python 3.11+
- Prefect 3.x
- Playwright (for Recall push)
- Ollama with qwen3-coder:30b (for builds)
- Consensus MCP (OAuth via mcp-remote)

## License
MIT
