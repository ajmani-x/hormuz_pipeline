"""
Disruption Simulator
---------------------
Takes a scenario injection (e.g. "Hormuz -50% flow") applied to the exposed
part of the network and computes: total supply gap, gap as % of national
demand, a rough price impact, and per-refinery lost intake.
"""
from models import ScenarioResult, RefineryImpact
from data_sources import REFINERIES


def run_scenario(exposure_report, disruption_pct: float, market_signals) -> ScenarioResult:
    """
    disruption_pct: fraction (0..1) of flow lost on the exposed route(s),
    e.g. 0.5 for "Hormuz -50%".
    """
    exposed_names = set(exposure_report.exposed_refineries)
    refinery_impacts = []
    total_gap = 0

    for r in REFINERIES:
        if r.name in exposed_names:
            lost = int(r.capacity_bbl_per_day * disruption_pct)
            total_gap += lost
            refinery_impacts.append(RefineryImpact(
                refinery=r.name,
                normal_intake_bbl_per_day=r.capacity_bbl_per_day,
                lost_intake_bbl_per_day=lost,
                utilization_impact_pct=round(disruption_pct * 100, 1),
            ))

    gap_pct_of_demand = round(100 * total_gap / exposure_report.national_demand_bbl_per_day, 2)

    # crude price impact heuristic: bigger gap + already-stressed freight/price
    # market -> larger price move. Purely illustrative.
    avg_price_change = sum(m.price_change_pct_7d for m in market_signals) / max(len(market_signals), 1)
    price_impact = round((gap_pct_of_demand / 10.0) * (1 + avg_price_change / 100.0) * 5.0, 2)

    return ScenarioResult(
        scenario=exposure_report.scenario,
        disruption_pct_of_route=disruption_pct,
        total_supply_gap_bbl_per_day=total_gap,
        supply_gap_pct_of_demand=gap_pct_of_demand,
        price_impact_usd_bbl=price_impact,
        refinery_impacts=refinery_impacts,
    )
