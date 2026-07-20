# Hormuz Supply Chain Disruption Pipeline (Prototype)

A runnable implementation of the six-stage agent pipeline:

```
live signals (news / AIS / market)
        -> Geopolitical Risk Agent
        -> Supply Chain Digital Twin
        -> Disruption Simulator
        -> Procurement Orchestrator
        -> SPR Optimization Agent
        -> Recommendation Engine
```

This is a **prototype**. It runs end-to-end on real data — live news, AIS
and market feeds with cached and published-baseline fallbacks — and produces
a structured, internally-consistent recommendation. The agent *heuristics*
are still simple weighted functions (see below), so treat the output as a
pipeline-design demo grounded in real inputs, not an authoritative call
about the Strait of Hormuz.

## Run it

```bash
python main.py
python main.py --disruption 0.65 --seed 7
python main.py --disruption 1.0 --json full_output.json
```

`--disruption` is the fraction of Hormuz-route flow assumed lost (0.5 =
"Hormuz -50%"). `--seed` is deprecated and has no effect (all data paths are
real/deterministic — nothing is randomly generated). `--json` also dumps the
full structured `FinalRecommendation` object for programmatic use.

## How the stages map to files

| Stage | File | What it does |
|---|---|---|
| Live signals | `data_sources.py` | Live news/AIS/market feeds (with cached + baseline fallbacks) + real reference network (refineries, suppliers, SPR sites) |
| Geopolitical Risk Agent | `agents/geo_risk_agent.py` | Weighted-heuristic risk score per shipping route |
| Digital Twin | `agents/digital_twin.py` | Looks up which refineries/routes are exposed given the flagged region |
| Disruption Simulator | `agents/disruption_simulator.py` | Applies the disruption %, computes supply gap and rough price impact |
| Procurement Orchestrator | `agents/procurement_agent.py` | Matches alternative suppliers to refineries by crude compatibility (API gravity, sulfur) and route safety, greedily by largest shortfall |
| SPR Optimization Agent | `agents/spr_agent.py` | Proposes a reserve drawdown to bridge the gap until replacement cargoes arrive; flags infeasibility rather than overpromising |
| Recommendation Engine | `orchestrator.py` (`_build_action_items`) | Synthesizes everything into a plain-language action list |

Every stage communicates through plain dataclasses in `models.py` — treat
these as the API contracts between agents if this gets split into separately
deployed services later.

## A key design decision worth knowing

A Strait of Hormuz disruption doesn't hit one shipping lane — it hits every
lane that transits the strait at once. So `flagged_routes()` in the risk
agent identifies *all* routes matching the region of concern, and every
downstream stage (twin, simulator, procurement) treats all of them as down
together. Earlier drafts of this scored only the single highest-risk route
and let other Hormuz-transiting suppliers pose as "safe" alternatives — that
was a real bug, not a stylistic choice, and it's worth remembering if you
extend the region-matching logic.

## Data provenance & degradation

Nothing in the data layer is random. Every feed follows a three-tier
fallback, and each signal carries a `data_quality` flag telling you which
tier produced it:

| Feed | Live source | Cache file | Baseline (no cache) |
|---|---|---|---|
| News | GDELT DOC 2.0 API (keyless) | `.cache/gdelt_news.json` | Real dated historical Hormuz/Red Sea events |
| Hormuz AIS | straits.live free API (keyless) — real tanker counts + Bandar Abbas congestion | `.cache/straits_live.json` | Actual observed values (cited, dated) |
| Hormuz "normal" tanker baseline | Self-calibrating median of observed history (`.cache/ais_history.json`, 30-day window) | — | Cited constant (130) until enough history accumulates |
| Non-Hormuz routes (Cape / Red Sea) | *(no free per-vessel source exists)* | — | Static figures derived from published route volumes — always `baseline` |
| Brent/WTI | Yahoo Finance chart API (keyless), then EIA API v2 (optional free `EIA_API_KEY`) | `.cache/market.json` | Cited recent price levels |
| Freight rate index | Live proxy: 7-day move of listed crude-tanker owners (FRO/STNG/TNK via Yahoo) | `.cache/market.json` | 1.0 (= freight normal; BDTI paid-only) |

`data_quality` values: `live` (fetched this run) → `cached` (last known good,
replayed on fetch failure) → `baseline` (published/cited reference figure).

Reference data (`REFINERIES`, `SUPPLIERS`, `SPR_SITES`) is real published
data refreshed by hand: refinery/SPR capacities are official figures;
supplier volumes are grounded in India's published FY2024-25 import mix with
headroom derived from reported spare capacity; SPR fill levels are the last
publicly reported state (India doesn't publish live reserve levels). Each
constant carries its source in an inline comment in `data_sources.py`.

To upgrade any tier (e.g. paid AIS for the Cape route, Platts freight data,
internal ERP contract terms), only `data_sources.py` changes — the agent
logic (agent `*.py` files, `orchestrator.py`) depends only on the dataclass
shapes in `models.py`. The scoring heuristics in `geo_risk_agent.py` and the
price-impact formula in `disruption_simulator.py` are simple weighted
functions meant to be replaced with real models (e.g. a trained classifier,
or an LLM reasoning step over the same features) once you have real signal
quality to justify it.

## What this deliberately does NOT do

The SPR agent produces a *recommendation*, never an action. There is no code
path in this prototype that executes a drawdown, places a procurement order,
or moves money — a chokepoint-driven reserve release is a policy decision
that belongs to whoever holds real authority over the reserve, and any real
build of this should keep a human-in-the-loop approval step between stage 5
and any actual execution.
