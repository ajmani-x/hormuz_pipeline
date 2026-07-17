"""
Synthetic stand-ins for live data feeds, plus static reference data describing
an illustrative Indian refinery / crude supply network.

IMPORTANT: The reference numbers here (capacities, dependencies, reserve
volumes) are illustrative placeholders for demo purposes, NOT sourced from a
live dataset. To make this a real system, swap the functions in this file for
real integrations:

  - generate_news_signals()   -> news/geopolitics API + NLP sentiment model
  - generate_ais_signals()    -> AIS tanker tracking provider (e.g. Kpler, MarineTraffic)
  - generate_market_signals() -> market data provider (e.g. Refinitiv, Platts)
  - REFINERIES / SUPPLIERS / SPR_SITES -> internal ERP / national statistics

Everything downstream (agents/, orchestrator.py) only depends on the
dataclass shapes in models.py, so swapping the data layer does not require
touching agent logic.
"""
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .models import NewsSignal, AISSignal, MarketSignal, CrudeGrade, Supplier, Refinery, SPRSite

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_DEFAULT_QUERY = '(Hormuz OR "Strait of Hormuz") (tanker OR oil OR shipping OR Iran OR Gulf)'

ROUTES = [
    "Hormuz-Gujarat",
    "Hormuz-Kochi",
    "Hormuz-Mumbai",
    "CapeRoute-EastCoast",
    "RedSea-WestCoast",
]

# --- Crude grades -----------------------------------------------------------

ARAB_LIGHT = CrudeGrade("Arab Light", api_gravity=33.0, sulfur_pct=1.8)
UPPER_ZAKUM = CrudeGrade("Upper Zakum", api_gravity=34.0, sulfur_pct=1.7)
BASRA_LIGHT = CrudeGrade("Basra Light", api_gravity=30.0, sulfur_pct=2.5)
URALS = CrudeGrade("Urals", api_gravity=31.0, sulfur_pct=1.5)
WTI = CrudeGrade("WTI Midland", api_gravity=40.0, sulfur_pct=0.4)
BONNY_LIGHT = CrudeGrade("Bonny Light", api_gravity=35.0, sulfur_pct=0.2)

# --- Suppliers (route + lead time determine substitutability) --------------

SUPPLIERS = [
    Supplier("Saudi Aramco", "Saudi Arabia", "Hormuz-Gujarat", ARAB_LIGHT, 300_000, 10, True),
    Supplier("ADNOC", "UAE", "Hormuz-Kochi", UPPER_ZAKUM, 200_000, 9, True),
    Supplier("Iraq SOMO", "Iraq", "Hormuz-Mumbai", BASRA_LIGHT, 150_000, 11, True),
    Supplier("Rosneft", "Russia", "CapeRoute-EastCoast", URALS, 400_000, 21, False),
    Supplier("US Gulf Exporters", "USA", "CapeRoute-EastCoast", WTI, 150_000, 24, False),
    Supplier("NNPC", "Nigeria", "RedSea-WestCoast", BONNY_LIGHT, 100_000, 18, False),
]

# --- Refineries --------------------------------------------------------------

REFINERIES = [
    Refinery(
        "Reliance Jamnagar", "Gujarat", 1_240_000, "Hormuz-Gujarat",
        compatible_api_range=(28.0, 42.0), compatible_sulfur_max=3.0,
        current_suppliers=["Saudi Aramco"],
    ),
    Refinery(
        "Nayara Vadinar", "Gujarat", 400_000, "Hormuz-Gujarat",
        compatible_api_range=(30.0, 36.0), compatible_sulfur_max=2.0,
        current_suppliers=["Saudi Aramco"],
    ),
    Refinery(
        "BPCL Kochi", "Kerala", 310_000, "Hormuz-Kochi",
        compatible_api_range=(30.0, 36.0), compatible_sulfur_max=2.0,
        current_suppliers=["ADNOC"],
    ),
    Refinery(
        "HPCL Mumbai", "Maharashtra", 190_000, "Hormuz-Mumbai",
        compatible_api_range=(28.0, 38.0), compatible_sulfur_max=2.6,
        current_suppliers=["Iraq SOMO"],
    ),
    Refinery(
        "IOC Paradip", "Odisha", 300_000, "CapeRoute-EastCoast",
        compatible_api_range=(28.0, 42.0), compatible_sulfur_max=2.0,
        current_suppliers=["Rosneft"],
    ),
]

NATIONAL_DEMAND_BBL_PER_DAY = 5_000_000  # illustrative

# --- Strategic Petroleum Reserve sites --------------------------------------

SPR_SITES = [
    SPRSite("Vizag", "Andhra Pradesh", capacity_mmbbl=9.9, current_fill_mmbbl=9.0,
            max_draw_rate_bbl_per_day=120_000, serves_refineries=["HPCL Mumbai", "IOC Paradip"]),
    SPRSite("Mangalore", "Karnataka", capacity_mmbbl=11.3, current_fill_mmbbl=10.0,
            max_draw_rate_bbl_per_day=150_000, serves_refineries=["BPCL Kochi", "Reliance Jamnagar"]),
    SPRSite("Padur", "Karnataka", capacity_mmbbl=18.3, current_fill_mmbbl=16.0,
            max_draw_rate_bbl_per_day=180_000, serves_refineries=["Reliance Jamnagar", "Nayara Vadinar"]),
]


# ---------------------------------------------------------------------------
# Live news feed (GDELT Project) + remaining synthetic feed generators
# ---------------------------------------------------------------------------

def _gdelt_get(params: dict, timeout: float) -> dict:
    url = f"{GDELT_DOC_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "hormuz-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fetch_gdelt_articles(query: str, maxrecords: int, timeout: float) -> list:
    data = _gdelt_get({
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": maxrecords,
        "sort": "hybridrel",
    }, timeout)
    return data.get("articles", [])


def _fetch_gdelt_tone(query: str, timeout: float) -> float:
    """Coverage-weighted average tone (~-10..+10) for the query, from GDELT's
    tone histogram. Returns 0.0 (neutral) if GDELT has no tone data for it."""
    data = _gdelt_get({"query": query, "mode": "tonechart", "format": "json"}, timeout)
    bins = data.get("tonechart", [])
    total_count = sum(b.get("count", 0) for b in bins)
    if total_count == 0:
        return 0.0
    weighted = sum(b.get("bin", 0) * b.get("count", 0) for b in bins)
    return weighted / total_count


def generate_news_signals(seed: int = None, region: str = "Strait of Hormuz",
                           query: str = None, maxrecords: int = 8, timeout: float = 15.0) -> list:
    """Live news signals from the GDELT Project (api.gdeltproject.org) — real
    articles matching `query`, scored with GDELT's real aggregate tone for
    that query. No synthetic data is used when the fetch succeeds.

    `seed` only matters for the synthetic fallback below; a live feed has
    nothing to seed against.

    GDELT rate-limits anonymous callers to ~1 request/5s, so the two calls
    this makes (article list, tone histogram) are spaced out accordingly. Any
    failure (network, rate limit, malformed response, no results) falls back
    to the synthetic generator so the rest of the pipeline still runs.
    """
    q = query or GDELT_DEFAULT_QUERY
    try:
        articles = _fetch_gdelt_articles(q, maxrecords, timeout)
        time.sleep(5)  # respect GDELT's ~1 req/5s rate limit before the 2nd call
        avg_tone = _fetch_gdelt_tone(q, timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        print(f"[hormuz_pipeline] GDELT fetch failed ({exc}); falling back to synthetic news signals.",
              file=sys.stderr)
        return _synthetic_news_signals(seed)

    if not articles:
        print("[hormuz_pipeline] GDELT returned no articles for this query; "
              "falling back to synthetic news signals.", file=sys.stderr)
        return _synthetic_news_signals(seed)

    sentiment = max(-1.0, min(1.0, avg_tone / 10.0))
    signals = []
    for i, art in enumerate(articles):
        relevance = round(max(0.5, 1.0 - i * 0.05), 2)  # rank decay; GDELT sorted by hybridrel
        signals.append(NewsSignal(
            headline=art.get("title") or "(untitled)",
            source=art.get("domain", "unknown"),
            sentiment=round(sentiment, 3),
            relevance=relevance,
            region=region,
        ))
    return signals


def _synthetic_news_signals(seed: int = None) -> list:
    rng = random.Random(seed)
    headlines = [
        ("Iran-linked forces seize tanker near Strait of Hormuz", "Reuters", -0.8),
        ("Gulf states hold naval exercises amid rising tension", "AP", -0.5),
        ("Sanctions tighten on Iranian oil exports", "Bloomberg", -0.6),
        ("Diplomatic talks aim to de-escalate Gulf tensions", "AFP", 0.4),
        ("Insurance premiums for Hormuz transits spike", "Lloyd's List", -0.7),
    ]
    signals = []
    for headline, source, base_sentiment in headlines:
        signals.append(NewsSignal(
            headline=headline,
            source=source,
            sentiment=base_sentiment + rng.uniform(-0.1, 0.1),
            relevance=rng.uniform(0.7, 1.0),
            region="Strait of Hormuz",
        ))
    return signals


def generate_ais_signals(seed: int = None) -> list:
    rng = random.Random(seed)
    signals = []
    for route in ROUTES:
        is_hormuz = route.startswith("Hormuz")
        normal = rng.randint(15, 25)
        drop = rng.uniform(0.3, 0.6) if is_hormuz else rng.uniform(0.0, 0.05)
        current = int(normal * (1 - drop))
        signals.append(AISSignal(
            route=route,
            tanker_count_normal=normal,
            tanker_count_current=current,
            avg_transit_delay_hours=rng.uniform(20, 60) if is_hormuz else rng.uniform(0, 5),
            port_congestion_index=rng.uniform(0.5, 0.9) if is_hormuz else rng.uniform(0.1, 0.3),
        ))
    return signals


def generate_market_signals(seed: int = None) -> list:
    rng = random.Random(seed)
    return [
        MarketSignal("Brent", price_usd_bbl=rng.uniform(85, 105), price_change_pct_7d=rng.uniform(5, 18),
                     freight_rate_index=rng.uniform(1.2, 1.8)),
        MarketSignal("Dubai", price_usd_bbl=rng.uniform(83, 103), price_change_pct_7d=rng.uniform(5, 18),
                     freight_rate_index=rng.uniform(1.3, 2.0)),
    ]
