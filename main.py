"""
CLI entry point.

Usage:
    python main.py
    python main.py --disruption 0.65 --seed 7
    python main.py --json out.json
"""
import argparse
import json

from orchestrator import run_pipeline
from models import to_dict


def render_report(rec) -> str:
    lines = []
    lines.append("=" * 78)
    lines.append(f"SCENARIO: {rec.scenario}")
    lines.append("=" * 78)
    lines.append("")
    lines.append("[1] GEOPOLITICAL RISK")
    lines.append(f"  {rec.risk_summary}")
    lines.append("")
    lines.append("[2] EXPOSURE (Digital Twin)")
    lines.append(f"  Exposed routes:     {', '.join(rec.exposure.exposed_routes)}")
    lines.append(f"  Exposed refineries: {', '.join(rec.exposure.exposed_refineries)}")
    lines.append(f"  Exposed volume:     {rec.exposure.exposed_volume_bbl_per_day:,} bbl/day")
    lines.append(f"  At-risk suppliers:  {', '.join(rec.exposure.at_risk_suppliers) or 'none'}")
    lines.append(f"  Affected SPR sites: {', '.join(rec.exposure.affected_spr_sites) or 'none'}")
    lines.append("")
    lines.append("[3] DISRUPTION IMPACT")
    lines.append(f"  Route flow reduction:   {rec.impact.disruption_pct_of_route:.0%}")
    lines.append(f"  Total supply gap:       {rec.impact.total_supply_gap_bbl_per_day:,} bbl/day "
                  f"({rec.impact.supply_gap_pct_of_demand}% of national demand)")
    lines.append(f"  Estimated price impact: +${rec.impact.price_impact_usd_bbl}/bbl")
    for ri in rec.impact.refinery_impacts:
        lines.append(f"    - {ri.refinery}: -{ri.lost_intake_bbl_per_day:,} bbl/day "
                      f"({ri.utilization_impact_pct}% utilization hit)")
    lines.append("")
    lines.append("[4] PROCUREMENT PLAN")
    for opt in rec.procurement.options:
        if opt.compatible:
            lines.append(f"    - {opt.refinery} <- {opt.supplier}: {opt.volume_bbl_per_day:,} bbl/day, "
                          f"{opt.lead_time_days}d lead time ({opt.notes})")
        else:
            lines.append(f"    - {opt.refinery}: {opt.volume_bbl_per_day:,} bbl/day UNMATCHED ({opt.notes})")
    lines.append(f"  Unmet gap:            {rec.procurement.unmet_gap_bbl_per_day:,} bbl/day")
    lines.append(f"  First cargo arrives:  day {rec.procurement.days_until_first_cargo}")
    lines.append("")
    lines.append("[5] SPR RECOMMENDATION")
    for site, vol in rec.spr.site_allocations.items():
        if vol > 0:
            lines.append(f"    - {site}: release {vol:,} bbl/day")
    lines.append(f"  Total draw:      {rec.spr.total_draw_bbl_per_day:,} bbl/day for "
                  f"{rec.spr.draw_duration_days} days ({rec.spr.total_drawn_mmbbl} MMbbl total)")
    lines.append(f"  Replenish after: day {rec.spr.replenish_after_day}")
    lines.append(f"  Feasible:        {'YES' if rec.spr.feasible else 'NO — escalate'}")
    lines.append(f"  Notes: {rec.spr.notes}")
    lines.append("")
    lines.append("[6] RECOMMENDED ACTIONS")
    for i, item in enumerate(rec.action_items, 1):
        lines.append(f"  {i}. {item}")
    lines.append("")
    lines.append("=" * 78)
    lines.append("DATA: news (GDELT), Brent/WTI (Yahoo/EIA), Hormuz AIS (straits.live) and the")
    lines.append("freight proxy are live; on fetch failure the last-known-good cache is used,")
    lines.append("then cited published baselines. Reference figures (refineries/suppliers/SPR)")
    lines.append("are real published data, refreshed by hand. Nothing is random. Each signal")
    lines.append("carries a data_quality flag: live | cached | baseline.")
    lines.append("=" * 78)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Hormuz supply-chain disruption pipeline (prototype)")
    parser.add_argument("--scenario", default="Strait of Hormuz flow disruption")
    parser.add_argument("--disruption", type=float, default=0.5, help="Fraction of route flow lost, e.g. 0.5")
    parser.add_argument("--region", default="Hormuz")
    parser.add_argument("--seed", type=int, default=42,
                        help="Deprecated — no effect (all data paths are real/deterministic)")
    parser.add_argument("--json", metavar="PATH", help="Also write full structured output to this JSON path")
    args = parser.parse_args()

    rec = run_pipeline(
        scenario_name=args.scenario,
        disruption_pct=args.disruption,
        region_of_concern=args.region,
        seed=args.seed,
    )

    print(render_report(rec))

    if args.json:
        with open(args.json, "w") as f:
            json.dump(to_dict(rec), f, indent=2, default=str)
        print(f"\nFull structured output written to {args.json}")


if __name__ == "__main__":
    main()
