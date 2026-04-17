"""
Coverage configuration — single source of truth for the daily news pipeline.

Maps coverage folder keys to Discord channels, market close windows,
and Brave Search queries.
"""

from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_ROOT = WORKSPACE_ROOT / "coverage"
INBOX_DIR = Path("/mnt/c/Users/natba/OneDrive/@ Cowork/investing/inbox")
OFFICIAL_DOC_CORPUS_DIR = Path("/mnt/c/Users/natba/OneDrive/@ Cowork/openclaw-investing-context")
OFFICIAL_DOC_CORPUS_WINDOWS_DIR = r"C:\Users\natba\OneDrive\@ Cowork\openclaw-investing-context"

# Inbox handoff uses portfolio-facing primary tickers where a coverage item is
# represented by an ETF or sector alias rather than a single company ticker.
INBOX_PRIMARY_TICKER_OVERRIDES: dict[str, str] = {
    "sectors/gold-miners": "GDX",
    "sectors/uranium-miners": "URA",
    "markets/korea": "EWY",
}

# Coverage files evolved over time. Tickers use the canonical filenames while
# older sector/market folders still use kpis.md / watchlist.md.
COVERAGE_FILE_ALIASES: dict[str, tuple[str, ...]] = {
    "kpi_tree.md": ("kpi_tree.md", "kpis.md"),
    "catalysts.md": ("catalysts.md", "watchlist.md"),
    "thesis.md": ("thesis.md",),
    "debates.md": ("debates.md",),
    "update_log.md": ("update_log.md",),
}


def resolve_coverage_file(coverage_path: Path, logical_name: str) -> Path:
    """
    Resolve a logical coverage filename to the best on-disk match.

    If no alias exists on disk yet, return the canonical target path so callers
    can still create or update the file predictably.
    """
    candidates = COVERAGE_FILE_ALIASES.get(logical_name, (logical_name,))
    for candidate in candidates:
        path = coverage_path / candidate
        if path.exists():
            return path
    return coverage_path / candidates[0]

# Discord server ID
GUILD_ID = "1482651151932461131"

# ── Coverage map ─────────────────────────────────────────────────────────────
# Keys must match subfolders under coverage/
# close: "us" | "asia_japan" | "asia_korea" | "asia_hk"

COVERAGE = {
    # ── Tickers: US close ─────────────────────────────────────────────────────
    "tickers/JPM":  {"channel": "jpm",   "category": "TICKERS", "close": "us"},
    "tickers/TMX":  {"channel": "tmx",   "category": "TICKERS", "close": "us"},
    "tickers/STAN": {"channel": "stan",  "category": "TICKERS", "close": "us"},
    "tickers/GRAB": {"channel": "grab",  "category": "TICKERS", "close": "us"},
    "tickers/SE":   {"channel": "se",    "category": "TICKERS", "close": "us"},
    "tickers/FUTU": {"channel": "futu",  "category": "TICKERS", "close": "us"},
    "tickers/GOOG": {"channel": "goog",  "category": "TICKERS", "close": "us"},
    "tickers/MMYT": {"channel": "mmyt",  "category": "TICKERS", "close": "us"},

    # ── Tickers: Asia Japan close ─────────────────────────────────────────────
    "tickers/MUFG": {"channel": "8316",  "category": "TICKERS", "close": "asia_japan"},
    "tickers/MFG":  {"channel": "8316",  "category": "TICKERS", "close": "asia_japan"},
    "tickers/8316": {"channel": "8316",  "category": "TICKERS", "close": "asia_japan"},

    # ── Tickers: Asia HK close ────────────────────────────────────────────────
    "tickers/HSBC": {"channel": "stan",  "category": "TICKERS", "close": "asia_hk"},
    "tickers/1299": {"channel": "1299",  "category": "TICKERS", "close": "asia_hk"},

    # ── Sectors: US close ─────────────────────────────────────────────────────
    "sectors/exchanges":      {"channel": "exchanges",      "category": "SECTORS", "close": "us"},
    "sectors/gold-miners":    {"channel": "gold-miners",    "category": "SECTORS", "close": "us"},
    "sectors/uranium-miners": {"channel": "uranium-miners", "category": "SECTORS", "close": "us"},

    # ── Sectors: Asia Japan close ─────────────────────────────────────────────
    "sectors/japan-banks": {"channel": "japan-banks", "category": "SECTORS", "close": "asia_japan"},

    # ── Sectors: Asia Korea close ───────────────────────────────────────────────
    "sectors/korea-memory": {"channel": "korea-memory", "category": "SECTORS", "close": "asia_korea"},

    # ── Markets: Asia Korea close ───────────────────────────────────────────────
    "markets/korea": {"channel": "korea", "category": "MARKETS", "close": "asia_korea"},
}

# ── Market close windows (UTC) ────────────────────────────────────────────────
# Asia windows use fixed UTC offsets (no DST).
# The "us" window is DST-aware — resolved dynamically in daily_news.py via zoneinfo.
# These static values are used only as fallback / documentation.

CLOSE_WINDOWS = {
    "asia_japan": {"hour": 6,  "minute": 0},   # 3:00 pm JST (UTC+9)
    "asia_korea": {"hour": 6,  "minute": 30},  # 3:30 pm KST (UTC+9)
    "asia_hk":    {"hour": 8,  "minute": 0},   # 4:00 pm HKT (UTC+8)
    "us":         {"hour": 21, "minute": 0},   # 4:00 pm ET — overridden at runtime for DST
}

# ── Brave Search queries per coverage item ────────────────────────────────────

SEARCH_QUERIES = {
    # Tickers
    "tickers/JPM":  "JPMorgan Chase JPM NII investment banking earnings credit",
    "tickers/TMX":  "TMX Group Toronto Stock Exchange Trayport Montreal Exchange derivatives",
    "tickers/STAN": "Standard Chartered STAN bank Asia China results",
    "tickers/HSBC": "HSBC Holdings Hong Kong wealth NII capital return results",
    "tickers/MUFG": "Mitsubishi UFJ MUFG 8306 megabank BOJ rates Morgan Stanley stake",
    "tickers/MFG":  "Mizuho Financial Group MFG 8411 megabank BOJ rates advisory results",
    "tickers/8316": "Sumitomo Mitsui SMFG 8316 megabank BOJ interest rates",
    "tickers/GRAB": "Grab Holdings GRAB superapp SE Asia ride-hailing GrabFin earnings",
    "tickers/SE":   "Sea Limited SE Shopee Garena SeaMoney e-commerce earnings",
    "tickers/1299": "AIA Group 1299 HKEX life insurance Asia new business value VONB",
    "tickers/FUTU": "Futu Holdings FUTU moomoo brokerage China AUC earnings",
    "tickers/GOOG": "Alphabet Google GOOG search cloud GCP AI Overviews earnings antitrust",
    "tickers/MMYT": "MakeMyTrip MMYT India travel OTA flights hotels earnings",

    # Sectors
    "sectors/exchanges":      "stock exchange operators CME ICE CBOE LSEG TMX volumes",
    "sectors/japan-banks":    "Japan banks megabank BOJ rate hike MUFG SMFG Mizuho NIM",
    "sectors/gold-miners":    "gold price gold miners GDX Newmont Barrick Agnico AISC central bank gold",
    "sectors/uranium-miners": "uranium price nuclear energy URA Cameco Kazatomprom reactor restart AI power",

    # Sectors (Asia)
    "sectors/korea-memory": "Samsung SK Hynix HBM DRAM NAND memory semiconductor Korea AI chip demand capex",

    # Markets
    "markets/korea": "Korea market EWY KOSPI won Samsung SK Hynix Value-Up reform foreign flows",
}

# ── Inbox-only watchlist monitor ─────────────────────────────────────────────
# These names do not get Discord posts. They are scanned for material articles
# and routed to the OneDrive inbox as notes when something looks actionable.

WATCHLIST_ITEMS: dict[str, dict] = {
    "PRU": {
        "ticker": "PRU",
        "name": "Prudential plc",
        "close": "asia_hk",
        "query": "Prudential plc PRU Asia insurer Hong Kong Indonesia new business profit",
        "match_terms": ["prudential", "pru", "jackson", "new business profit", "ape", "embedded value"],
    },
    "HKEX": {
        "ticker": "HKEX",
        "name": "Hong Kong Exchanges and Clearing",
        "close": "asia_hk",
        "query": "HKEX Hong Kong Exchanges and Clearing IPO turnover Stock Connect derivatives",
        "match_terms": ["hkex", "hong kong exchanges", "stock connect", "hong kong exchanges and clearing"],
    },
    "HANG-LUNG": {
        "ticker": "HANG-LUNG",
        "name": "Hang Lung Properties",
        "close": "asia_hk",
        "query": "Hang Lung Properties Hong Kong retail office China luxury mall results",
        "match_terms": ["hang lung", "hang lung properties", "plaza 66"],
    },
    "KYOTOFG": {
        "ticker": "KYOTOFG",
        "name": "Kyoto Financial Group",
        "close": "asia_japan",
        "query": "Kyoto Financial Group regional bank Japan BOJ rates results",
        "match_terms": ["kyoto financial", "kyotofg", "kyoto fg"],
    },
    "RESONA": {
        "ticker": "RESONA",
        "name": "Resona Holdings",
        "close": "asia_japan",
        "query": "Resona Holdings Japan bank BOJ rates capital return results",
        "match_terms": ["resona"],
    },
    "CHIBA": {
        "ticker": "CHIBA",
        "name": "Chiba Bank",
        "close": "asia_japan",
        "query": "Chiba Bank Japan regional bank BOJ rates results",
        "match_terms": ["chiba bank"],
    },
    "DAIWAHOUSE": {
        "ticker": "DAIWAHOUSE",
        "name": "Daiwa House Industry",
        "close": "asia_japan",
        "query": "Daiwa House Industry Japan property developer housing logistics results",
        "match_terms": ["daiwa house", "daiwa house industry"],
    },
    "MEC": {
        "ticker": "MEC",
        "name": "Mitsubishi Estate",
        "close": "asia_japan",
        "query": "Mitsubishi Estate Japan office property Marunouchi JREIT results",
        "match_terms": ["mitsubishi estate", "marunouchi"],
    },
    "MQG": {
        "ticker": "MQG",
        "name": "Macquarie Group",
        "close": "us",
        "query": "Macquarie Group MQG infrastructure asset management commodities results",
        "match_terms": ["macquarie", "mqg", "mam", "commodities and global markets"],
    },
    "GDG": {
        "ticker": "GDG",
        "name": "Generation Development Group",
        "close": "us",
        "query": "Generation Development Group GDG retirement income LifeIncome results managed accounts",
        "match_terms": ["generation development group", "gdg", "lifeincome"],
    },
    "CGF": {
        "ticker": "CGF",
        "name": "Challenger Limited",
        "close": "us",
        "query": "Challenger Limited CGF annuities retirement income Australia results",
        "match_terms": ["challenger", "cgf", "annuities"],
    },
    "ERSTE": {
        "ticker": "ERSTE",
        "name": "Erste Group",
        "close": "us",
        "query": "Erste Group Bank CEE bank Austria results NII capital",
        "match_terms": ["erste group", "erste", "erste bank"],
    },
    "IBKR": {
        "ticker": "IBKR",
        "name": "Interactive Brokers",
        "close": "us",
        "query": "Interactive Brokers IBKR DARTs net interest margin client accounts results",
        "match_terms": ["interactive brokers", "ibkr", "darts", "client accounts", "net interest income"],
    },
}

# ── Preferred news sources ────────────────────────────────────────────────────
# Ordered by priority — earlier entries rank higher in fetch_news.py.

PREFERRED_SOURCES = [
    "bloomberg.com",
    "reuters.com",
    "wsj.com",
    "ft.com",
    "economist.com",
    "nikkei.com",
    "scmp.com",
    "financialtimes.com",  # alternate FT hostname
    "marketwatch.com",
    "cnbc.com",
    "apnews.com",
    "barrons.com",
]

# Trusted sources can pass the credibility gate even if they are not top-tier.
# Keep this list conservative. Unknown domains should not dominate the pipeline.
TRUSTED_SOURCES = PREFERRED_SOURCES + [
    "channelnewsasia.com",
    "businesstimes.com.sg",
    "asia.nikkei.com",
    "theglobeandmail.com",
    "financialpost.com",
    "economictimes.indiatimes.com",
    "businesstoday.in",
    "business-standard.com",
    "thehindubusinessline.com",
    "en.yna.co.kr",
    "en.sedaily.com",
    "koreaherald.com",
    "koreajoongangdaily.joins.com",
    "kedglobal.com",
    "digitimes.com",
    "skift.com",
    "theasianbanker.com",
    "sec.gov",
    "hkexnews.hk",
    "jpx.co.jp",
    "boj.or.jp",
    "federalreserve.gov",
    "ecb.europa.eu",
    "cmegroup.com",
    "theice.com",
    "tmx.com",
]

# ── Blocked sources ───────────────────────────────────────────────────────────
# Low-quality aggregators, copy sites, and opinion mills.
# Articles from these are dropped before any LLM call.

BLOCKED_SOURCES = [
    "simplywall.st",
    "fool.com",           # Motley Fool
    "investopedia.com",   # educational, not news
    "benzinga.com",
    "finbold.com",
    "financemagnates.com",
    "investorplace.com",
    "stockanalysis.com",
    "zacks.com",
    "gurufocus.com",
    "tipranks.com",
    "wallstreetmojo.com",
    "macrotrends.net",
    "seekingalpha.com",   # opinion/research marketplace, not primary news
    "marketbeat.com",     # mostly ownership churn / secondary rewrites
    "dailypolitical.com",
    "watcher.guru",
    "cryptoast.fr",
    "moneylife.in",
    "ad-hoc-news.de",               # German press release aggregator, no editorial filter
    "newkerala.com",                 # low-authority Indian news aggregator
    "markets.financialcontent.com",  # content syndication platform
    "marketscreener.com",            # quote/PR/secondary aggregation
    "parameter.io",                  # unknown-provenance rewrites
    "bestmediainfo.com",            # Indian media industry, not financial news
    "worldecomag.com",              # unverified financial content site
    "analyticsinsight.net",         # tech content farm, republishes/embellishes
    "travelbizmonitor.com",         # low-quality trade aggregator
    "stocktitan.net",               # alert/aggregation wrapper
    "fxstreet.com",                 # market commentary, weak for thesis-linked news
    "heygotrade.com",               # low-authority market content site
    "bitcoinworld.co.in",           # crypto content farm
    "bloomingbit.io",               # crypto-focused Korean site
    "insidentity.com",              # low-authority insurance content site
    "defenseworld.net",             # holdings/position-change spam
    "wral.com",                     # local TV news, weak financial relevance
    "news.futunn.com",              # broker content portal / secondary aggregation
    "cryptonews.net",               # low-authority crypto news site
    "substack.com",                 # generic newsletter host; allow explicit exceptions only
    "rscapital.substack.com",       # retail investor Substack, no institutional credibility
    "manilatimes.net",              # low-quality syndicated business wire / low signal for this pipeline
    "timesofindia.indiatimes.com",  # broad liveblogs / low signal for institutional market work
]

# ── Filter context size limits ────────────────────────────────────────────────
# Applied consistently in filter_material.py across Pass 1 and Pass 2.
# Sized to fit ~800–1500 tokens per context block without mid-sentence truncation.

MAX_KPI_CHARS      = 1500   # kpi_tree.md sent to Pass 1 (stripped) and Pass 2 (full)
MAX_CATALYST_CHARS = 900    # catalysts near-term section
MAX_THESIS_CHARS   = 900    # thesis variant view + risks (Pass 2 only)
MAX_DEBATES_CHARS  = 1200   # current key debates / resolution criteria

# ── Special channels (macro-open, catalyst-alerts, weekly-digest) ────────────
# Created by discord_setup.py; IDs stored under these keys in channel_map.json.

SPECIAL_CHANNELS = {
    "special/daily-briefing":  {"channel": "daily-briefing",  "category": "DAILY-UPDATES"},
    "special/macro-open":      {"channel": "macro-open",      "category": "DAILY-UPDATES"},
    "special/catalyst-alerts": {"channel": "catalyst-alerts", "category": "DAILY-UPDATES"},
    "special/breaking":        {"channel": "breaking",        "category": "DAILY-UPDATES"},
    "special/weekly-digest":   {"channel": "weekly-digest",   "category": "WEEKLY"},
    "special/coverage-updates": {"channel": "coverage-updates", "category": "META"},
    "special/earnings-uploads": {"channel": "earnings-uploads", "category": "META"},
    "special/bot-commands":     {"channel": "bot-commands",     "category": "META"},
    "special/twitter-signal":   {"channel": "twitter-signal",   "category": "SIGNALS"},
}

# ── Twitter signal config ─────────────────────────────────────────────────────
TWITTER_SIGNAL_MAX_PER_RUN    = 5   # max Discord posts per run
TWITTER_SIGNAL_LOOKBACK_HOURS = 10  # fetch tweets from last N hours (fallback if --since-ts not provided)

# ── X / Twitter (optional — requires X API v2 credentials) ───────────────────
# Set X_BEARER_TOKEN in .env to enable. Leave empty to skip.

# ── Ticker metadata — used by earnings system ────────────────────────────────
# sec_cik:  SEC EDGAR Central Index Key (10-digit, zero-padded). None = not a SEC filer.
# ir_page:  Investor relations landing page (for transcript scraping).
# av_symbol: Alpha Vantage symbol for earnings calendar / estimates.
#            None = not covered by Alpha Vantage (non-US-listed).

TICKER_META: dict[str, dict] = {
    "tickers/JPM":  {
        "company_name": "JPMorgan Chase & Co.",
        "sec_cik":  "0000019617",
        "ir_page":  "https://www.jpmorganchase.com/ir/quarterly-earnings",
        "website_probe_urls": [
            "https://www.jpmorganchase.com/ir/quarterly-earnings",
            "https://www.jpmorganchase.com/ir/annual-report",
        ],
        "exchange_adapter": "sec",
        "exchange_symbol": "JPM",
        "av_symbol": "JPM",
        "finnhub_symbol": "JPM",
    },
    "tickers/HSBC": {
        "company_name": "HSBC Holdings plc",
        "sec_cik":  None,
        "ir_page":  "https://www.hsbc.com/investors/results-and-announcements",
        "website_probe_urls": [
            "https://www.hsbc.com/investors/results-and-announcements",
            "https://www.hsbc.com/investors/results-and-announcements/annual-report",
        ],
        "exchange_adapter": "hkex",
        "exchange_code": "0005",
        "exchange_symbol": "0005",
        "av_symbol": None,
        "finnhub_symbol": "HSBC",
    },
    "tickers/TMX":  {
        "company_name": "TMX Group Limited",
        "sec_cik":  None,          # TSX-listed (X.TO); files on SEDAR, not SEC
        "ir_page":  "https://www.tmx.com/investor-relations",
        "av_symbol": None,
        "finnhub_symbol": None,    # TSX-listed; thin Finnhub coverage
    },
    "tickers/STAN": {
        "company_name": "Standard Chartered PLC",
        "sec_cik":  None,          # LSE-listed; does not file with SEC
        "ir_page":  "https://www.sc.com/en/investors/financial-results/",
        "website_probe_urls": [
            "https://www.sc.com/en/investors/financial-results/",
        ],
        "exchange_adapter": "lse",
        "exchange_symbol": "STAN",
        "exchange_slug": "standard-chartered-plc",
        "av_symbol": None,
        "finnhub_symbol": None,    # LSE-listed; not on Finnhub
    },
    "tickers/MUFG": {
        "company_name": "Mitsubishi UFJ Financial Group, Inc.",
        "sec_cik":  None,
        "ir_page":  "https://www.mufg.jp/english/ir/financialinfo/index.html",
        "exchange_adapter": "tse",
        "exchange_code": "8306",
        "exchange_symbol": "8306",
        "av_symbol": None,
        "finnhub_symbol": "MUFG",
    },
    "tickers/MFG": {
        "company_name": "Mizuho Financial Group, Inc.",
        "sec_cik":  None,
        "ir_page":  "https://www.mizuhogroup.com/investors",
        "exchange_adapter": "tse",
        "exchange_code": "8411",
        "exchange_symbol": "8411",
        "av_symbol": None,
        "finnhub_symbol": "MFG",
    },
    "tickers/GRAB": {
        "company_name": "Grab Holdings Limited",
        "sec_cik":  "0001833928",
        "ir_page":  "https://investors.grab.com/financial-information/quarterly-results",
        "exchange_adapter": "sec",
        "exchange_symbol": "GRAB",
        "av_symbol": "GRAB",
        "finnhub_symbol": "GRAB",
    },
    "tickers/SE":   {
        "company_name": "Sea Limited",
        "sec_cik":  "0001726445",
        "ir_page":  "https://www.sea.com/investors/financials/results",
        "exchange_adapter": "sec",
        "exchange_symbol": "SE",
        "av_symbol": "SE",
        "finnhub_symbol": "SE",
    },
    "tickers/FUTU": {
        "company_name": "Futu Holdings Limited",
        "sec_cik":  "0001780731",
        "ir_page":  "https://ir.futuholdings.com/financial-information/quarterly-results",
        "exchange_adapter": "sec",
        "exchange_symbol": "FUTU",
        "av_symbol": "FUTU",
        "finnhub_symbol": "FUTU",
    },
    "tickers/GOOG": {
        "company_name": "Alphabet Inc.",
        "sec_cik":  "0001652044",
        "ir_page":  "https://abc.xyz/investor/",
        "website_probe_urls": [
            "https://abc.xyz/investor/",
            "https://abc.xyz/investor/Earnings/default.aspx",
        ],
        "exchange_adapter": "sec",
        "exchange_symbol": "GOOG",
        "av_symbol": "GOOG",
        "finnhub_symbol": "GOOGL",
    },
    "tickers/MMYT": {
        "company_name": "MakeMyTrip Limited",
        "sec_cik":  "0001403708",
        "ir_page":  "https://investors.makemytrip.com/financial-information/quarterly-results",
        "exchange_adapter": "sec",
        "exchange_symbol": "MMYT",
        "av_symbol": "MMYT",
        "finnhub_symbol": "MMYT",
    },
    "tickers/8316": {
        "company_name": "Sumitomo Mitsui Financial Group, Inc.",
        "sec_cik":  "0001023428",   # SMFG files 20-F with SEC
        "ir_page":  "https://www.smfg.co.jp/english/investor/financial/",
        "exchange_adapter": "tse",
        "exchange_code": "8316",
        "exchange_symbol": "8316",
        "av_symbol": None,           # Not on Alpha Vantage
        "finnhub_symbol": "SMFG",   # US-listed ADR
    },
    "tickers/1299": {
        "company_name": "AIA Group Limited",
        "sec_cik":  None,           # HKEx-listed; does not file with SEC
        "ir_page":  "https://www.aia.com/en/investor-relations/overview/results-presentations",
        "website_probe_urls": [
            "https://www.aia.com/en/investor-relations/overview",
            "https://www.aia.com/en/investor-relations/overview/results-presentations",
        ],
        "exchange_adapter": "hkex",
        "exchange_code": "1299",
        "exchange_symbol": "1299",
        "av_symbol": None,
        "finnhub_symbol": None,    # HKEx-listed; not on Finnhub
    },
}

# ── Future API key slots (add to .env when ready) ────────────────────────────
# ALPHA_VANTAGE_KEY  — free tier: 25 req/day. Used for earnings calendar + estimates.
# FINNHUB_API_KEY    — paid tier for transcripts and international data.
# FMP_API_KEY        — FinancialModelingPrep for transcripts.
# LINQALPHA_API_KEY  — LinqAlpha (no public API yet; upload transcripts to #earnings-uploads).

# ── Portfolio briefing — price data and baseline ──────────────────────────────
# YAHOO_SYMBOLS maps coverage key → yfinance ticker symbol.
# Only ticker keys are included (sectors/markets have no single price series).
# Currency notes:
#   STAN.L  — quoted in GBX (pence), not GBP
#   8316.T  — JPY
#   1299.HK — HKD
#   X.TO    — CAD

YAHOO_SYMBOLS: dict[str, str] = {
    "tickers/JPM":  "JPM",
    "tickers/HSBC": "0005.HK",
    "tickers/TMX":  "X.TO",
    "tickers/STAN": "STAN.L",
    "tickers/MUFG": "8306.T",
    "tickers/MFG":  "8411.T",
    "tickers/GRAB": "GRAB",
    "tickers/SE":   "SE",
    "tickers/FUTU": "FUTU",
    "tickers/GOOG": "GOOGL",
    "tickers/MMYT": "MMYT",
    "tickers/8316": "8316.T",
    "tickers/1299": "1299.HK",
}

# Anchor date for cumulative % calculations in portfolio_briefing.py.
# Set once when tracking begins; update manually if you want to reset the baseline.
BRIEFING_BASELINE_DATE: str = "2026-03-16"

# ── X / Twitter (optional — requires X API v2 credentials) ───────────────────
# Set X_BEARER_TOKEN in .env to enable. Leave empty to skip.

# ── RSS feeds per coverage item ──────────────────────────────────────────────
# Direct feeds from preferred financial outlets.  Higher quality and lower
# latency than web search.  Each coverage key maps to a list of feed URLs.
# Leave empty to skip RSS for that item.  Sector/market items get broad feeds;
# tickers get sector-relevant feeds (there are no per-company RSS feeds).

RSS_FEEDS: dict[str, list[str]] = {
    # ── Broad financial feeds (shared across tickers) ────────────────────────
    # These are reused in per-ticker lists below.
}

_RSS_REUTERS_BIZ  = "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best"
_RSS_CNBC_FINANCE = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"
_RSS_CNBC_WORLD   = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362"
_RSS_MW_TOP       = "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"
_RSS_NIKKEI       = "https://asia.nikkei.com/rss"

RSS_FEEDS = {
    # Tickers — sector-relevant feeds
    "tickers/JPM":  [_RSS_CNBC_FINANCE, _RSS_MW_TOP],
    "tickers/HSBC": [_RSS_CNBC_WORLD, _RSS_REUTERS_BIZ],
    "tickers/TMX":  [_RSS_MW_TOP],
    "tickers/STAN": [_RSS_CNBC_WORLD, _RSS_REUTERS_BIZ],
    "tickers/MUFG": [_RSS_NIKKEI, _RSS_REUTERS_BIZ],
    "tickers/MFG":  [_RSS_NIKKEI, _RSS_REUTERS_BIZ],
    "tickers/GRAB": [_RSS_CNBC_WORLD],
    "tickers/SE":   [_RSS_CNBC_WORLD],
    "tickers/FUTU": [_RSS_CNBC_WORLD],
    "tickers/GOOG": [_RSS_CNBC_FINANCE, _RSS_MW_TOP],
    "tickers/MMYT": [_RSS_CNBC_WORLD],
    "tickers/8316": [_RSS_NIKKEI],
    "tickers/1299": [_RSS_CNBC_WORLD],
    # Sectors
    "sectors/exchanges":      [_RSS_MW_TOP],
    "sectors/gold-miners":    [_RSS_MW_TOP, _RSS_REUTERS_BIZ],
    "sectors/uranium-miners": [_RSS_MW_TOP],
    "sectors/japan-banks":    [_RSS_NIKKEI],
    # Sectors (Asia)
    "sectors/korea-memory": [_RSS_CNBC_WORLD, _RSS_NIKKEI],
    # Markets
    "markets/korea": [_RSS_CNBC_WORLD, _RSS_NIKKEI],
}

# Broad RSS feeds can leak unrelated stories into single-name pipelines.
# For single-ticker coverage, require at least one alias/brand mention in the
# RSS title/summary before the article enters Pass 1.
RSS_MATCH_TERMS: dict[str, list[str]] = {
    "tickers/JPM":  ["jpmorgan", "jp morgan", "chase"],
    "tickers/HSBC": ["hsbc", "hang seng bank", "mid east", "hong kong wealth"],
    "tickers/TMX":  ["tmx", "toronto stock exchange", "tsx", "trayport", "montreal exchange"],
    "tickers/STAN": ["standard chartered", "stanchart"],
    "tickers/MUFG": ["mufg", "mitsubishi ufj", "bank of tokyo-mitsubishi", "morgan stanley stake"],
    "tickers/MFG":  ["mizuho", "mizuho financial", "mfg"],
    "tickers/GRAB": ["grab", "grabfin", "grabfood", "grabcar", "grabmart"],
    "tickers/SE":   ["sea limited", "shopee", "garena", "seamoney", "sea ltd"],
    "tickers/FUTU": ["futu", "moomoo"],
    "tickers/GOOG": ["alphabet", "google", "youtube", "android", "waymo", "gcp"],
    "tickers/MMYT": ["makemytrip", "make my trip", "goibibo", "redbus", "mybiz"],
    "tickers/8316": ["sumitomo mitsui", "smfg", "smcc", "olive"],
    "tickers/1299": ["aia"],
    "sectors/exchanges": ["trayport", "cme", "ice", "cboe", "lseg", "tmx", "hkex", "nasdaq", "nyse", "eurex", "lch", "clearing", "post-trade", "market data", "index", "exchange operator"],
    "sectors/gold-miners": ["gold", "miner", "mining", "newmont", "barrick", "agnico", "kinross", "aisc", "gdx"],
    "sectors/uranium-miners": ["uranium", "nuclear", "reactor", "cameco", "kazatomprom", "yellowcake", "u3o8", "ura"],
    "sectors/japan-banks": ["boj", "bank of japan", "megabank", "mufg", "smfg", "mizuho", "jgb", "japan bank"],
    "sectors/korea-memory": ["samsung", "sk hynix", "hbm", "dram", "nand", "memory", "semiconductor", "chip", "fab", "cxmt", "micron"],
    "markets/korea": ["korea", "kospi", "kosdaq", "won", "krw", "ewy", "samsung", "sk hynix", "value-up"],
}

X_ACCOUNTS: dict[str, list[str]] = {
    "tickers/JPM":         ["jpmorgan"],
    "tickers/8316":        [],
    "tickers/GOOG":        ["Google", "sundarpichai"],
    "sectors/japan-banks": ["BOJ_q"],
    "sectors/gold-miners": [],
}
