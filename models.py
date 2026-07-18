"""
Shared data contracts passed between pipeline stages.

Every agent in this prototype communicates through plain dataclasses so the
"messages" between stages are inspectable, loggable, and serializable to
JSON. In a production build, these are the schemas you'd formalize as API
contracts (or Pydantic models) between independently deployed agents.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional
import json


def to_dict(obj) -> dict:
    return asdict(obj)


def to_json(obj) -> str:
    return json.dumps(asdict(obj), indent=2, default=str)


# ---------------------------------------------------------------------------
# Stage 0: raw signals (synthetic stand-ins for News/AIS/Market feeds)
# ---------------------------------------------------------------------------

@dataclass
class NewsSignal:
    headline: str
    source: str
    sentiment: float          # -1 (very negative/escalatory) .. +1 (de-escalatory)
    relevance: float          # 0..1, how relevant to the route/region
    region: str


@dataclass
class AISSignal:
    route: str
    tanker_count_normal: int
    tanker_count_current: int
    avg_transit_delay_hours: float
    port_congestion_index: float   # 0..1


@dataclass
class MarketSignal:
    benchmark: str            # e.g. "Brent", "Dubai"
    price_usd_bbl: float
    price_change_pct_7d: float
    freight_rate_index: float # 0..1 (relative to baseline)


# ---------------------------------------------------------------------------
# Stage 1: Geopolitical Risk Agent output
# ---------------------------------------------------------------------------

@dataclass
class RouteRiskScore:
    route: str
    risk_score: float                 # 0..1
    disruption_probability: float     # 0..1
    contributing_factors: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reference / static state used by the Digital Twin
# ---------------------------------------------------------------------------

@dataclass
class CrudeGrade:
    name: str
    api_gravity: float     # higher = lighter
    sulfur_pct: float       # higher = sourer


@dataclass
class Supplier:
    name: str
    country: str
    route: str              # which shipping route this supplier's crude travels
    crude: CrudeGrade
    max_additional_bbl_per_day: int
    lead_time_days: int
    hormuz_dependent: bool


@dataclass
class Refinery:
    name: str
    location: str
    capacity_bbl_per_day: int
    primary_route: str
    compatible_api_range: tuple      # (min, max)
    compatible_sulfur_max: float
    current_suppliers: list = field(default_factory=list)  # supplier names


@dataclass
class SPRSite:
    name: str
    location: str
    capacity_mmbbl: float
    current_fill_mmbbl: float
    max_draw_rate_bbl_per_day: int
    serves_refineries: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 2: Digital Twin exposure output
# ---------------------------------------------------------------------------

@dataclass
class ExposureReport:
    scenario: str
    exposed_refineries: list          # list of refinery names
    exposed_routes: list
    exposed_volume_bbl_per_day: int
    national_demand_bbl_per_day: int
    at_risk_suppliers: list = field(default_factory=list)   # suppliers shipping via an exposed route
    affected_spr_sites: list = field(default_factory=list)  # SPR sites backing an exposed refinery


# ---------------------------------------------------------------------------
# Stage 3: Disruption Simulator output
# ---------------------------------------------------------------------------

@dataclass
class RefineryImpact:
    refinery: str
    normal_intake_bbl_per_day: int
    lost_intake_bbl_per_day: int
    utilization_impact_pct: float


@dataclass
class ScenarioResult:
    scenario: str
    disruption_pct_of_route: float
    total_supply_gap_bbl_per_day: int
    supply_gap_pct_of_demand: float
    price_impact_usd_bbl: float
    refinery_impacts: list = field(default_factory=list)  # list[RefineryImpact]


# ---------------------------------------------------------------------------
# Stage 4: Procurement Orchestrator output
# ---------------------------------------------------------------------------

@dataclass
class ProcurementOption:
    refinery: str
    supplier: str
    route: str
    volume_bbl_per_day: int
    lead_time_days: int
    compatible: bool
    notes: str = ""


@dataclass
class ProcurementPlan:
    scenario: str
    options: list = field(default_factory=list)          # list[ProcurementOption]
    unmet_gap_bbl_per_day: int = 0
    days_until_first_cargo: Optional[int] = None


# ---------------------------------------------------------------------------
# Stage 5: SPR Optimization Agent output
# ---------------------------------------------------------------------------

@dataclass
class SPRRecommendation:
    scenario: str
    site_allocations: dict            # {site_name: bbl_per_day drawdown}
    total_draw_bbl_per_day: int
    draw_duration_days: int
    total_drawn_mmbbl: float
    replenish_after_day: int
    feasible: bool
    notes: str = ""


# ---------------------------------------------------------------------------
# Final synthesized recommendation
# ---------------------------------------------------------------------------

@dataclass
class FinalRecommendation:
    scenario: str
    risk_summary: str
    exposure: ExposureReport
    impact: ScenarioResult
    procurement: ProcurementPlan
    spr: SPRRecommendation
    action_items: list = field(default_factory=list)
    top_risk_route: str = ""
    top_risk_score: float = 0.0
