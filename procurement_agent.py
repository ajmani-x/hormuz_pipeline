"""
Procurement Orchestrator
-------------------------
For each refinery with lost intake, searches available suppliers for
volume that is (a) not exposed to the same disrupted route and (b)
crude-compatible with that refinery's unit configuration (API gravity /
sulfur tolerance). Greedily allocates supplier capacity across refineries
in order of largest shortfall first.
"""
from ..models import ProcurementPlan, ProcurementOption
from ..data_sources import REFINERIES, SUPPLIERS


def _is_compatible(refinery, supplier) -> bool:
    lo, hi = refinery.compatible_api_range
    api_ok = lo <= supplier.crude.api_gravity <= hi
    sulfur_ok = supplier.crude.sulfur_pct <= refinery.compatible_sulfur_max
    return api_ok and sulfur_ok


def build_plan(scenario_result, disrupted_routes) -> ProcurementPlan:
    """disrupted_routes: a route name (str) or an iterable of route names that
    are all considered unavailable for this scenario (e.g. every Hormuz-* lane
    during a Strait of Hormuz disruption)."""
    if isinstance(disrupted_routes, str):
        disrupted_routes = {disrupted_routes}
    else:
        disrupted_routes = set(disrupted_routes)

    refinery_lookup = {r.name: r for r in REFINERIES}
    # suppliers not on any disrupted route are candidates; remaining capacity
    # tracked as we allocate across refineries
    candidate_suppliers = [s for s in SUPPLIERS if s.route not in disrupted_routes]
    remaining_capacity = {s.name: s.max_additional_bbl_per_day for s in candidate_suppliers}

    options = []
    unmet = 0
    first_cargo_days = None

    # Largest shortfall first, so critical refineries get first claim on
    # scarce alternative-supplier capacity.
    impacts_sorted = sorted(scenario_result.refinery_impacts, key=lambda i: -i.lost_intake_bbl_per_day)

    for impact in impacts_sorted:
        refinery = refinery_lookup[impact.refinery]
        needed = impact.lost_intake_bbl_per_day

        compatible = [s for s in candidate_suppliers if _is_compatible(refinery, s)]
        compatible.sort(key=lambda s: s.lead_time_days)  # prefer fastest delivery

        for supplier in compatible:
            if needed <= 0:
                break
            available = remaining_capacity.get(supplier.name, 0)
            if available <= 0:
                continue
            take = min(available, needed)
            remaining_capacity[supplier.name] -= take
            needed -= take
            options.append(ProcurementOption(
                refinery=refinery.name,
                supplier=supplier.name,
                route=supplier.route,
                volume_bbl_per_day=take,
                lead_time_days=supplier.lead_time_days,
                compatible=True,
                notes=f"{supplier.crude.name} crude, {supplier.lead_time_days}d transit via {supplier.route}",
            ))
            if first_cargo_days is None or supplier.lead_time_days < first_cargo_days:
                first_cargo_days = supplier.lead_time_days

        if needed > 0:
            unmet += needed
            options.append(ProcurementOption(
                refinery=refinery.name,
                supplier="UNMATCHED",
                route="n/a",
                volume_bbl_per_day=needed,
                lead_time_days=-1,
                compatible=False,
                notes="No compatible alternative supplier capacity found; candidate for SPR coverage.",
            ))

    return ProcurementPlan(
        scenario=scenario_result.scenario,
        options=options,
        unmet_gap_bbl_per_day=unmet,
        days_until_first_cargo=first_cargo_days,
    )
