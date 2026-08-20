"""
OKR — MOEX mirror contracts as a share of the crypto exchanges' TradFi turnover.

One number: the daily turnover of two MOEX FORTS baskets divided by the daily
TradFi turnover of the six crypto venues (Binance, OKX, Bybit, MEXC, Bitget,
Hyperliquid).  Both sides in roubles, so the ratio is dimensionless.

The baskets are ISS ASSETCODEs, verified against the FORTS market snapshot and
each contract's ISS description (GROUPTYPE / FACEUNIT / CONTRACTNAME) on
20.08.2026.  They are only a *filter*: the ETL stores the daily VALUE of every
FORTS asset, so changing a basket needs no re-backfill.
"""

# ── Basket A: mirror commodity contracts ──────────────────────────────────────
# GROUPTYPE = "Товары" with a foreign benchmark as the underlying.  The first
# block is quoted in dollars (FACEUNIT = USR); the three at the end mirror ICE
# cocoa / raw sugar and Dutch TTF gas but are quoted in ₽ / € — they are mirror
# contracts all the same, so they were included by explicit decision.
#
# Deliberately NOT here: WHEAT, AI92, AI95, DTL, SUGAR (domestic Russian goods),
# and GL / SL / GLDRUBF / SLVRUBF (rouble-per-gram gold and silver — the same
# metal, but the domestic precious-metals contract rather than the mirror one).
COMMODITY_BASKET: tuple[str, ...] = (
    # Energy
    "BR", "BRM", "NG", "NGM", "WTI",
    # Precious metals (main + mini)
    "GOLD", "GOLDM", "SILV", "SILVM", "PLT", "PLTM", "PLD", "PLDM",
    # Base metals
    "COPPER", "ALUM", "NICKEL", "ZINC",
    # Softs
    "COFFEE", "ORANGE",
    # Mirror contracts quoted in ₽ / € rather than $
    "COCOA", "SUGR", "TTF",
)

# ── Basket B: futures on foreign securities ───────────────────────────────────
# Foreign ETF shares / index units and foreign shares & ADRs.  Russian indices
# and shares are excluded, and so are the MOEX crypto-index futures (BTC / ETH /
# SOL / XRP / TRX) — those track a MOEX index, not a foreign security.  IBIT and
# ETHA stay: they are futures on BlackRock's US-listed ETF shares.
FOREIGN_SECURITIES_BASKET: tuple[str, ...] = (
    # Foreign ETFs / index units
    "NASD", "SPYF", "IBIT", "ETHA", "SOXQ", "TLT", "QQQF", "SP500F",
    "DAX", "HANG", "NIKK", "STOX", "DJ30", "R2000", "EM",
    "KOREA", "INDIA", "CHINA", "BRAZIL", "ARGT", "SAUDI", "AFRICA",
    # Foreign shares and ADRs
    "TENCENT", "HYNIX", "BAIDU", "SAMSUNG", "ALIBABA", "XIA", "TSM",
    "JDCOM", "SAP", "NOVARTIS", "ASML", "TOYOTA", "PDD", "SONY",
)

OKR_BASKETS: dict[str, tuple[str, ...]] = {
    "commodity": COMMODITY_BASKET,
    "foreign":   FOREIGN_SECURITIES_BASKET,
}

# Every ASSETCODE that counts towards the numerator.
NUMERATOR_ASSETS: tuple[str, ...] = COMMODITY_BASKET + FOREIGN_SECURITIES_BASKET

# ── Denominator: the TradFi universe already collected on the crypto side ─────
# Equity perps come from stock_daily_volume (the whole ~460-ticker universe on
# all six venues).  Commodities / metals / index ETFs come from the curated
# `instruments` rows in ohlcv_daily — listed here as canonical BASE tickers.
#
# The curated US stocks that also live in `instruments` (NVDA, AAPL, …) are NOT
# here on purpose: the stock ETL already counts them, and adding the ohlcv rows
# would double them.  Same reasoning as `US_STOCK_CURATED_BASES` in timescale.py.
TRADFI_OHLCV_BASES: tuple[str, ...] = (
    # Energy
    "BRN", "BRENT", "WTI", "USOIL", "UKOIL", "NATGAS", "NGAS", "TTF",
    # Metals
    "XAU", "XAG", "XPT", "XPD", "COPPER", "ALUMINIUM",
    # Agricultural
    "WHEAT", "CORN", "URANIUM",
    # Index ETFs
    "QQQ", "SPY",
)

# ── ETL cadence ───────────────────────────────────────────────────────────────
# The page shows 30 days; 90 are kept so a moving average always has history.
BACKFILL_DAYS = 90
# Re-fetch this many recent days on every pass (ISS publishes late corrections).
LOOKBACK_DAYS = 3
# Seconds between passes.  The numerator only changes once a day, after the
# FORTS evening session settles.
ETL_INTERVAL_SEC = 21_600
