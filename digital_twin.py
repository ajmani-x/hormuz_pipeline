"""
Supply Chain Digital Twin
-------------------------
Holds the current-state model of refineries, suppliers, and routes, and
answers: "given this risk score on this route, what in our network is
actually exposed?"

This is intentionally a static-graph lookup here (route -> refineries that
depend on it). A production twin would be a live, continuously-updated graph
(refinery tank levels, in-transit cargo positions, contract volumes) rather
than the fixed reference data in data_sources.py.
"""
from models import ExposureReport
from data_sources import REFINERIES, NATIONAL_DEMAND_BBL_PER_DAY


def get_exposed_refineries(routes: list) -> list:
    route_set = set(routes)
    return [r for r in REFINERIES if r.primary_route in route_set]


def assess_exposure(route_risks: list, scenario_name: str) -> ExposureReport:
    """route_risks: list of RouteRiskScore for every route caught up in the scenario
    (e.g. all Hormuz-* routes when the scenario is a Strait of Hormuz disruption —
    a chokepoint event doesn't discriminate between shipping lanes through it)."""
    routes = [rr.route for rr in route_risks]
    exposed = get_exposed_refineries(routes)
    exposed_volume = sum(r.capacity_bbl_per_day for r in exposed)

    return ExposureReport(
        scenario=scenario_name,
        exposed_refineries=[r.name for r in exposed],
        exposed_routes=routes,
        exposed_volume_bbl_per_day=exposed_volume,
        national_demand_bbl_per_day=NATIONAL_DEMAND_BBL_PER_DAY,
    )
