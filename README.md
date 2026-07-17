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

This is a **mechanics demo**. It runs end-to-end and produces a structured,
internally-consistent recommendation, but every input is synthetic. It is
meant to prove out the pipeline design and give you something to build
against, not to make a real call about the Strait of Hormuz.

## Run it

```bash
python main.py
python main.py --disruption 0.65 --seed 7
python main.py --disruption 1.0 --json full_output.json
```

`--disruption` is the fraction of Hormuz-route flow assumed lost (0.5 =
"Hormuz -50%"). `--seed` controls the synthetic signal generator so you can
get reproducible or varied scenarios. `--json` also dumps the full structured
`FinalRecommendation` object for programmatic use.

## How the stages map to files

| Stage | File | What it does |
|---|---|---|
| Live signals | `data_sources.py` | Synthetic news/AIS/market generators + static reference network (refineries, suppliers, SPR sites) |
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

## What's fake here, and what to plug in for a real system

Everything in `data_sources.py` is illustrative — refinery capacities,
supplier lead times, SPR fill levels, and the signal generators are all
synthetic. To make this real:

- `generate_news_signals()` → a news/geopolitical risk API + sentiment model
- `generate_ais_signals()` → a tanker-tracking provider (e.g. Kpler, MarineTraffic, Windward)
- `generate_market_signals()` → a market data provider (e.g. Platts, Refinitiv)
- `REFINERIES` / `SUPPLIERS` / `SPR_SITES` → internal ERP data, contract terms, and actual national reserve figures

None of the agent logic (`agents/*.py`, `orchestrator.py`) needs to change to
swap in real data — it only depends on the dataclass shapes in `models.py`.
The scoring heuristics in `geo_risk_agent.py` and the price-impact formula in
`disruption_simulator.py` are simple weighted functions meant to be replaced
with real models (e.g. a trained classifier, or an LLM reasoning step over
the same features) once you have real signal quality to justify it.

## What this deliberately does NOT do

The SPR agent produces a *recommendation*, never an action. There is no code
path in this prototype that executes a drawdown, places a procurement order,
or moves money — a chokepoint-driven reserve release is a policy decision
that belongs to whoever holds real authority over the reserve, and any real
build of this should keep a human-in-the-loop approval step between stage 5
and any actual execution.
