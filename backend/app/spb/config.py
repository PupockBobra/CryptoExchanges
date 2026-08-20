"""
SPB Exchange perpetual-futures configuration (via Finam TradeAPI).

These are the "вечные фьючерсы" listed on СПБ Биржа.  Finam serves them under
MIC ``RUSX`` with the ticker suffix ``perpA`` (e.g. ``AMZNperpA@RUSX``).  They
are NOT present in Finam's ``/v1/assets`` dump, but quotes/bars resolve.

Turnover is quoted in USD (price × size × lot); the ETL converts to RUB at query
time via the shared ``moex_fx_rates`` table — the same USDRUBF rate used by the
crypto and MOEX volume pages, so all turnover charts stay comparable.

Lot size matters for the historical backfill: daily bars expose only ``volume``
(in contracts), so turnover is approximated as ``volume × price × lot``.  The
single-stock perps are lot 1, but the crypto-index perps use fractional/large
lots (e.g. BTC index = 0.0001 BTC/contract), verified empirically against the
live quote's exact turnover (turnover / (volume × price) == lot).
"""

# Finam MIC under which the SPB perpetual futures resolve.
FINAM_MIC = "RUSX"

# ticker → (display name, lot size, display group).  Single source of truth.
SPB_INSTRUMENTS: dict[str, tuple[str, float, str]] = {
    # ── Single-stock perps (lot 1) ──────────────────────────────────────────
    "AMDperpA":  ("Advanced Micro Devices", 1.0, "US Market"),
    "AMZNperpA": ("Amazon.com",             1.0, "US Market"),
    "APPperpA":  ("AppLovin",               1.0, "US Market"),
    "BSXperpA":  ("Boston Scientific",      1.0, "US Market"),
    "CVNAperpA": ("Carvana",                1.0, "US Market"),
    "CMGperpA":  ("Chipotle Mexican Grill", 1.0, "US Market"),
    "COHRperpA": ("Coherent",               1.0, "US Market"),
    "COINperpA": ("Coinbase Global",        1.0, "US Market"),
    "CRWDperpA": ("CrowdStrike Holdings",   1.0, "US Market"),
    "DASHperpA": ("DoorDash",               1.0, "US Market"),
    "LULUperpA": ("lululemon athletica",    1.0, "US Market"),
    "LITEperpA": ("Lumentum Holdings",      1.0, "US Market"),
    "NBISperpA": ("Nebius Group",           1.0, "US Market"),
    "NFLXperpA": ("Netflix",                1.0, "US Market"),
    "PANWperpA": ("Palo Alto Networks",     1.0, "US Market"),
    "HOODperpA": ("Robinhood Markets",      1.0, "US Market"),
    "SNDKperpA": ("SanDisk",                1.0, "US Market"),
    "SMCIperpA": ("Super Micro Computer",   1.0, "US Market"),
    "TSLAperpA": ("Tesla",                  1.0, "US Market"),
    "UBERperpA": ("Uber Technologies",      1.0, "US Market"),
    # ── Crypto-index perps (fractional / large lots) ────────────────────────
    "BTCUSDperpA": ("Bitcoin Index",  0.0001, "Crypto"),
    "ETHUSDperpA": ("Ethereum Index", 0.001,  "Crypto"),
    "SOLUSDperpA": ("Solana Index",   0.1,    "Crypto"),
    "XRPUSDperpA": ("Ripple Index",   10.0,   "Crypto"),
    "TRXUSDperpA": ("Tron Index",     10.0,   "Crypto"),
}

# Display group order (sections on the SPB Volume page).
SPB_GROUP_ORDER: list[str] = ["US Market", "Crypto"]

# Derived lookups.
SPB_NAMES:  dict[str, str]   = {t: name  for t, (name, _, _) in SPB_INSTRUMENTS.items()}
SPB_LOTS:   dict[str, float] = {t: lot   for t, (_, lot, _) in SPB_INSTRUMENTS.items()}
SPB_GROUPS: dict[str, str]   = {t: group for t, (_, _, group) in SPB_INSTRUMENTS.items()}

# Tickers the ETL fetches, in display order.
SPB_TICKERS: list[str] = list(SPB_INSTRUMENTS.keys())
