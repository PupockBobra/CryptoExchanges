"""
MM (market-maker) FORTS futures — static configuration.

Six tabs, one per ISS FORTS collection (see /iss/securitygroups/futures_forts/
collections).  Each tab shows, for the *front-month* contract of every liquid
underlying, a live order book + two spread-on-volume charts (absolute in the
instrument's quote currency, and basis points) — the same design as the SPB
Order Book page.

Finam serves MOEX FORTS derivatives under MIC "RTSX" as "<SECID>@RTSX" (the same
path the crypto-index overlay on the SPB page already uses).
"""

MM_MIC = "RTSX"                    # Finam MIC for MOEX FORTS derivatives

# ISS collection id (under securitygroup "futures_forts") → tab id + label.
# Order here is the sidebar order.
MM_GROUPS: list[dict] = [
    {"id": "index",     "collection": "futures_forts_index",     "label": "Фьючерсы на индексы"},
    {"id": "shares",    "collection": "futures_forts_shares",    "label": "Фьючерсы на акции"},
    {"id": "currency",  "collection": "futures_forts_currency",  "label": "Фьючерсы на валюты"},
    {"id": "commodity", "collection": "futures_forts_commodity", "label": "Фьючерсы на товарные контракты"},
]
MM_GROUP_IDS: list[str] = [g["id"] for g in MM_GROUPS]
MM_COLLECTION: dict[str, str] = {g["id"]: g["collection"] for g in MM_GROUPS}
MM_LABEL: dict[str, str] = {g["id"]: g["label"] for g in MM_GROUPS}

# Underlyings that ISS does NOT list in their collection but that must still be
# tracked: ASSETCODE → group id.  WTI (нефть ВТИ, quoted in $/barrel, listed
# 13.07.2026) trades on FORTS and shows up in the market snapshot, but the
# ``futures_forts_commodity`` collection doesn't contain it — the collection walk
# alone would silently miss it.  Front-month / step_ratio / liquidity gate are
# resolved for these from the same market snapshot as everything else.
MM_EXTRA_ASSETS: dict[str, str] = {"WTI": "commodity"}

# Liquidity gate: keep an underlying's front-month only if its open interest is
# worth at least this many roubles (OPENPOSITION × price × STEPPRICE/MINSTEP).
# Dead books (empty stack) are excluded so we don't stream them on the brokerage
# token.  Instruments that pass but still lack 1 000 000 ₽ of book depth simply
# show "нет данных" on the chart (the depth gate is the finer filter).  Tunable.
MM_MIN_OI_RUB: float = 10_000_000.0

# Per-side depth target for the spread-on-volume math (roubles on the bid AND on
# the ask), same as the SPB Order Book page's 1 млн ₽ line.
MM_TARGET_RUB: float = 1_000_000.0

# ISS FACEUNIT / quote-unit → display symbol for the absolute-spread axis.
# FORTS futures are quoted in their underlying's currency (₽ for shares & most
# FX crosses, $ for USR = US-dollar contracts, points for index futures), while
# STEPPRICE/MINSTEP (always roubles) sizes the depth regardless.
_CCY_SYMBOL: dict[str, str] = {
    "RUB": "₽", "SUR": "₽", "USR": "$", "USD": "$", "EUR": "€", "GBP": "£",
    "JPY": "¥", "CNY": "¥", "CHF": "₣", "CAD": "C$", "KZT": "₸", "TRY": "₺",
    "INR": "₹",
}


def quote_symbol(faceunit: str | None, unit: str | None) -> str:
    """Display symbol for the absolute spread of a FORTS contract.

    The free-text ``UNIT`` wins when it says "в пунктах", because for index
    futures ``FACEUNIT`` names the currency the contract is *settled* through,
    not the unit it is *quoted* in: RTS/RVI carry ``USR`` and MIX/RGBI ``RUB``
    while all of them are quoted in index points, and MOEXCNY carries ``CNY``.
    Trusting FACEUNIT first labeled an RTS spread of 25.8 points as "25.8 $".

    Otherwise use the explicit ``FACEUNIT`` code (GOLD/WTI → $, Si/WHEAT → ₽),
    falling back to the ``UNIT`` text.  Unknown codes are shown verbatim so
    nothing is silently mislabeled."""
    u = (unit or "").lower()
    if "пункт" in u:
        return "пт"
    if faceunit:
        return _CCY_SYMBOL.get(faceunit.strip().upper(), faceunit.strip())
    if "доллар" in u:
        return "$"
    if "рубл" in u:
        return "₽"
    return "₽"
