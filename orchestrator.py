"""
Orchestrator
------------
Chains the six stages together exactly as in the architecture diagram:

  live signals -> Geopolitical Risk Agent -> Supply Chain Digital Twin
  -> Disruption Simulator -> Procurement Orchestrator -> SPR Agent
  -> Recommendation Engine

Run this via main.py, or import `run_pipeline` directly.
"""
from models import FinalRecommendation
from data_sources import generate_news_signals, generate_ais_signals, generate_market_signals
import geo_risk_agent, digital_twin, disruption_simulator, procurement_agent, spr_agent


def run_pipeline(scenario_name: str = "Hormuz disruption scenario",
                  disruption_pct: float = 0.5,
                  region_of_concern: str = "Hormuz",
                  seed: int = 42) -> FinalRecommendation:

    # Stage 0: pull (synthetic) live signals
    news = generate_news_signals(seed=seed)
    ais = generate_ais_signals(seed=seed)
    market = generate_market_signals(seed=seed)

    # Stage 1: Geopolitical Risk Agent
    route_scores = geo_risk_agent.score_routes(news, ais, market, region_of_concern=region_of_concern)
    top = geo_risk_agent.top_risk(route_scores)
    # A chokepoint disruption hits every lane through the region at once, so
    # exposure/impact/procurement all treat the whole flagged region as down —
    # not just whichever single route showed the strongest anomaly signal.
    at_risk_routes = geo_risk_agent.flagged_routes(route_scores, region_of_concern=region_of_concern)

    # Stage 2: Supply Chain Digital Twin
    exposure = digital_twin.assess_exposure(at_risk_routes, scenario_name)

    # Stage 3: Disruption Simulator
    impact = disruption_simulator.run_scenario(exposure, disruption_pct, market)

    # Stage 4: Procurement Orchestrator
    plan = procurement_agent.build_plan(impact, disrupted_routes=[r.route for r in at_risk_routes])

    # Stage 5: SPR Optimization Agent
    spr_rec = spr_agent.recommend_drawdown(impact, plan)

    # Stage 6: Recommendation Engine (synthesis)
    action_items = _build_action_items(top, impact, plan, spr_rec)

    risk_summary = (
        f"Route '{top.route}' flagged at {top.risk_score:.0%} risk "
        f"({top.disruption_probability:.0%} disruption probability). "
        f"Factors: {', '.join(top.contributing_factors)}."
    )

    return FinalRecommendation(
        scenario=scenario_name,
        risk_summary=risk_summary,
        exposure=exposure,
        impact=impact,
        procurement=plan,
        spr=spr_rec,
        action_items=action_items,
    )


def _build_action_items(top_risk, impact, plan, spr_rec) -> list:
    items = []
    items.append(
        f"Monitor '{top_risk.route}' — risk score {top_risk.risk_score:.0%}. "
        f"Escalate if disruption probability exceeds 75%."
    )
    items.append(
        f"Expect a supply gap of {impact.total_supply_gap_bbl_per_day:,} bbl/day "
        f"({impact.supply_gap_pct_of_demand}% of national demand); "
        f"est. price impact +${impact.price_impact_usd_bbl}/bbl."
    )
    matched = [o for o in plan.options if o.compatible]
    if matched:
        by_supplier = {}
        for o in matched:
            by_supplier.setdefault(o.supplier, 0)
            by_supplier[o.supplier] += o.volume_bbl_per_day
        for supplier, vol in by_supplier.items():
            items.append(f"Procure {vol:,} bbl/day from {supplier} (fastest compatible alternative).")
    if plan.unmet_gap_bbl_per_day > 0:
        items.append(
            f"{plan.unmet_gap_bbl_per_day:,} bbl/day remains uncovered by alternative "
            f"procurement — route to SPR release."
        )
    if spr_rec.total_draw_bbl_per_day > 0:
        sites = ", ".join(f"{name} ({vol:,} bbl/day)" for name, vol in spr_rec.site_allocations.items() if vol > 0)
        items.append(
            f"Release {spr_rec.total_draw_bbl_per_day:,} bbl/day from SPR ({sites}) "
            f"for {spr_rec.draw_duration_days} days, pending human sign-off."
        )
        items.append(f"Begin replenishing SPR sites after day {spr_rec.replenish_after_day} once cargoes land.")
    if not spr_rec.feasible:
        items.append("WARNING: proposed SPR draw does not cover the bridge period — escalate for policy decision.")
    return items
