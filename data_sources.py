"""
Live feeds (news, AIS, market prices) plus reference data describing India's
refinery / crude supply network. NOTHING in this module is random — every
number is one of three tiers, tagged on each signal via `data_quality`:

  "live"     -> fetched from a real source during this run
  "cached"   -> last-known-good payload from a previous successful fetch,
                replayed from .cache/*.json when the live fetch fails
  "baseline" -> a fixed, real published reference figure (cited inline),
                used only when there is no live source and no cache

Provenance, feed by feed:

  - generate_news_signals()   -> GDELT DOC 2.0 API (live) -> cache ->
    BASELINE_NEWS_SIGNALS (real historical Hormuz/Red Sea events, dated).
  - generate_ais_signals()    -> straits.live free public API (live: real
    AIS-derived tanker counts in the Hormuz watch box + real Bandar Abbas
    port congestion) -> cache -> HORMUZ_AIS_BASELINE (actual observed
    values, observation date cited). tanker_count_normal self-calibrates
    from the observed history in .cache/ais_history.json (median), falling
    back to a cited constant until enough history accumulates. Non-Hormuz
    routes (Cape/Red Sea) have no free per-vessel source (straits.live's
    byRegion.cape is explicitly 0 — "per-vessel Cape tracking is not run"),
    so they use NON_HORMUZ_ROUTE_BASELINES: static figures derived from
    published route volumes, always tagged "baseline".
  - generate_market_signals() -> Yahoo Finance chart API (live Brent/WTI)
    -> EIA API v2 spot series (if EIA_API_KEY is set; free key from
    https://www.eia.gov/opendata/) -> cache -> MARKET_BASELINE.
    freight_rate_index is a live proxy computed from the trailing 7-day
    move of listed crude-tanker owners (FRO/STNG/TNK via Yahoo) — real
    tanker freight indices (BDTI) are paid data with no free API.
  - REFINERIES / SPR_SITES capacities -> real published figures (sourced
    below; refreshed by hand — capacities change every few years, not live).
  - SPR_SITES current_fill_mmbbl -> last publicly reported figures (India
    does not publish live reserve levels; sources cited below).
  - SUPPLIERS -> real trading partners with volumes grounded in India's
    published FY2024-25 import mix (PPAC monthly data) and real voyage
    times; headroom figures are documented derivations from reported spare
    capacity, not contract terms (those aren't public).

Everything downstream (agents/, orchestrator.py) only depends on the
dataclass shapes in models.py, so upgrading any tier (e.g. paid AIS for the
Cape route) only requires touching this module.
"""
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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

# ---------------------------------------------------------------------------
# Last-known-good cache (.cache/*.json) + .env loading
# ---------------------------------------------------------------------------

CACHE_DIR = Path(__file__).with_name(".cache")


def _cache_put(name: str, payload: dict):
    """Persist a successful raw fetch so later failed fetches can replay it."""
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        (CACHE_DIR / f"{name}.json").write_text(
            json.dumps({"fetched_at": time.time(), "data": payload}))
    except OSError as exc:
        print(f"[hormuz_pipeline] cache write failed for {name} ({exc}); continuing without cache.",
              file=sys.stderr)


def _cache_get(name: str):
    """Return {"fetched_at": ts, "data": payload} or None."""
    try:
        return json.loads((CACHE_DIR / f"{name}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _cache_age_str(fetched_at: float) -> str:
    minutes = max(0, (time.time() - fetched_at) / 60)
    return f"{minutes / 60:.1f}h" if minutes >= 90 else f"{minutes:.0f}min"


def load_env_file(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


_DOTENV = load_env_file(Path(__file__).with_name(".env"))
EIA_API_KEY = os.environ.get("EIA_API_KEY") or _DOTENV.get("EIA_API_KEY")

# --- Crude grades -----------------------------------------------------------
# Real assays (typical published API gravity / sulfur wt%):

ARAB_LIGHT = CrudeGrade("Arab Light", api_gravity=33.0, sulfur_pct=1.8)
UPPER_ZAKUM = CrudeGrade("Upper Zakum", api_gravity=34.0, sulfur_pct=1.7)
BASRA_MEDIUM = CrudeGrade("Basrah Medium", api_gravity=27.9, sulfur_pct=3.0)   # SOMO assay (post-2021 grade split)
URALS = CrudeGrade("Urals", api_gravity=31.0, sulfur_pct=1.5)
WTI = CrudeGrade("WTI Midland", api_gravity=40.0, sulfur_pct=0.4)
BONNY_LIGHT = CrudeGrade("Bonny Light", api_gravity=35.0, sulfur_pct=0.2)

# --- Suppliers (route + lead time determine substitutability) --------------
# Volumes grounded in India's real import mix, FY2024-25 (PPAC monthly data /
# Kpler tanker tracking, widely reported): total imports ~4.8-5.0 Mbbl/d, of
# which Russia ~36-38%, Iraq ~18-20%, Saudi ~13-15%, UAE ~8-9%, US ~4-5%,
# Nigeria ~2%. max_additional_bbl_per_day is procurement HEADROOM above
# current deliveries, derived as documented below (reported spare capacity /
# export flexibility — not contract terms, which aren't public). Lead times
# are real typical laden voyage days to Indian west/east-coast ports.

SUPPLIERS = [
    # Saudi: ~650k b/d current to India; OPEC's largest spare capacity holder
    # (~3 Mbbl/d, IEA OMR 2025) — 500k b/d additional to one buyer is realistic.
    # Ras Tanura -> Gujarat: ~3-4 days laden.
    Supplier("Saudi Aramco", "Saudi Arabia", "Hormuz-Gujarat", ARAB_LIGHT, 500_000, 4, True),
    # UAE: ~420k b/d current to India; ADNOC spare ~0.6-1.0 Mbbl/d (IEA).
    # Note: the 1.5 Mbbl/d ADCOP pipeline to Fujairah can bypass the strait,
    # but Upper Zakum loads inside the Gulf -> modeled Hormuz-dependent.
    # Zirku/Fujairah -> Kochi: ~4 days laden.
    Supplier("ADNOC", "UAE", "Hormuz-Kochi", UPPER_ZAKUM, 300_000, 4, True),
    # Iraq: ~900k b/d current to India (India's #2 supplier); Basrah export
    # capacity constrained -> modest headroom. Basrah -> Mumbai: ~5 days.
    Supplier("Iraq SOMO", "Iraq", "Hormuz-Mumbai", BASRA_MEDIUM, 250_000, 5, True),
    # Russia: ~1.75 Mbbl/d current to India (largest supplier since 2022);
    # exports already near capacity -> headroom limited. Baltic/Black Sea ->
    # India via Suez ~26d, via Cape ~35d; modeled on the Cape corridor.
    Supplier("Rosneft", "Russia", "CapeRoute-EastCoast", URALS, 300_000, 30, False),
    # US: ~200k b/d current to India; US Gulf exports flex with arbitrage.
    # USGC -> India via Cape of Good Hope: ~38-42 days.
    Supplier("US Gulf Exporters", "USA", "CapeRoute-EastCoast", WTI, 250_000, 40, False),
    # Nigeria: ~100k b/d current; spot-market flexible light sweet.
    # West Africa -> West Coast India: ~18-20 days.
    Supplier("NNPC", "Nigeria", "RedSea-WestCoast", BONNY_LIGHT, 150_000, 19, False),
]

# --- Refineries --------------------------------------------------------------
# Capacities are real published nameplate figures (converted to bbl/day
# where the source quotes MMTPA, at ~20,080 bbl/day per MMTPA):
#   Reliance Jamnagar: 1.24M bpd combined (RIL; Wikipedia)
#   Nayara Vadinar:     20 MMTPA / ~405,000 bpd (Nayara Energy)
#   BPCL Kochi:         15.5 MMTPA / ~311,000 bpd (BPCL)
#   HPCL Mumbai:        9.5 MMTPA / ~190,000 bpd (HPCL; mopng.gov.in)
#   IOC Paradip:        15.0 MMTPA / ~300,000 bpd (IndianOil)
# Compatibility ranges reflect each plant's real crude-slate profile
# (complexity / Nelson index, reported diet); current_suppliers reflect
# reported term/spot sourcing patterns (Reuters/Kpler trade reporting),
# constrained to the suppliers modeled above.

REFINERIES = [
    # Highest-complexity refinery in the world (Nelson ~21.1, RIL): built to
    # run heavy sour discounted crudes; major Urals buyer since 2022 alongside
    # Middle East term barrels.
    Refinery(
        "Reliance Jamnagar", "Gujarat", 1_240_000, "Hormuz-Gujarat",
        compatible_api_range=(22.0, 45.0), compatible_sulfur_max=4.5,
        current_suppliers=["Saudi Aramco", "Rosneft"],
    ),
    # Rosneft-invested (49.13%); runs a predominantly Russian slate since
    # 2022 (reported >70% Urals share), plus Gulf grades.
    Refinery(
        "Nayara Vadinar", "Gujarat", 405_000, "Hormuz-Gujarat",
        compatible_api_range=(26.0, 38.0), compatible_sulfur_max=3.0,
        current_suppliers=["Rosneft", "Saudi Aramco"],
    ),
    # Post-IREP (2017 Integrated Refinery Expansion Project) Kochi is a
    # high-complexity plant designed for high-sulfur, cheaper crudes (BPCL).
    Refinery(
        "BPCL Kochi", "Kerala", 311_000, "Hormuz-Kochi",
        compatible_api_range=(24.0, 40.0), compatible_sulfur_max=3.5,
        current_suppliers=["ADNOC"],
    ),
    # Mid-complexity coastal plant; long-standing West Asian sour diet via
    # term contracts (HPCL annual reports).
    Refinery(
        "HPCL Mumbai", "Maharashtra", 190_000, "Hormuz-Mumbai",
        compatible_api_range=(26.0, 40.0), compatible_sulfur_max=3.0,
        current_suppliers=["Iraq SOMO"],
    ),
    # IOC's most complex refinery (Nelson ~12.2), designed for 100% high-
    # sulfur crude; large Russian + Iraqi share in reported slate.
    Refinery(
        "IOC Paradip", "Odisha", 300_000, "CapeRoute-EastCoast",
        compatible_api_range=(24.0, 40.0), compatible_sulfur_max=3.0,
        current_suppliers=["Rosneft", "Iraq SOMO"],
    ),
]

# India's crude demand is real-world grounded: ~5.4 MMbbl/day actual
# consumption in 2026, with OPEC projecting ~6.0 MMbbl/day; 5.8M is the
# commonly cited current estimate (Business Standard / Kotak Neo).
NATIONAL_DEMAND_BBL_PER_DAY = 5_800_000

# --- Strategic Petroleum Reserve sites --------------------------------------
# Capacities are ISPRL's real published storage figures (Vizag 1.33 MMT,
# Mangalore 1.5 MMT, Padur 2.5 MMT -> ~5.33 MMT / ~36.9 MMbbl total,
# ~9.5 days of national demand). current_fill_mmbbl uses the last publicly
# REPORTED state (India doesn't publish live levels): all three caverns were
# filled to capacity during the April-May 2020 price crash (PIB release,
# ~Rs 5,000 crore saving reported to Parliament); ADNOC holds ~5.86 MMbbl
# of the Mangalore cavern under a 2018 storage-and-first-refusal agreement
# (counted as available here since India holds first right to the oil in an
# emergency). Small drawdowns/re-fills since then aren't published, so fills
# are carried at ~95% of capacity, tagged as reported-baseline figures.
# max_draw_rate_bbl_per_day: ISPRL doesn't publish evacuation rates —
# engineering estimates from cavern pump/pipeline sizing, flagged as such.

SPR_SITES = [
    SPRSite("Vizag", "Andhra Pradesh", capacity_mmbbl=9.75, current_fill_mmbbl=9.3,
            max_draw_rate_bbl_per_day=120_000, serves_refineries=["HPCL Mumbai", "IOC Paradip"]),
    SPRSite("Mangalore", "Karnataka", capacity_mmbbl=11.0, current_fill_mmbbl=10.5,
            max_draw_rate_bbl_per_day=150_000, serves_refineries=["BPCL Kochi", "Reliance Jamnagar"]),
    SPRSite("Padur", "Karnataka", capacity_mmbbl=18.3, current_fill_mmbbl=17.4,
            max_draw_rate_bbl_per_day=180_000, serves_refineries=["Reliance Jamnagar", "Nayara Vadinar"]),
]


# ---------------------------------------------------------------------------
# Live news feed (GDELT Project) -> cache -> real historical baselines
# ---------------------------------------------------------------------------

def _gdelt_get(params: dict, timeout: float) -> dict:
    url = f"{GDELT_DOC_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "hormuz-pipeline/1.0",
                                               "Accept": "*/*"})
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


# Real, dated historical events (headline paraphrases of actual coverage) —
# used only when GDELT is unreachable AND no cached fetch exists. Sentiments
# are fixed editorial scores of each real event, not random.
BASELINE_NEWS_SIGNALS = [
    ("Iran seizes MSC Aries container ship near Strait of Hormuz (Apr 2024)", "Reuters", -0.8),
    ("Houthi attacks cut Red Sea/Suez transits by ~60% vs 2023 (2024)", "EIA", -0.7),
    ("Israel-Iran June 2025 strikes spark Hormuz closure fears; tankers U-turn", "Bloomberg", -0.8),
    ("Hormuz war-risk insurance premiums spike amid Gulf escalation (2024-25)", "Lloyd's List", -0.6),
    ("Gulf de-escalation talks resume; shipping advisories eased", "AFP", 0.4),
]


def _news_signals_from_raw(raw: dict, quality: str, region: str) -> list:
    sentiment = max(-1.0, min(1.0, raw["avg_tone"] / 10.0))
    signals = []
    for i, art in enumerate(raw["articles"]):
        relevance = round(max(0.5, 1.0 - i * 0.05), 2)  # rank decay; GDELT sorted by hybridrel
        signals.append(NewsSignal(
            headline=art.get("title") or "(untitled)",
            source=art.get("domain", "unknown"),
            sentiment=round(sentiment, 3),
            relevance=relevance,
            region=region,
            data_quality=quality,
        ))
    return signals


def _news_fallback(region: str) -> list:
    cached = _cache_get("gdelt_news")
    if cached:
        print(f"[hormuz_pipeline] using cached GDELT news from {_cache_age_str(cached['fetched_at'])} ago.",
              file=sys.stderr)
        return _news_signals_from_raw(cached["data"], "cached", region)
    print("[hormuz_pipeline] no news cache; using real historical baseline events.", file=sys.stderr)
    return [
        NewsSignal(headline=h, source=s, sentiment=sent, relevance=0.9,
                   region=region, data_quality="baseline")
        for h, s, sent in BASELINE_NEWS_SIGNALS
    ]


def generate_news_signals(seed: int = None, region: str = "Strait of Hormuz",
                           query: str = None, maxrecords: int = 8, timeout: float = 15.0) -> list:
    """Live news signals from the GDELT Project (api.gdeltproject.org) — real
    articles matching `query`, scored with GDELT's real aggregate tone for
    that query. On failure: replay the last successful fetch from .cache/
    (data_quality="cached"), else real historical baseline events
    (data_quality="baseline"). Never random.

    `seed` is accepted for backward compatibility but has no effect — there
    is no synthetic path left to seed.

    GDELT rate-limits anonymous callers to ~1 request/5s, so the two calls
    this makes (article list, tone histogram) are spaced out accordingly.
    """
    q = query or GDELT_DEFAULT_QUERY
    try:
        articles = _fetch_gdelt_articles(q, maxrecords, timeout)
        time.sleep(10)  # GDELT throttles anonymous callers hard; ~1 req/5s documented, 10s is reliably clear
        avg_tone = _fetch_gdelt_tone(q, timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        print(f"[hormuz_pipeline] GDELT fetch failed ({exc}).", file=sys.stderr)
        return _news_fallback(region)

    if not articles:
        print("[hormuz_pipeline] GDELT returned no articles for this query.", file=sys.stderr)
        return _news_fallback(region)

    raw = {"articles": articles, "avg_tone": avg_tone}
    _cache_put("gdelt_news", raw)
    return _news_signals_from_raw(raw, "live", region)


# ---------------------------------------------------------------------------
# Live AIS (straits.live) -> cache -> observed baselines
# ---------------------------------------------------------------------------

STRAITS_LIVE_API = "https://straits.live/api/v1"

# Fallback Hormuz figures when straits.live is unreachable and no cache
# exists: actual values observed from the same API (checked 2026-07-20:
# byType.tanker=124, Bandar Abbas congestion 5/6≈0.83; typical observed
# tanker range 110-140, consistent with EIA's ~20-21 Mbbl/d Hormuz flow).
HORMUZ_AIS_BASELINE = {"tanker_count": 124, "congestion": 0.83}

# Baseline used for tanker_count_normal until enough observed history
# accumulates in .cache/ais_history.json (see _hormuz_normal_baseline).
HORMUZ_TANKER_NORMAL_BASELINE = 130

# No free per-vessel source covers these corridors (straits.live's
# byRegion.cape is explicitly 0 — "per-vessel Cape tracking is not run"),
# so they carry static figures derived from published route volumes,
# always tagged data_quality="baseline":
#   CapeRoute: Russia->India ~1.6-1.8 Mbbl/d (Kpler/Reuters 2024-25) plus
#     US/Atlantic-basin cargoes ≈ ~25 Suezmax/VLCC-equivalents in transit
#     on the ~30-day leg at any time; no chokepoint congestion.
#   RedSea: Suez/Red Sea transits down ~60% vs 2023 since the Houthi
#     attacks began (EIA, 2024) — modeled as 12 normal -> 8 current with
#     elevated delay from convoy/security routing.
NON_HORMUZ_ROUTE_BASELINES = {
    "CapeRoute-EastCoast": {"normal": 25, "current": 25, "delay_hours": 0.0, "congestion": 0.15},
    "RedSea-WestCoast":    {"normal": 12, "current": 8,  "delay_hours": 6.0, "congestion": 0.25},
}

_AIS_HISTORY_MIN_SAMPLES = 10
_AIS_HISTORY_MIN_SPAN_S = 3 * 86400
_AIS_HISTORY_MAX_AGE_S = 30 * 86400


def _record_ais_history(tanker_count: int):
    """Append this observation to .cache/ais_history.json (pruned to 30 days)
    so tanker_count_normal can self-calibrate from real observed history."""
    cached = _cache_get("ais_history")
    history = cached["data"].get("observations", []) if cached else []
    now = time.time()
    history = [o for o in history if now - o["ts"] < _AIS_HISTORY_MAX_AGE_S]
    history.append({"ts": now, "tanker_count": tanker_count})
    _cache_put("ais_history", {"observations": history})


def _hormuz_normal_baseline() -> int:
    """Median of ≥10 real observations spanning ≥3 days, else the cited
    constant. This replaces the old hardcoded 140 with a baseline that
    converges on the strait's actual observed normal."""
    cached = _cache_get("ais_history")
    if cached:
        obs = cached["data"].get("observations", [])
        if (len(obs) >= _AIS_HISTORY_MIN_SAMPLES
                and obs[-1]["ts"] - obs[0]["ts"] >= _AIS_HISTORY_MIN_SPAN_S):
            return round(statistics.median(o["tanker_count"] for o in obs))
    return HORMUZ_TANKER_NORMAL_BASELINE


def _fetch_straits_live(path: str, timeout: float) -> dict:
    req = urllib.request.Request(f"{STRAITS_LIVE_API}{path}", headers={"User-Agent": "hormuz-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _ais_signals_from_raw(raw: dict, quality: str) -> list:
    """Build all route signals from a raw Hormuz observation. The same real
    corridor-wide numbers apply to all three Hormuz-* routes — no free API
    distinguishes traffic per shipping lane, and the pipeline's chokepoint
    modeling treats a Hormuz disruption as hitting every lane through it.
    Non-Hormuz routes always come from published baselines (see above)."""
    normal = _hormuz_normal_baseline()
    signals = []
    for route in ROUTES:
        if route.startswith("Hormuz"):
            congestion = raw["congestion"]
            signals.append(AISSignal(
                route=route,
                tanker_count_normal=normal,
                tanker_count_current=raw["tanker_count"],
                # calibrated proxy over the real congestion ratio (no free
                # measured-transit-delay source exists): 20h base + up to
                # 40h as Bandar Abbas congestion saturates
                avg_transit_delay_hours=20 + congestion * 40,
                port_congestion_index=congestion,
                data_quality=quality,
            ))
        else:
            b = NON_HORMUZ_ROUTE_BASELINES[route]
            signals.append(AISSignal(
                route=route,
                tanker_count_normal=b["normal"],
                tanker_count_current=b["current"],
                avg_transit_delay_hours=b["delay_hours"],
                port_congestion_index=b["congestion"],
                data_quality="baseline",
            ))
    return signals


def _ais_fallback() -> list:
    cached = _cache_get("straits_live")
    if cached:
        print(f"[hormuz_pipeline] using cached straits.live AIS from {_cache_age_str(cached['fetched_at'])} ago.",
              file=sys.stderr)
        return _ais_signals_from_raw(cached["data"], "cached")
    print("[hormuz_pipeline] no AIS cache; using observed baseline figures (as of 2026-07-20).",
          file=sys.stderr)
    return _ais_signals_from_raw(HORMUZ_AIS_BASELINE, "baseline")


def generate_ais_signals(seed: int = None, timeout: float = 10.0) -> list:
    """Live tanker/congestion data from straits.live's free public API — real
    AIS-derived vessel counts for the Strait of Hormuz corridor, real
    per-port congestion at Bandar Abbas (the Iran-side port right at the
    strait). On failure: replay the last successful fetch from .cache/
    (data_quality="cached"), else cited observed baselines ("baseline").
    Never random. `seed` is accepted for backward compatibility but has no
    effect."""
    try:
        vessels = _fetch_straits_live("/vessels", timeout)
        ports = _fetch_straits_live("/ports", timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as exc:
        print(f"[hormuz_pipeline] straits.live fetch failed ({exc}).", file=sys.stderr)
        return _ais_fallback()

    real_tanker_current = vessels.get("byType", {}).get("tanker")
    bandar_abbas = next((p for p in ports.get("ports", []) if p.get("id") == "bandar-abbas"), None)
    real_congestion = (bandar_abbas["congestion"] / bandar_abbas["total"]) if bandar_abbas and bandar_abbas.get("total") else None

    if real_tanker_current is None or real_congestion is None:
        print("[hormuz_pipeline] straits.live response missing expected fields.", file=sys.stderr)
        return _ais_fallback()

    raw = {"tanker_count": real_tanker_current, "congestion": real_congestion}
    _cache_put("straits_live", raw)
    _record_ais_history(real_tanker_current)
    return _ais_signals_from_raw(raw, "live")


# ---------------------------------------------------------------------------
# Live market prices (Yahoo -> EIA -> cache -> baseline) + freight proxy
# ---------------------------------------------------------------------------

YAHOO_CHART_API = "https://query1.finance.yahoo.com/v8/finance/chart"
EIA_SPOT_API = "https://api.eia.gov/v2/petroleum/pri/spt/data/"

# Fallback price levels when every live source and the cache are unavailable:
# actual closing levels observed 2026-07-20 (Yahoo BZ=F / CL=F).
MARKET_BASELINE = {
    "brent": {"price": 88.41, "change_pct_7d": 0.0},
    "wti": {"price": 81.74, "change_pct_7d": 0.0},
}

# Freight baseline: 1.0 = freight at its trailing normal (BDTI near its
# recent average, ~1000-1100 per Baltic Exchange prints through 2025-26).
FREIGHT_RATE_BASELINE = 1.0

# Listed crude-tanker owners used as the live freight proxy (all verified
# resolving on Yahoo): Frontline, Scorpio Tankers, Teekay Tankers.
FREIGHT_PROXY_TICKERS = ["FRO", "STNG", "TNK"]
_FREIGHT_PROXY_SENSITIVITY = 5.0  # index points per 100% avg equity move (calibration)

_QUALITY_RANK = {"live": 0, "cached": 1, "baseline": 2}


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


def _fetch_eia_spot(series: str, timeout: float = 10.0) -> dict:
    """Daily spot price from the EIA API v2 (free key). RBRTE = Brent,
    RWTC = WTI Cushing. 7-day change computed from the returned series."""
    params = urllib.parse.urlencode({
        "api_key": EIA_API_KEY,
        "frequency": "daily",
        "data[0]": "value",
        "facets[series][]": series,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 8,
    })
    req = urllib.request.Request(f"{EIA_SPOT_API}?{params}",
                                 headers={"User-Agent": "hormuz-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        rows = json.loads(resp.read())["response"]["data"]
    prices = [float(r["value"]) for r in rows if r.get("value") is not None]
    price, week_ago = prices[0], prices[min(5, len(prices) - 1)]
    return {"price": price, "change_pct_7d": (price - week_ago) / week_ago * 100}


def _fetch_freight_proxy(timeout: float) -> float:
    """Live freight-rate index proxy: average trailing 7-day % move of listed
    crude-tanker owners, mapped around 1.0 (= freight normal). A real tanker
    freight index (Baltic BDTI) is paid-only; tanker-owner equities are the
    closest free real-time signal of spot tanker rates."""
    changes = [_fetch_yahoo_quote(t, timeout)["change_pct_7d"] for t in FREIGHT_PROXY_TICKERS]
    avg_change = sum(changes) / len(changes)
    index = 1.0 + (avg_change / 100.0) * _FREIGHT_PROXY_SENSITIVITY
    return max(0.8, min(2.5, index))


def _market_signals_from_raw(raw: dict, quality: str) -> list:
    # each signal reports the worst tier among its components (prices vs freight)
    freight_quality = raw.get("freight_quality", quality)
    worst = max(quality, freight_quality, key=lambda q: _QUALITY_RANK[q])
    freight = round(raw["freight_index"], 3)
    return [
        MarketSignal("Brent", price_usd_bbl=round(raw["brent"]["price"], 2),
                     price_change_pct_7d=round(raw["brent"]["change_pct_7d"], 2),
                     freight_rate_index=freight, data_quality=worst),
        MarketSignal("WTI", price_usd_bbl=round(raw["wti"]["price"], 2),
                     price_change_pct_7d=round(raw["wti"]["change_pct_7d"], 2),
                     freight_rate_index=freight, data_quality=worst),
    ]


def _market_fallback() -> list:
    cached = _cache_get("market")
    if cached:
        print(f"[hormuz_pipeline] using cached market data from {_cache_age_str(cached['fetched_at'])} ago.",
              file=sys.stderr)
        return _market_signals_from_raw(cached["data"], "cached")
    print("[hormuz_pipeline] no market cache; using baseline price levels (as of 2026-07-20).",
          file=sys.stderr)
    raw = {"brent": MARKET_BASELINE["brent"], "wti": MARKET_BASELINE["wti"],
           "freight_index": FREIGHT_RATE_BASELINE, "freight_quality": "baseline"}
    return _market_signals_from_raw(raw, "baseline")


def generate_market_signals(seed: int = None, timeout: float = 10.0) -> list:
    """Live Brent/WTI prices — Yahoo Finance's public quote endpoint first,
    then the EIA API v2 daily spot series if an EIA_API_KEY is configured,
    then the last-known-good cache, then cited baseline levels. The
    freight_rate_index is a live proxy from crude-tanker-owner equities
    (see _fetch_freight_proxy); if only the freight leg fails, prices stay
    "live" and the signal's data_quality reports the worst component.
    Never random. `seed` is accepted for backward compatibility but has no
    effect."""
    brent = wti = None
    try:
        brent = _fetch_yahoo_quote("BZ=F", timeout)
        wti = _fetch_yahoo_quote("CL=F", timeout)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as exc:
        print(f"[hormuz_pipeline] Yahoo Finance fetch failed ({exc}).", file=sys.stderr)
        if EIA_API_KEY:
            try:
                brent = _fetch_eia_spot("RBRTE", timeout)
                wti = _fetch_eia_spot("RWTC", timeout)
                print("[hormuz_pipeline] using EIA spot series instead.", file=sys.stderr)
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as eia_exc:
                print(f"[hormuz_pipeline] EIA fetch failed too ({eia_exc}).", file=sys.stderr)

    if brent is None or wti is None:
        return _market_fallback()

    try:
        freight_index, freight_quality = _fetch_freight_proxy(timeout), "live"
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
        print(f"[hormuz_pipeline] freight proxy fetch failed ({exc}); "
              "using cached/baseline freight index.", file=sys.stderr)
        cached = _cache_get("market")
        if cached:
            freight_index, freight_quality = cached["data"]["freight_index"], "cached"
        else:
            freight_index, freight_quality = FREIGHT_RATE_BASELINE, "baseline"

    raw = {"brent": brent, "wti": wti,
           "freight_index": freight_index, "freight_quality": freight_quality}
    _cache_put("market", raw)
    return _market_signals_from_raw(raw, "live")
