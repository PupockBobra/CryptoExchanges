"""
Market-maker presence detection — all tunables in one place.

Nothing here is hard-coded downstream: the collector reads the capture settings,
the analytic core takes a :class:`DetectParams` (the API lets the page override
every field per request), and the page's sliders are seeded from the defaults.

Venue is SPB Exchange only (Finam MIC ``RUSX``).  The instrument list is the
subset of ``SPB_INSTRUMENTS`` we were asked to watch; names and lots come from
there so there is still a single source of truth for the venue.
"""

from dataclasses import dataclass, field

from app.spb.config import SPB_INSTRUMENTS

# ── Capture ──────────────────────────────────────────────────────────────────

# Tickers to capture, as the requested base names.  ANTHROPIC / OPENAI / SPCX
# are deliberately absent: they do not resolve on SPB (checked against Finam,
# 04.08.2026) — pre-IPO names trade on crypto venues, not here.
MMD_BASES: list[str] = [
    # ── Single-stock perps ───────────────────────────────────────────────────
    "AMD", "AMZN", "APP", "COHR", "COIN", "CRWD", "HOOD", "LITE",
    "NBIS", "NFLX", "PANW", "SMCI", "SNDK", "TSLA", "UBER",
    # ── Crypto-index perps (06.08.2026) ──────────────────────────────────────
    # Nothing in the detector needs special-casing for these, but they do look
    # different from the equity names, and it is worth recording why the same
    # thresholds still hold: the tick varies by four orders of magnitude
    # (BTC 0.1 … TRX 0.00001) and top-of-book spreads are far tighter
    # (measured 06.08.2026: BTC 0.1 bp, ETH 1.7, SOL 2.7, XRP 2.9, TRX 2.1,
    # against 6–20 bp on the stocks).  The step is inferred per instrument and
    # the search radius is a fraction of price, so both scale by themselves;
    # the contract multiplier (BTC 0.0001 … XRP/TRX 10) is applied where money
    # is computed, from ``SPB_LOTS``.
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "TRXUSD",
]
MMD_TICKERS: list[str] = [f"{b}perpA" for b in MMD_BASES]

# Sanity: every captured ticker must be a known SPB instrument (name/lot lookup).
MMD_TICKERS = [t for t in MMD_TICKERS if t in SPB_INSTRUMENTS]

SAMPLE_SEC = 5            # one stored snapshot per instrument per 5 s (epoch grid)
STORE_DEPTH = 20          # levels stored per side; deeper levels never carry MM
                          # quotes and would multiply the row count for nothing

# A book that has not changed is normal (an MM standing still is exactly what we
# are looking for), but a book that stopped updating because the FEED died looks
# identical — and would fake perfect persistence.  Samples are therefore written
# only while the stream session is known alive; otherwise the grid point is
# recorded as a miss.
STALE_AFTER_SEC = 90.0

# ── Instrument metrics ───────────────────────────────────────────────────────

# SPB perps quote in USD with a 0.01 tick.  Used only as the fallback: the real
# step is inferred from the observed price grid (``infer_price_step``), so a
# venue change cannot silently corrupt the "distance in steps" axis.
DEFAULT_PRICE_STEP = 0.01

# A price level in the book shows the SUM of every order resting at that price,
# so a maker's own order disappears into a bigger number the moment somebody
# queues an identical one beside it.  Measured on UBER (2127 snapshots): the bid
# level read 350 in 51.8% of snapshots and 700 in 48.0% — never both, 99.8%
# together — i.e. one 350-lot quote that was always there, joined half the time
# by a second one.  Matching only the exact size scored it 0.52 and the maker was
# never confirmed.  A level therefore also counts for candidate V when it carries
# a whole number of V's, up to this multiple.  Kept small: at 3 the bands are
# still narrow (the tolerance is measured against V, not against k·V), while a
# larger cap would let one small size explain half the book.
MAX_STACK_MULTIPLE = 3

# …and the guard the multiple rule needs.  A candidate whose presence comes almost
# entirely from being "half of somebody else's level" is not evidence of anything:
# with k up to 3 a small size divides into a great many levels.  A candidate must
# therefore have been seen standing ALONE at its own price in at least this share
# of the snapshots that counted for it.  Observed on AMD right after the stack
# rule shipped: candidates of 17 and 5 lots stood alone in 1% and 2% of snapshots
# and were being reported as quoters.
MIN_ALONE_SHARE = 0.10

# Corridors around the mid for the cumulative-depth metric, as a fraction of mid.
CORRIDORS: tuple[float, ...] = (0.001, 0.0025, 0.005)

# ── Detection thresholds (every one of these is a slider on the page) ────────


@dataclass(frozen=True)
class DetectParams:
    """Thresholds of the detector.

    Persistence is asked of a resting SIZE — "a level of this volume stood
    somewhere within the search radius" — and pairing is by size alone, because
    a market maker's obligation ties the two quotes' sizes, not their distances
    from the mid.  Distance is an output, reported per side.

    ``bin_steps=None`` means "derive from the data".  It only sets the row height
    of the heat map now (median top-of-book spread in steps); the detector itself
    no longer bins by distance.
    """

    persistence_min: float = 0.7    # share of snapshots a cluster must hold
    volume_tol: float = 0.10        # ±10% counts as "the same" resting volume
    symmetry_tol: float = 0.25      # |V_bid-V_ask| / max, above this → one-sided
    # Why a volume floor exists at all: the symmetry test is a RATIO, so a single
    # lot resting on each side passes it exactly (|1-1|/1 = 0) and a stray
    # 1-contract order that nobody bothers to cancel is about as persistent as
    # anything in the book.  Observed live on NFLX: a 1×1 pair scored 0.97 / 0.87
    # presence and was reported as "MM volume: 1".  A quoting obligation is never
    # one lot, so this floor costs nothing and removes the whole class.
    min_cluster_volume: float = 2.0
    # How far from the mid a maker's quote may sit and still count.  Expressed as
    # a fraction of price, not as a step count, so one setting means the same
    # thing on a $30 and a $1400 instrument.  Distance is NOT a matching
    # condition (bid and ask may rest at different distances) — this only bounds
    # where the search looks, keeping deep unrelated size out of the estimate.
    search_radius_pct: float = 0.005
    bin_steps: int | None = None    # heat-map row width, in price steps
    max_offset_steps: int = 2000    # ignore levels further out than this
    min_snapshots: int = 20         # below this a "share" is not a statistic
    corridors: tuple[float, ...] = field(default=CORRIDORS)


DEFAULTS = DetectParams()

# ── Session modes (Moscow time) ──────────────────────────────────────────────
# MM obligations normally apply to the main session only, so the page can split
# the metrics by mode and read the difference as the MM contribution.  Bounds are
# hours (MSK), [start, end).
SESSION_MODES: dict[str, tuple[float, float]] = {
    "morning": (7.0, 10.0),
    "main":    (10.0, 18.75),
    "evening": (18.75, 23.75),
}
SESSION_MODE_LABELS: dict[str, str] = {
    "all":     "Все режимы",
    "morning": "Утренняя 07:00–10:00",
    "main":    "Основная 10:00–18:45",
    "evening": "Вечерняя 18:45–23:45",
}
