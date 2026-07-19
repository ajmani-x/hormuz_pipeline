"""
Live feeds (news, market prices) plus reference data describing India's
refinery / crude supply network. Data provenance, stage by stage:

  - generate_news_signals()   -> REAL, live (GDELT DOC 2.0 API)
  - generate_market_signals() -> REAL, live (Yahoo Finance quote API)
  - generate_ais_signals()    -> REAL (for Hormuz-* routes), live (straits.live's
    free public API: real AIS-derived tanker counts + real Bandar Abbas port
    congestion). Kpler/MarineTraffic/Windward are still paid-only, but this
    free aggregator covers what we need. Non-Hormuz routes and the
    tanker_count_normal baseline have no free equivalent and stay synthetic.
  - REFINERIES / SPR_SITES capacities -> REAL, but static (public figures,
    sourced below; refineries expand and capacity numbers only change every
    few years, so this is refreshed by hand, not fetched live).
  - SPR_SITES current_fill_mmbbl -> synthetic. India's government does not
    publish live strategic-reserve fill levels (reserve levels are
    security-sensitive) — the most recent public figures found were from
    2014, so a plausible "mostly full" fill is assumed instead.
  - SUPPLIERS -> the named companies/countries (Iraq SOMO, Saudi Aramco,
    ADNOC, Rosneft, US Gulf, NNPC) are India's real major crude trading
    partners; their volumes/lead times/crude grades are illustrative
    approximations, not contract figures (those aren't public).
  - ROUTES, crude compatibility ranges, NATIONAL_DEMAND_BBL_PER_DAY ->
    illustrative modeling choices (national demand is grounded in a real
    published estimate, see below, but shipping "routes" here are simplified
    corridors for the demo, not an official named list).

Everything downstream (agents/, orchestrator.py) only depends on the
dataclass shapes in models.py, so swapping any of the still-synthetic pieces
for a real integration later doesn't require touching agent logic.
"""
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from models import NewsSignal, AISSignal, MarketSignal, CrudeGrade, Supplier, Refinery, SPRSite

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
# Capacities are real published nameplate figures (converted to bbl/day
# where the source quotes MMTPA, at ~20,080 bbl/day per MMTPA):
#   Reliance Jamnagar: 1.24M bpd combined (RIL; Wikipedia)
#   Nayara Vadinar:     20 MMTPA / ~405,000 bpd (Nayara Energy)
#   BPCL Kochi:         15.5 MMTPA / ~311,000 bpd (BPCL)
#   HPCL Mumbai:        9.5 MMTPA / ~190,000 bpd (HPCL; mopng.gov.in)
#   IOC Paradip:        15.0 MMTPA / ~300,000 bpd (IndianOil)
# Crude-compatibility ranges and current_suppliers are illustrative.

REFINERIES = [
    Refinery(
        "Reliance Jamnagar", "Gujarat", 1_240_000, "Hormuz-Gujarat",
        compatible_api_range=(28.0, 42.0), compatible_sulfur_max=3.0,
        current_suppliers=["Saudi Aramco"],
    ),
    Refinery(
        "Nayara Vadinar", "Gujarat", 405_000, "Hormuz-Gujarat",
        compatible_api_range=(30.0, 36.0), compatible_sulfur_max=2.0,
        current_suppliers=["Saudi Aramco"],
    ),
    Refinery(
        "BPCL Kochi", "Kerala", 311_000, "Hormuz-Kochi",
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

# India's crude demand is real-world grounded: ~5.4 MMbbl/day actual
# consumption in 2026, with OPEC projecting ~6.0 MMbbl/day; 5.8M is the
# commonly cited current estimate (Business Standard / Kotak Neo).
NATIONAL_DEMAND_BBL_PER_DAY = 5_800_000

# --- Strategic Petroleum Reserve sites --------------------------------------
# Capacities are ISPRL's real published storage figures (Vizag 1.33 MMT,
# Mangalore 1.5 MMT, Padur 2.5 MMT -> ~5.33 MMT / ~36.9 MMbbl total,
# ~9.5 days of national demand). current_fill_mmbbl is NOT public (ISPRL
# doesn't publish live reserve levels) so it stays a plausible illustrative
# assumption — see module docstring.

SPR_SITES = [
    SPRSite("Vizag", "Andhra Pradesh", capacity_mmbbl=9.75, current_fill_mmbbl=9.0,
            max_draw_rate_bbl_per_day=120_000, serves_refineries=["HPCL Mumbai", "IOC Paradip"]),
    SPRSite("Mangalore", "Karnataka", capacity_mmbbl=11.0, current_fill_mmbbl=10.0,
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


STRAITS_LIVE_API = "https://straits.live/api/v1"


def _fetch_straits_live(path: str, timeout: float) -> dict:
    req = urllib.request.Request(f"{STRAITS_LIVE_API}{path}", headers={"User-Agent": "hormuz-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def generate_ais_signals(seed: int = None, timeout: float = 10.0) -> list:
    """Live tanker/congestion data from straits.live's free public API — real
    AIS-derived vessel counts for the Strait of Hormuz corridor, real
    per-port congestion at Bandar Abbas (the Iran-side port right at the
    strait). No free API distinguishes traffic per shipping *lane* (only per
    corridor/port), so the same real corridor-wide numbers are applied to
    all three Hormuz-* routes — consistent with the pipeline's own
    chokepoint modeling, where a Hormuz disruption doesn't discriminate
    between lanes through it. Non-Hormuz routes (CapeRoute/RedSea) have no
    coverage from this source and stay synthetic. tanker_count_normal and
    avg_transit_delay_hours have no free historical-baseline endpoint either,
    so those stay illustrative even when tanker_count_current is real.
    Falls back to fully synthetic on any fetch failure.
    """
    rng = random.Random(seed)
    try:
        vessels = _fetch_straits_live("/vessels", timeout)
        ports = _fetch_straits_live("/ports", timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as exc:
        print(f"[hormuz_pipeline] straits.live fetch failed ({exc}); falling back to synthetic AIS signals.",
              file=sys.stderr)
        return _synthetic_ais_signals(seed)

    real_tanker_current = vessels.get("byType", {}).get("tanker")
    bandar_abbas = next((p for p in ports.get("ports", []) if p.get("id") == "bandar-abbas"), None)
    real_congestion = (bandar_abbas["congestion"] / bandar_abbas["total"]) if bandar_abbas and bandar_abbas.get("total") else None

    if real_tanker_current is None or real_congestion is None:
        print("[hormuz_pipeline] straits.live response missing expected fields; falling back to synthetic AIS signals.",
              file=sys.stderr)
        return _synthetic_ais_signals(seed)

    signals = []
    for route in ROUTES:
        is_hormuz = route.startswith("Hormuz")
        if is_hormuz:
            normal = 140  # illustrative pre-tension baseline; no free historical-baseline endpoint
            current = real_tanker_current
            congestion = real_congestion
            delay = 20 + congestion * 40  # illustrative proxy: scales with the real congestion ratio
        else:
            normal = rng.randint(15, 25)
            current = int(normal * (1 - rng.uniform(0.0, 0.05)))
            delay = rng.uniform(0, 5)
            congestion = rng.uniform(0.1, 0.3)
        signals.append(AISSignal(
            route=route,
            tanker_count_normal=normal,
            tanker_count_current=current,
            avg_transit_delay_hours=delay,
            port_congestion_index=congestion,
        ))
    return signals


def _synthetic_ais_signals(seed: int = None) -> list:
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


YAHOO_CHART_API = "https://query1.finance.yahoo.com/v8/finance/chart"


def _fetch_yahoo_quote(ticker: str, timeout: float = 10.0) -> dict:
    url = f"{YAHOO_CHART_API}/{ticker}?interval=1d&range=1mo"
    req = urllib.request.Request(url, headers={"User-Agent": "hormuz-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    result = data["chart"]["result"][0]
    price = result["meta"]["regularMarketPrice"]
    closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
    week_ago = closes[-6] if len(closes) >= 6 else closes[0]
    change_pct_7d = (price - week_ago) / week_ago * 100
    return {"price": price, "change_pct_7d": change_pct_7d}


def generate_market_signals(seed: int = None, timeout: float = 10.0) -> list:
    """Live Brent/WTI prices from Yahoo Finance's public (keyless, unofficial)
    quote endpoint — real price levels and real trailing 7-day % change, not
    synthetic. freight_rate_index has no equivalent free/keyless real-time
    source (real tanker freight indices are paid data), so it stays an
    illustrative placeholder alongside the two real fields. Falls back to
    fully synthetic values on any fetch failure so the pipeline still runs.
    """
    rng = random.Random(seed)
    try:
        brent = _fetch_yahoo_quote("BZ=F", timeout)
        wti = _fetch_yahoo_quote("CL=F", timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as exc:
        print(f"[hormuz_pipeline] Yahoo Finance fetch failed ({exc}); falling back to synthetic market signals.",
              file=sys.stderr)
        return [
            MarketSignal("Brent", price_usd_bbl=rng.uniform(85, 105), price_change_pct_7d=rng.uniform(5, 18),
                         freight_rate_index=rng.uniform(1.2, 1.8)),
            MarketSignal("WTI", price_usd_bbl=rng.uniform(80, 100), price_change_pct_7d=rng.uniform(5, 18),
                         freight_rate_index=rng.uniform(1.3, 2.0)),
        ]

    return [
        MarketSignal("Brent", price_usd_bbl=round(brent["price"], 2), price_change_pct_7d=round(brent["change_pct_7d"], 2),
                     freight_rate_index=rng.uniform(1.2, 1.8)),
        MarketSignal("WTI", price_usd_bbl=round(wti["price"], 2), price_change_pct_7d=round(wti["change_pct_7d"], 2),
                     freight_rate_index=rng.uniform(1.3, 2.0)),
    ]
