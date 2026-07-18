"""
Supply Chain Digital Twin
-------------------------
Answers: "given these at-risk routes, what in our network is actually
exposed?" — refineries, suppliers, and SPR sites — by traversing the supply
graph (supply_graph.py) instead of a single flat lookup, so exposure follows
real multi-hop relationships (route -> refinery -> SPR site, and
route <- supplier) rather than just the one route->refinery join.

A production twin would be a live, continuously-updated graph (refinery tank
levels, in-transit cargo positions, contract volumes) rather than the fixed
reference data in data_sources.py — but the traversal logic here would stay
the same.
"""
from models import ExposureReport
from data_sources import REFINERIES, NATIONAL_DEMAND_BBL_PER_DAY
from supply_graph import SUPPLY_GRAPH


def get_exposed_refineries(routes: list) -> list:
    exposed_names = set(SUPPLY_GRAPH.reachable(routes, node_type="refinery"))
    return [r for r in REFINERIES if r.name in exposed_names]


def assess_exposure(route_risks: list, scenario_name: str) -> ExposureReport:
    """route_risks: list of RouteRiskScore for every route caught up in the scenario
    (e.g. all Hormuz-* routes when the scenario is a Strait of Hormuz disruption —
    a chokepoint event doesn't discriminate between shipping lanes through it)."""
    routes = [rr.route for rr in route_risks]
    exposed = get_exposed_refineries(routes)
    exposed_volume = sum(r.capacity_bbl_per_day for r in exposed)

    # Multi-hop queries the old flat lookup couldn't answer: which suppliers
    # ship via a disrupted route (route <- supplier), and which SPR sites sit
    # downstream of an exposed refinery (route -> refinery -> spr_site).
    at_risk_suppliers = sorted({
        supplier for route in routes for supplier in SUPPLY_GRAPH.predecessors(route, "ships_via")
    })
    affected_spr_sites = sorted(SUPPLY_GRAPH.reachable(routes, node_type="spr_site"))

    return ExposureReport(
        scenario=scenario_name,
        exposed_refineries=[r.name for r in exposed],
        exposed_routes=routes,
        exposed_volume_bbl_per_day=exposed_volume,
        national_demand_bbl_per_day=NATIONAL_DEMAND_BBL_PER_DAY,
        at_risk_suppliers=at_risk_suppliers,
        affected_spr_sites=affected_spr_sites,
    )
