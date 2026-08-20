"""
Replay + analysis service: DB rows → snapshots → :mod:`app.mmdetect.core`.

This is the offline half of the estimator.  It never touches Finam — everything
it serves is computed from stored snapshots, so the page can re-run the detector
with different thresholds as often as the analyst drags a slider, and so the
whole analysis keeps working on a machine with no market access at all.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.db.timescale import (
    fetch_ob_coverage,
    fetch_ob_miss_stats,
    fetch_ob_snapshots,
)
from app.mmdetect.config import (
    MMD_TICKERS,
    SAMPLE_SEC,
    SESSION_MODES,
    STORE_DEPTH,
    DetectParams,
)
from app.mmdetect.core import analyze, build_heatmap
from app.spb.config import SPB_GROUPS, SPB_NAMES

log = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))
MAX_WINDOW_HOURS = 48       # a wider window is a report, not an interactive page


def window(hours: float, end: datetime | None = None) -> tuple[datetime, datetime]:
    """[from, to) clamped to a sane width.  Both ends always bounded — an
    unbounded scan locks every chunk of the hypertable."""
    to = end or datetime.now(timezone.utc)
    hours = max(0.25, min(float(hours), MAX_WINDOW_HOURS))
    return to - timedelta(hours=hours), to


def in_session_mode(ts: datetime, mode: str) -> bool:
    """Does ``ts`` fall in the requested trading mode (Moscow time)?

    Splitting by mode is how the page offers the only quasi-control the data
    allows: MM obligations normally bind during the main session, so comparing
    main against evening reads as an estimate of the maker's contribution.
    """
    if mode == "all" or mode not in SESSION_MODES:
        return True
    lo, hi = SESSION_MODES[mode]
    h = ts.astimezone(_MSK)
    return lo <= h.hour + h.minute / 60.0 < hi


def stride_for(hours: float, target_snapshots: int = 600) -> int:
    """Sampling stride (seconds) that keeps a window near ``target_snapshots``.

    Used by the all-instrument summary: 20 instruments × a 6-hour window at the
    full 5-second grid is over a million rows per request, and the summary needs
    the shape of the record rather than every frame.  A multiple of the capture
    grid keeps the thinned series epoch-aligned.

    600 rather than 1500 (06.08.2026): the detector's cost is linear in samples,
    and at 1500 the summary took ~7 s of CPU for 20 instruments — long enough
    that the page rendered empty before the first response.  Every figure in the
    summary is a share or a median over these samples, and 600 is far past the
    point where either is noisy; the per-instrument card still uses the full
    5-second grid, and the stride is printed under the table.
    """
    points = hours * 3600 / SAMPLE_SEC
    k = max(1, int(points / target_snapshots + 0.999))
    return SAMPLE_SEC * k


async def load_snapshots(symbol: str, ts_from: datetime, ts_to: datetime,
                         mode: str = "all", stride_sec: int | None = None) -> list[dict]:
    """Stored levels → snapshots, best price first on each side.

    Rows arrive ordered by (ts, side, level_idx), so one pass regroups them; the
    stored order already is book order, which is why ``level_idx`` is persisted
    rather than re-derived here.
    """
    rows = await fetch_ob_snapshots(symbol, ts_from, ts_to, stride_sec)
    snaps: list[dict] = []
    cur_ts = None
    cur: dict | None = None
    for r in rows:
        if r["ts"] != cur_ts:
            if cur is not None:
                snaps.append(cur)
            cur_ts = r["ts"]
            cur = {"ts": cur_ts, "bids": [], "asks": []}
        key = "bids" if r["side"] == "bid" else "asks"
        cur[key].append({"price": float(r["price"]), "size": float(r["volume"])})
    if cur is not None:
        snaps.append(cur)
    if mode != "all":
        snaps = [s for s in snaps if in_session_mode(s["ts"], mode)]
    return snaps


def downsample(series: list[dict], max_points: int = 1200) -> list[dict]:
    """Thin the per-snapshot series for the chart by taking the median of each
    equal slice.  A trading day at a 5-second grid is ~12 000 points; the median
    keeps the level of the line honest where plain decimation would sample
    whichever instant happened to land on the stride.

    Key-generic on purpose: the series carries one column per detected quoter
    (``mm0_bps``, ``mm1_bps``, …) and how many there are is not known here."""
    n = len(series)
    if n <= max_points:
        return [{k: v for k, v in s.items() if k != "mid"} for s in series]
    keys = [k for k in series[0] if k not in ("ts", "mid")]
    out = []
    edges = [round(i * n / max_points) for i in range(max_points + 1)]
    for i in range(max_points):
        lo, hi = edges[i], edges[i + 1]
        if hi <= lo:
            continue
        chunk = series[lo:hi]
        row = {"ts": chunk[len(chunk) // 2]["ts"]}
        for k in keys:
            vals = sorted(c[k] for c in chunk if c.get(k) is not None)
            row[k] = vals[len(vals) // 2] if vals else None
        out.append(row)
    return out


def _iso(series: list[dict]) -> list[dict]:
    """ISO timestamps and rounded values — float noise ("77.99999999999727"
    steps) is both misleading in a tooltip and a chunk of the payload."""
    nd = {"spread_abs": 6, "spread_steps": 2, "spread_bps": 3}
    out = []
    for s in series:
        row = {"ts": s["ts"].isoformat() if isinstance(s["ts"], datetime) else s["ts"]}
        for k, v in s.items():
            if k in ("ts", "mid"):
                continue
            row[k] = None if v is None else round(v, nd.get(k, 3))
        out.append(row)
    return out


async def analyze_symbol(symbol: str, ts_from: datetime, ts_to: datetime,
                         params: DetectParams, mode: str = "all",
                         with_heatmap: bool = True, heatmap_cols: int = 240,
                         stride_sec: int | None = None) -> dict:
    """Full result for one instrument: estimate, evidence and chart series."""
    snaps = await load_snapshots(symbol, ts_from, ts_to, mode, stride_sec)
    res = analyze(snaps, params, depth_cap=STORE_DEPTH)
    heat = build_heatmap(res, heatmap_cols) if with_heatmap else None
    res.pop("_bins", None)
    res["series"] = _iso(downsample(res["series"]))
    return {
        "ticker": symbol,
        "name": SPB_NAMES.get(symbol, symbol),
        "from": ts_from.isoformat(),
        "to": ts_to.isoformat(),
        "mode": mode,
        "params": {
            "persistence_min": params.persistence_min,
            "volume_tol": params.volume_tol,
            "symmetry_tol": params.symmetry_tol,
            "bin_steps": res["bin_steps"],
            "bin_steps_auto": params.bin_steps is None,
            "corridors": list(params.corridors),
            "stride_sec": stride_sec or SAMPLE_SEC,
        },
        "result": res,
        "heatmap": heat,
    }


async def coverage(ts_from: datetime, ts_to: datetime) -> dict[str, dict]:
    """Per instrument: how complete the captured record is over the window.

    ``expected`` is grid points inside SPB trading hours only — comparing a
    night-time window against a 5-second grid would report a fake 5% coverage.
    """
    from app.spb.spread_etl import _is_trading_now

    stored = {r["symbol"]: r for r in await fetch_ob_coverage(ts_from, ts_to)}
    missed = {r["symbol"]: r for r in await fetch_ob_miss_stats(ts_from, ts_to)}

    expected = 0
    t = ts_from
    step = timedelta(seconds=SAMPLE_SEC)
    while t < ts_to:
        if _is_trading_now(t):
            expected += 1
        t += step

    out = {}
    for ticker in MMD_TICKERS:
        s = stored.get(ticker)
        m = missed.get(ticker)
        n = int(s["n_snapshots"]) if s else 0
        n_missed = int(m["n_missed"]) if m and m["n_missed"] is not None else 0
        out[ticker] = {
            "ticker": ticker,
            "name": SPB_NAMES.get(ticker, ticker),
            "group": SPB_GROUPS.get(ticker, ""),
            "n_snapshots": n,
            "first_ts": s["first_ts"].isoformat() if s and s["first_ts"] else None,
            "last_ts": s["last_ts"].isoformat() if s and s["last_ts"] else None,
            "expected": expected,
            # Share of grid points with no stored snapshot.  Counted against the
            # trading-hours grid rather than against the collector's own tally so
            # a collector that was simply not running still shows up as a gap.
            "miss_ratio": (1.0 - n / expected) if expected else None,
            "n_missed_logged": n_missed,
        }
    return out
