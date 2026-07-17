"""
SPR Optimization Agent
-----------------------
Decides how much strategic reserve to release, from which sites, and for how
long, to bridge the gap between "disruption hits" and "replacement cargoes
physically arrive" (procurement lead time). This is deliberately the most
conservative agent in the pipeline: it proposes a release plan and flags
feasibility, but never executes anything -- a drawdown decision belongs to
whoever holds real authority over the reserve.
"""
import math

from ..models import SPRRecommendation
from ..data_sources import SPR_SITES


def recommend_drawdown(scenario_result, procurement_plan) -> SPRRecommendation:
    total_gap = scenario_result.total_supply_gap_bbl_per_day
    bridge_days = procurement_plan.days_until_first_cargo or 14  # fallback assumption

    affected_refineries = {i.refinery for i in scenario_result.refinery_impacts}
    relevant_sites = [s for s in SPR_SITES if affected_refineries & set(s.serves_refineries)]

    if not relevant_sites:
        return SPRRecommendation(
            scenario=scenario_result.scenario,
            site_allocations={},
            total_draw_bbl_per_day=0,
            draw_duration_days=0,
            total_drawn_mmbbl=0.0,
            replenish_after_day=0,
            feasible=False,
            notes="No SPR site maps to the affected refineries in the reference network.",
        )

    # Allocate draw proportionally to each site's max draw rate, capped by
    # what's actually needed and by each site's remaining fill.
    total_max_rate = sum(s.max_draw_rate_bbl_per_day for s in relevant_sites)
    allocations = {}
    remaining_need = total_gap

    for site in sorted(relevant_sites, key=lambda s: -s.max_draw_rate_bbl_per_day):
        share = site.max_draw_rate_bbl_per_day / total_max_rate if total_max_rate else 0
        proposed = min(site.max_draw_rate_bbl_per_day, math.ceil(total_gap * share))
        site_capacity_bbl_per_day_over_bridge = (site.current_fill_mmbbl * 1_000_000) / max(bridge_days, 1)
        proposed = int(min(proposed, site_capacity_bbl_per_day_over_bridge, remaining_need))
        proposed = max(proposed, 0)
        allocations[site.name] = proposed
        remaining_need -= proposed

    total_draw = sum(allocations.values())
    total_drawn_mmbbl = round((total_draw * bridge_days) / 1_000_000, 3)
    feasible = total_draw >= total_gap * 0.6  # covers most of the gap -> workable bridge

    notes = (
        f"Covers {round(100 * total_draw / total_gap, 1) if total_gap else 0}% of the "
        f"{total_gap:,} bbl/day gap for {bridge_days} days, until first replacement cargo "
        f"lands. Replenish reserve sites once procurement volumes are flowing."
    )

    return SPRRecommendation(
        scenario=scenario_result.scenario,
        site_allocations=allocations,
        total_draw_bbl_per_day=total_draw,
        draw_duration_days=bridge_days,
        total_drawn_mmbbl=total_drawn_mmbbl,
        replenish_after_day=bridge_days,
        feasible=feasible,
        notes=notes,
    )
