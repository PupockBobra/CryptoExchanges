"""
Analytic core of the MM-presence estimator — pure functions, no API, no DB.

Input is a list of order-book snapshots (whatever produced them: the live
collector, a DB replay, or a synthetic book in a test).  Output is the estimate
of *how much volume rests in the book with what spread*, plus the evidence
behind it.  Everything here is deterministic and side-effect free, which is what
makes the detector testable against synthetic books (see
``tests/test_mmdetect_core.py``).

Method, in short
----------------
An anonymous book never says who posted a level, so MM presence is inferred from
two properties an obligated quoter has and noise traffic does not:

1. **Persistence** — the same resting volume keeps showing up at the same
   distance from the mid.  Distance is measured in **price steps**, never in
   absolute price: the market moves and the quoter moves its quote with it, so
   an absolute-price axis would smear one standing quote across the whole range.
2. **Two-sidedness** — a cluster of comparable SIZE rests on the other side.
   This is what separates a quoter from a one-sided algo wall, the single most
   common false positive.  Note what is *not* required: the two quotes need not
   sit at the same distance from the mid.  The obligation ties their sizes, not
   their distances, and demanding mirror distances made the detector blind to
   real makers (see the note above ``find_size_clusters``).

Both are thresholds, not certainties — see the disclaimer rendered on the page.

Aggregates are medians and quartiles, never means: books tear on news, and one
torn snapshot drags a mean far more than it drags the estimate a human would
make by looking at the heat map.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime

from app.mmdetect.config import (
    DEFAULTS,
    DEFAULT_PRICE_STEP,
    MAX_STACK_MULTIPLE,
    MIN_ALONE_SHARE,
    STORE_DEPTH,
    DetectParams,
)

Level = dict          # {"price": float, "size": float}
Snapshot = dict       # {"ts": datetime, "bids": [Level], "asks": [Level]}

BID, ASK = "bid", "ask"

# How many confirmed quoters get a per-snapshot spread series.  The chart can
# only carry a few lines legibly, and the payload grows with each one; every
# pair still gets its summary statistics regardless.
MAX_TRACKED_PAIRS = 4


# ── small statistics helpers ─────────────────────────────────────────────────

def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _pctl(vals: list[float], q: float) -> float | None:
    """Linear-interpolation percentile (``q`` in 0..1).  Small samples are the
    norm here (a thin instrument can leave a handful of valid snapshots), so
    this stays defined for n=1 instead of raising like ``statistics.quantiles``."""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _spread_stats(vals: list[float]) -> dict:
    return {"median": _median(vals), "p25": _pctl(vals, 0.25),
            "p75": _pctl(vals, 0.75), "n": len(vals)}


# ── instrument geometry ──────────────────────────────────────────────────────

def infer_price_step(snapshots: list[Snapshot], fallback: float = DEFAULT_PRICE_STEP) -> float:
    """Smallest positive gap between adjacent price levels seen in the sample.

    The tick is a property of the instrument, but nothing in the Finam order-book
    payload states it, and hard-coding it per venue is exactly the kind of
    assumption that rots silently.  A sparse book (SPB's quiet names quote 20+
    ticks apart) simply yields a larger inferred step on that window — which is
    still the right quantum for that book — and the fallback covers a window with
    a single level per side.
    """
    gaps: list[float] = []
    for snap in snapshots:
        for side in (snap.get("bids") or [], snap.get("asks") or []):
            for i in range(len(side) - 1):
                g = abs(side[i]["price"] - side[i + 1]["price"])
                if g > 1e-12:
                    gaps.append(round(g, 8))
    if not gaps:
        return fallback
    return min(gaps)


def top_of_book(snap: Snapshot) -> tuple[float, float, float] | None:
    """(best_bid, best_ask, mid) or None if either side is empty (a one-sided
    book has no mid, so every distance-from-mid metric is undefined)."""
    bids, asks = snap.get("bids") or [], snap.get("asks") or []
    if not bids or not asks:
        return None
    bb, ba = bids[0]["price"], asks[0]["price"]
    if bb <= 0 or ba <= 0:
        return None
    return bb, ba, (bb + ba) / 2.0


def offset_steps(price: float, mid: float, side: str, step: float) -> float:
    """Distance from the mid in price steps (always ≥ 0, both sides)."""
    d = (mid - price) if side == BID else (price - mid)
    return d / step


# ── per-snapshot metrics ─────────────────────────────────────────────────────

def corridor_depth(snap: Snapshot, mid: float, corridors: tuple[float, ...],
                   depth_cap: int = STORE_DEPTH) -> dict:
    """Cumulative resting volume inside ±corridor of the mid, per side.

    Returns per corridor: ``bid`` / ``ask`` in contracts, ``two_sided`` =
    ``min(bid, ask)`` (the size actually quoted on both sides, which is what an
    obligation is written against), the same in USD notional, and ``truncated``.

    ``truncated`` matters: the venue caps the book we receive, so an outermost
    stored level that still sits inside the corridor edge means the corridor is
    only partly observed and the number is a **lower bound**, not "there is
    nothing further out".
    """
    out = {}
    bids, asks = snap.get("bids") or [], snap.get("asks") or []
    for c in corridors:
        lo, hi = mid * (1 - c), mid * (1 + c)
        v_bid = sum(l["size"] for l in bids if l["price"] >= lo)
        v_ask = sum(l["size"] for l in asks if l["price"] <= hi)
        n_bid = sum(l["size"] * l["price"] for l in bids if l["price"] >= lo)
        n_ask = sum(l["size"] * l["price"] for l in asks if l["price"] <= hi)
        truncated = (
            (len(bids) >= depth_cap and bids[-1]["price"] > lo) or
            (len(asks) >= depth_cap and asks[-1]["price"] < hi)
        )
        out[c] = {
            "bid": v_bid, "ask": v_ask, "two_sided": min(v_bid, v_ask),
            "bid_usd": n_bid, "ask_usd": n_ask, "two_sided_usd": min(n_bid, n_ask),
            "truncated": truncated,
        }
    return out


def snapshot_bins(snap: Snapshot, mid: float, step: float, bin_steps: int,
                  max_offset_steps: int) -> dict[tuple[str, int], float]:
    """Resting volume per (side, offset bin) for one snapshot.

    The bin index is ``floor(offset_in_steps / bin_steps)``, so bin 0 holds the
    levels at the top of the book and bin *k* everything ``k`` bin-widths out.
    Volumes of several levels falling into one bin are summed — that is the
    point of binning a sparse grid.
    """
    out: dict[tuple[str, int], float] = {}
    for side, levels in ((BID, snap.get("bids") or []), (ASK, snap.get("asks") or [])):
        for lvl in levels:
            off = offset_steps(lvl["price"], mid, side, step)
            if off < 0 or off > max_offset_steps:
                continue
            b = int(off // bin_steps)
            key = (side, b)
            out[key] = out.get(key, 0.0) + lvl["size"]
    return out


def _bin_prices(snap: Snapshot, mid: float, step: float, bin_steps: int,
                b: int) -> tuple[float | None, float | None]:
    """Innermost bid price and innermost ask price inside bin ``b`` — the pair of
    prices an MM quoting at that distance would be showing."""
    best_bid = best_ask = None
    for lvl in snap.get("bids") or []:
        if int(offset_steps(lvl["price"], mid, BID, step) // bin_steps) == b:
            best_bid = lvl["price"] if best_bid is None else max(best_bid, lvl["price"])
    for lvl in snap.get("asks") or []:
        if int(offset_steps(lvl["price"], mid, ASK, step) // bin_steps) == b:
            best_ask = lvl["price"] if best_ask is None else min(best_ask, lvl["price"])
    return best_bid, best_ask


# ── cluster detection ────────────────────────────────────────────────────────
#
# What a market maker is obliged to do — and what it is NOT.
#
# Its two quotes must be of the same SIZE.  They need NOT sit at the same
# distance from the mid: the maker skews, the mid drifts when client flow steps
# inside on one side only, and nothing in the obligation ties the two distances
# together.  An earlier version of this detector required BOTH — same size and
# mirror distance — and that second requirement is what made it blind.
#
# Measured on AMD (1433 snapshots, 6 h): a ~140-lot quote was present on the ask
# in 96.4% of snapshots and on the bid in 99.4%, both sides at once in 96.0% —
# a textbook maker.  Yet its distance from the mid wandered across buckets
# (34% at 0–30 steps, 38% at 30–60, 22% at 150–180), so a per-distance score
# peaked at 0.50 and the pair was never confirmed.
#
# So persistence is now asked of the SIZE, not of a place: "a level of volume V
# exists somewhere within the search radius", and pairing is by size alone.
# Distance stops being a condition and becomes an output — reported per side,
# because now the two sides legitimately differ.


@dataclass
class SizeCluster:
    """A resting size that keeps showing up on one side of the book."""
    side: str
    volume: float                 # representative resting size (contracts)
    presence: float               # share of snapshots holding it within the radius
    presence_alone: float = 0.0   # …of which, alone at its price level (k = 1)
    qualifies: bool = False       # clears persistence + stand-alone thresholds
    dist_steps: float | None = None   # median distance from the mid, in price steps
    dist_bps: float | None = None     # the same, in bps of the mid
    matched: bool = False         # confirmed by a same-size cluster opposite


def dominant_volume(values: list[float], tol: float) -> tuple[float | None, int]:
    """The resting size that repeats most often, and how many samples carry it.

    A sample can hold two different plateaus over a window (the maker re-sized,
    or two participants took turns), and a plain median would land between them
    and match neither.  This takes the widest group of samples that are mutually
    within the tolerance — the mode of a continuous quantity — and returns its
    median as the representative size.
    """
    vals = sorted(v for v in values if v > 0)
    if not vals:
        return None, 0
    # Two samples belong to one plateau if both sit within ±tol of a common
    # centre, i.e. their ratio does not exceed (1+tol)/(1-tol).
    ratio = (1 + tol) / (1 - tol) if tol < 1 else float("inf")
    best_i, best_j, i = 0, 0, 0
    for j in range(len(vals)):
        while vals[j] > vals[i] * ratio:
            i += 1
        if j - i > best_j - best_i:
            best_i, best_j = i, j
    window = vals[best_i:best_j + 1]
    return _median(window), len(window)


def volume_centres(pooled: list[float], tol: float, max_centres: int = 12) -> list[float]:
    """Candidate resting sizes, densest first.

    Repeatedly takes the widest mutually-within-tolerance group of the pooled
    volumes (``dominant_volume``), records its median as a candidate, removes
    everything that candidate already explains, and looks again — so a book that
    rests 140 lots near the top and 20 lots deeper yields both, instead of one
    average that describes neither.
    """
    vals = sorted(v for v in pooled if v > 0)
    centres: list[float] = []
    while vals and len(centres) < max_centres:
        c, count = dominant_volume(vals, tol)
        if c is None or count < 2:      # nothing repeats any more — stop
            break
        centres.append(c)
        lo, hi = c * (1 - tol), c * (1 + tol)
        vals = [v for v in vals if not (lo <= v <= hi)]
    return sorted(centres)


def _match_centres(centres: list[float], v: float, tol: float,
                   max_k: int = MAX_STACK_MULTIPLE) -> list[tuple[float, int]]:
    """Every candidate size that could explain an observed level volume, with the
    number of them stacked there.

    A level carrying ``k`` identical orders reads as ``k·V`` — see
    ``MAX_STACK_MULTIPLE``.  The tolerance is taken against ``V`` rather than
    ``k·V``, so a stack has to be an exact multiple, not merely a large number.

    ALL matches are returned, not the best one: each candidate is a separate
    hypothesis scored on its own.  A level of 700 is evidence both for "a 700
    quote" and for "two 350 quotes", and picking only the tightest fit would let
    the larger candidate silently starve the smaller one — which is exactly the
    case this rule exists to catch.  ``centres`` is sorted, so each ``k`` costs
    one bisect.
    """
    if not centres:
        return []
    out = []
    for k in range(1, max_k + 1):
        target = v / k
        span = tol * target
        lo = bisect_left(centres, target - span * 2)
        hi = bisect_right(centres, target + span * 2)
        for c in centres[max(0, lo - 1):min(hi + 1, len(centres))]:
            if abs(v - k * c) <= tol * c:
                out.append((c, k))
    return out


def find_size_clusters(per_snapshot: list[list[tuple[float, float, float]]], n_snapshots: int,
                       params: DetectParams
                       ) -> tuple[list[SizeCluster], dict[float, list[float | None]]]:
    """Every candidate resting size on one side, scored.

    ``per_snapshot[i]`` is that snapshot's ``(volume, distance_in_steps)`` levels
    already restricted to the search radius.  A candidate scores the share of
    snapshots in which *some* level within the radius carried that size — where
    it stood does not matter, only that it stood.

    Returns ALL candidates with ``qualifies`` marking those that clear the
    thresholds, rather than only the survivors: the page's profile chart needs
    the rejected ones too (that is how a reader sees what the current cut-off
    drops), and scoring them twice was the single most expensive thing this
    module did — 29 s for a 20-instrument summary before this returned the lot.

    Also returns, per candidate, the price of its nearest-to-mid level in each
    snapshot.  That is the quote whose spread gets measured later, and this pass
    already has it in hand; re-deriving it afterwards meant scanning every level
    again for every pair and was the second-biggest cost in the module.
    """
    pooled = [v for snap in per_snapshot for v, _d, _p in snap
              if v >= params.min_cluster_volume]
    centres = volume_centres(pooled, params.volume_tol)
    if not centres:
        return [], {}

    hits: dict[float, int] = {c: 0 for c in centres}
    alone: dict[float, int] = {c: 0 for c in centres}
    dists: dict[float, list[float]] = {c: [] for c in centres}
    prices: dict[float, list[float | None]] = {c: [None] * n_snapshots for c in centres}
    for idx, snap in enumerate(per_snapshot):
        # Nearest-to-mid distance per candidate in THIS snapshot: a size can rest
        # on several levels at once, and the quote that matters is the one facing
        # the market.
        seen: dict[float, tuple[float, int, float]] = {}
        for v, d, price in snap:
            if v < params.min_cluster_volume:
                continue
            for c, k in _match_centres(centres, v, params.volume_tol):
                if c not in seen or d < seen[c][0]:
                    seen[c] = (d, k, price)
        for c, (d, k, price) in seen.items():
            hits[c] += 1
            dists[c].append(d)
            prices[c][idx] = price
            if k == 1:
                alone[c] += 1

    out = []
    for c in centres:
        presence = hits[c] / n_snapshots if n_snapshots else 0.0
        # Never seen in its own right → its "presence" is an inference from other
        # participants' levels, not an observation.  See MIN_ALONE_SHARE.
        stands_alone = bool(hits[c]) and alone[c] >= MIN_ALONE_SHARE * hits[c]
        out.append(SizeCluster(
            side="", volume=c, presence=presence,
            presence_alone=alone[c] / n_snapshots if n_snapshots else 0.0,
            qualifies=presence >= params.persistence_min and stands_alone,
            dist_steps=_median(dists[c]), dist_bps=None))
    return sorted(out, key=lambda x: -x.volume), prices


def match_by_size(bids: list[SizeCluster], asks: list[SizeCluster],
                  params: DetectParams) -> list[dict]:
    """Confirmed pairs: a bid cluster and an ask cluster of comparable SIZE.

    Distance is deliberately not a condition — see the module note.  Matching is
    greedy from the largest size down, and each cluster is used at most once, so
    one big resting quote cannot be paired with every small one opposite.
    """
    pairs = []
    used_ask: set[int] = set()
    for cb in sorted(bids, key=lambda x: -x.volume):
        best, best_err = None, None
        for i, ca in enumerate(asks):
            if i in used_ask:
                continue
            hi = max(cb.volume, ca.volume)
            if hi <= 0:
                continue
            err = abs(cb.volume - ca.volume) / hi
            if err > params.symmetry_tol:
                continue
            if best_err is None or err < best_err:
                best, best_err = i, err
        if best is None:
            continue
        ca = asks[best]
        used_ask.add(best)
        cb.matched = ca.matched = True
        pairs.append({
            "volume_bid": cb.volume,
            "volume_ask": ca.volume,
            "volume_two_sided": min(cb.volume, ca.volume),
            "size_mismatch": best_err,
            "presence_bid": cb.presence,
            "presence_ask": ca.presence,
            # Of that presence, how often the quote stood ALONE at its price
            # level.  A low share means the size is usually queued together with
            # an identical order, which is worth seeing next to the estimate.
            "alone_bid": cb.presence_alone,
            "alone_ask": ca.presence_alone,
            "dist_bid_steps": cb.dist_steps,
            "dist_ask_steps": ca.dist_steps,
        })
    return sorted(pairs, key=lambda p: -p["volume_two_sided"])


def _dedupe_stacks(pairs: list[dict], tol: float,
                   max_k: int = MAX_STACK_MULTIPLE) -> list[dict]:
    """Collapse pairs that describe the SAME price level at different multiples.

    The stack rule (see ``_match_centres``) deliberately lets a level of 2V count
    for a candidate of V, which is what makes a maker visible inside somebody
    else's level.  Its cost is the mirror case: when V and 2V both qualify on
    both sides, one level gets reported as two quoters and its size is counted
    twice in the total.  Observed on ETH: the ask 20 steps out read 30050 in 77%
    of snapshots and 15000 in the rest, and both were reported — a $86k total
    where at most $57k rests there.

    Two pairs are the same level when their sizes are a whole multiple apart AND
    they sit at the same distance.  The survivor is the one seen standing alone
    more often — the size actually observed in its own right rather than inferred
    from a multiple.  This understates the NUMBER of participants (two makers of
    V each read as one of 2V) and that is the intended direction: the book cannot
    tell them apart, and inventing a participant is worse than missing one.
    """
    def alone(p: dict) -> float:
        return (p["alone_bid"] + p["alone_ask"]) / 2.0

    def same_place(a: dict, b: dict) -> bool:
        for k in ("dist_bid_steps", "dist_ask_steps"):
            x, y = a.get(k), b.get(k)
            if x is None or y is None:
                return False
            if abs(x - y) > max(2.0, 0.25 * max(x, y)):
                return False
        return True

    kept: list[dict] = []
    for p in pairs:
        dup = None
        for q in kept:
            hi, lo = max(p["volume_two_sided"], q["volume_two_sided"]), \
                     min(p["volume_two_sided"], q["volume_two_sided"])
            if lo <= 0:
                continue
            if any(abs(hi - k * lo) <= tol * lo for k in range(2, max_k + 1)) and same_place(p, q):
                dup = q
                break
        if dup is None:
            kept.append(p)
        elif alone(p) > alone(dup):
            kept[kept.index(dup)] = p
    return kept


def _levels_within(snap: Snapshot, mid: float, step: float, side: str,
                   radius_steps: float) -> list[tuple[float, float, float]]:
    """``(volume, distance_in_steps, price)`` for one side, inside the radius."""
    out = []
    for lvl in snap.get("bids" if side == BID else "asks") or []:
        d = offset_steps(lvl["price"], mid, side, step)
        if 0 <= d <= radius_steps and lvl["size"] > 0:
            out.append((lvl["size"], d, lvl["price"]))
    return out


# ── the whole pipeline ───────────────────────────────────────────────────────

def analyze(snapshots: list[Snapshot], params: DetectParams = DEFAULTS,
            price_step: float | None = None,
            depth_cap: int = STORE_DEPTH) -> dict:
    """Full estimate for one instrument over one window.

    Returns the summary a human reads, the clusters behind it, the per-snapshot
    series the charts draw, and the size profile.  Snapshots with a one-sided
    (or empty) book are dropped up front — every metric here is relative to a mid
    that such a book does not have.
    """
    usable = []
    for s in snapshots:
        tob = top_of_book(s)
        if tob:
            usable.append((s, tob))
    n = len(usable)
    step = price_step or infer_price_step([s for s, _ in usable])

    # Bin width is display-only now (heat-map rows); the detector no longer bins.
    tob_steps = [round((ba - bb) / step) for _, (bb, ba, _) in usable]
    auto_bin = max(1, int(_median([t for t in tob_steps if t > 0]) or 1))
    bin_steps = params.bin_steps or auto_bin

    empty = {
        "n_snapshots": n, "price_step": step, "bin_steps": bin_steps,
        "enough_data": False, "spread_observed": _spread_stats([]),
        "spread_observed_abs": _spread_stats([]),
        "spread_mm": _spread_stats([]), "spread_mm_abs": _spread_stats([]),
        "mm_volume": None, "corridors": {},
        "clusters": [], "pairs": [], "series": [], "profile": [],
        "valid_match_share": 0.0, "radius_steps": None,
    }
    if n < params.min_snapshots:
        return empty

    # Pass 1 — per-snapshot metrics, levels inside the search radius, heat-map bins.
    bid_levels: list[list[tuple[float, float]]] = []
    ask_levels: list[list[tuple[float, float]]] = []
    series: list[dict] = []
    radii: list[float] = []
    corridor_vals: dict[float, dict[str, list[float]]] = {
        c: {"bid": [], "ask": [], "two_sided": [], "two_sided_usd": [], "trunc": []}
        for c in params.corridors
    }
    per_snap_bins: list[dict[tuple[str, int], float]] = []
    for snap, (bb, ba, mid) in usable:
        # The radius is a fraction of price, so one setting means the same thing
        # on a $30 and a $1400 instrument.
        radius_steps = mid * params.search_radius_pct / step
        radii.append(radius_steps)
        bid_levels.append(_levels_within(snap, mid, step, BID, radius_steps))
        ask_levels.append(_levels_within(snap, mid, step, ASK, radius_steps))
        per_snap_bins.append(snapshot_bins(snap, mid, step, bin_steps,
                                           params.max_offset_steps))
        depth = corridor_depth(snap, mid, params.corridors, depth_cap)
        for c, d in depth.items():
            corridor_vals[c]["bid"].append(d["bid"])
            corridor_vals[c]["ask"].append(d["ask"])
            corridor_vals[c]["two_sided"].append(d["two_sided"])
            corridor_vals[c]["two_sided_usd"].append(d["two_sided_usd"])
            corridor_vals[c]["trunc"].append(1.0 if d["truncated"] else 0.0)
        series.append({
            "ts": snap["ts"], "mid": mid,
            "spread_abs": ba - bb,
            "spread_steps": (ba - bb) / step,
            "spread_bps": (ba - bb) / mid * 1e4,
            # per-quoter spreads (mm0_bps, mm1_bps, …) are added in pass 3
        })

    # Pass 2 — persistent sizes per side, then pairing by size.
    bid_all, bid_prices = find_size_clusters(bid_levels, n, params)
    ask_all, ask_prices = find_size_clusters(ask_levels, n, params)
    for c in bid_all:
        c.side = BID
    for c in ask_all:
        c.side = ASK
    mid_med = _median([s["mid"] for s in series]) or 0.0
    for c in bid_all + ask_all:
        c.dist_bps = (c.dist_steps * step / mid_med * 1e4) if (c.dist_steps and mid_med) else None
    bid_clusters = [c for c in bid_all if c.qualifies]
    ask_clusters = [c for c in ask_all if c.qualifies]
    pairs = _dedupe_stacks(match_by_size(bid_clusters, ask_clusters, params),
                           params.volume_tol)

    # Pass 3 — the spread each confirmed quoter is showing, snapshot by snapshot.
    #
    # One book can hold SEVERAL market makers, and they are not interchangeable:
    # on AMD two distinct sizes rest at once.  Collapsing them into one median
    # would describe none of them, so every pair is measured separately and the
    # page lists them side by side.  Only the largest few get a per-snapshot
    # series (that is what the chart can show); the rest still get their summary.
    mm_abs: list[float] = []
    mm_bps: list[float] = []
    matched_snaps = 0
    for rank, pair in enumerate(pairs):
        p_abs, p_bps, notional, hits = [], [], [], 0
        track = rank < MAX_TRACKED_PAIRS
        pb_all = bid_prices.get(pair["volume_bid"]) or []
        pa_all = ask_prices.get(pair["volume_ask"]) or []
        for idx, (_snap, (_bb, _ba, mid)) in enumerate(usable):
            pb = pb_all[idx] if idx < len(pb_all) else None
            pa = pa_all[idx] if idx < len(pa_all) else None
            if pb is None or pa is None:
                if track:
                    series[idx][f"mm{rank}_bps"] = None
                continue
            sp = pa - pb
            p_abs.append(sp)
            p_bps.append(sp / mid * 1e4)
            # What the quote is worth at the price it is actually shown at — the
            # smaller of the two sides, since that is the size standing on both.
            # Per contract-unit: a venue multiplier (lot) is applied by the
            # caller, which is where the instrument's contract spec lives.
            notional.append(min(pair["volume_bid"] * pb, pair["volume_ask"] * pa))
            hits += 1
            if track:
                series[idx][f"mm{rank}_bps"] = sp / mid * 1e4
        pair["spread_abs"] = _spread_stats(p_abs)
        pair["spread_bps"] = _spread_stats(p_bps)
        pair["notional_usd"] = _median(notional)
        # Share of snapshots in which THIS quoter was standing on both sides at
        # once — the honest denominator for "it was there", per quoter.
        pair["match_share"] = hits / n if n else 0.0
        pair["tracked"] = track
        if rank == 0:                       # headline series = the largest quoter
            mm_abs, mm_bps = p_abs, p_bps
            matched_snaps = hits

    # Profile for the chart: every candidate size and how often it rested, above
    # and below the threshold, so the page can show what the current cut-off
    # keeps and what it drops.  Same objects the detector scored — no rescan.
    profile = [{
        "side": c.side, "volume": c.volume, "presence": c.presence,
        "dist_steps": c.dist_steps, "dist_bps": c.dist_bps,
    } for c in bid_all + ask_all]
    profile.sort(key=lambda p: (-p["presence"], -p["volume"]))

    two_sided_vals = [p["volume_two_sided"] for p in pairs]

    return {
        "n_snapshots": n,
        "price_step": step,
        "bin_steps": bin_steps,
        "radius_steps": _median(radii),
        "enough_data": True,
        "spread_observed": _spread_stats([s["spread_bps"] for s in series]),
        "spread_observed_abs": _spread_stats([s["spread_abs"] for s in series]),
        "spread_mm": _spread_stats(mm_bps),
        "spread_mm_abs": _spread_stats(mm_abs),
        # With more than one quoter the useful figures are the TOTAL resting on
        # both sides and the largest single quoter — a median across quoters of
        # different sizes describes none of them.
        "mm_volume": None if not pairs else {
            "total": sum(two_sided_vals),
            "largest": max(two_sided_vals),
            "median": _median(two_sided_vals),
            "p25": _pctl(two_sided_vals, 0.25),
            "p75": _pctl(two_sided_vals, 0.75),
            "n_pairs": len(pairs),
        },
        "corridors": {
            str(c): {
                "bid": _spread_stats(v["bid"]),
                "ask": _spread_stats(v["ask"]),
                "two_sided": _spread_stats(v["two_sided"]),
                "two_sided_usd": _spread_stats(v["two_sided_usd"]),
                "truncated_share": (sum(v["trunc"]) / n) if n else 0.0,
            } for c, v in corridor_vals.items()
        },
        "clusters": [asdict(c) for c in bid_clusters + ask_clusters],
        "pairs": pairs,
        "series": series,
        "profile": profile,
        "valid_match_share": matched_snaps / n if n else 0.0,
        "_bins": per_snap_bins,      # heat map input; not part of the JSON payload
    }


def build_heatmap(result: dict, max_cols: int = 240, max_bins: int = 12) -> dict:
    """Time × offset-bin × volume matrix for the heat map, downsampled in time.

    A trading day at a 5-second step is ~12 000 snapshots; sending one column per
    snapshot would be a multi-megabyte payload to draw a few hundred pixels.
    Columns are averaged over equal time slices, which is also the honest
    reduction for "how much volume typically rested here".

    Rows are capped at ``max_bins`` per side.  A thin book can put its outermost
    level hundreds of bins out (observed: 326 rows on HOOD), which squeezes the
    region where quoting actually happens into a couple of unreadable pixels —
    and it is that region the chart exists to show.  Twelve bins is already
    generous: at the default bin width (one top-of-book spread) that reaches
    ~1% away from the mid, well past anything a maker quotes.
    """
    series, bins = result.get("series") or [], result.get("_bins") or []
    if not series or not bins:
        return {"x": [], "y": [], "z": [], "bin_steps": result.get("bin_steps", 1)}

    n = len(series)
    cols = min(max_cols, n)
    edges = [round(i * n / cols) for i in range(cols + 1)]

    all_bins = sorted({b for snap in bins for (_s, b) in snap})
    if not all_bins:
        return {"x": [], "y": [], "z": [], "bin_steps": result.get("bin_steps", 1)}
    max_bin = min(max(all_bins), max_bins - 1)
    # Rows: asks above the mid (positive), bids below (negative) — the mid sits in
    # the middle of the axis, which is how a book reads on screen.
    rows = [(ASK, b) for b in range(max_bin, -1, -1)] + [(BID, b) for b in range(0, max_bin + 1)]

    z: list[list[float | None]] = []
    for side, b in rows:
        row: list[float | None] = []
        for i in range(cols):
            lo, hi = edges[i], edges[i + 1]
            if hi <= lo:
                row.append(None)
                continue
            vals = [bins[k].get((side, b), 0.0) for k in range(lo, hi)]
            avg = sum(vals) / len(vals)
            row.append(avg if avg > 0 else None)
        z.append(row)

    def _ts(i: int):
        t = series[i]["ts"]
        return t.isoformat() if isinstance(t, datetime) else t

    return {
        "x": [_ts(edges[i]) for i in range(cols)],
        "y": [(b if side == ASK else -b) for side, b in rows],
        "z": z,
        "bin_steps": result["bin_steps"],
        "max_bin_shown": max_bin,
        "bins_present": max(all_bins),      # so the UI can say the view is cropped
    }
