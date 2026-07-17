"""
Geopolitical Risk Agent
-----------------------
Fuses news sentiment, AIS tanker-movement anomalies, and market-price signals
into a single risk score per shipping route. This is the entry point of the
pipeline: everything downstream keys off `disruption_probability`.

The scoring here is a simple weighted heuristic so the mechanics are
transparent. A production version would likely replace `_score_route` with
a trained model (e.g. gradient boosted trees or an LLM-based reasoning step
over the same features).
"""
from models import RouteRiskScore


def _news_component(news_signals, region_filter: str) -> float:
    relevant = [n for n in news_signals if region_filter.lower() in n.region.lower()]
    if not relevant:
        return 0.0
    # negative sentiment + high relevance -> higher risk
    weighted = [(-n.sentiment) * n.relevance for n in relevant]
    return max(0.0, min(1.0, sum(weighted) / len(weighted)))


def _ais_component(ais_signal) -> float:
    traffic_drop = 1 - (ais_signal.tanker_count_current / max(ais_signal.tanker_count_normal, 1))
    delay_norm = min(ais_signal.avg_transit_delay_hours / 72.0, 1.0)
    return max(0.0, min(1.0, 0.5 * traffic_drop + 0.3 * delay_norm + 0.2 * ais_signal.port_congestion_index))


def _market_component(market_signals) -> float:
    if not market_signals:
        return 0.0
    freight = sum(m.freight_rate_index for m in market_signals) / len(market_signals)
    price_move = sum(m.price_change_pct_7d for m in market_signals) / len(market_signals)
    return max(0.0, min(1.0, 0.5 * min(freight / 2.0, 1.0) + 0.5 * min(price_move / 20.0, 1.0)))


def score_routes(news_signals, ais_signals, market_signals, region_of_concern: str = "Hormuz") -> list:
    """Returns a RouteRiskScore per route present in ais_signals."""
    news_score = _news_component(news_signals, region_of_concern)
    market_score = _market_component(market_signals)

    results = []
    for ais in ais_signals:
        is_flagged_region = region_of_concern.lower() in ais.route.lower()
        ais_score = _ais_component(ais)

        if is_flagged_region:
            risk = 0.45 * news_score + 0.35 * ais_score + 0.20 * market_score
        else:
            # routes outside the flagged region still get some AIS/market signal
            # but no news weight, since the news feed is region-scoped
            risk = 0.6 * ais_score + 0.4 * market_score * 0.3

        risk = max(0.0, min(1.0, risk))
        factors = []
        if is_flagged_region:
            factors.append(f"news_sentiment_score={news_score:.2f}")
        factors.append(f"ais_anomaly_score={ais_score:.2f}")
        factors.append(f"market_stress_score={market_score:.2f}")

        results.append(RouteRiskScore(
            route=ais.route,
            risk_score=round(risk, 3),
            disruption_probability=round(risk, 3),  # in this simple model risk == P(disruption)
            contributing_factors=factors,
        ))
    return results


def top_risk(route_scores: list) -> RouteRiskScore:
    return max(route_scores, key=lambda r: r.risk_score)


def flagged_routes(route_scores: list, region_of_concern: str = "Hormuz") -> list:
    """All routes physically passing through the region of concern. A chokepoint
    disruption (e.g. Strait of Hormuz) hits every shipping lane through it at once,
    not just whichever single lane happens to show the strongest anomaly signal —
    so downstream stages should treat the whole region as exposed, not one route."""
    return [r for r in route_scores if region_of_concern.lower() in r.route.lower()]
