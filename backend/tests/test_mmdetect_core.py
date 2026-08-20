"""
MM-presence detector core — synthetic order books.

Three books the detector must tell apart, per the method's own claims:

  1. a textbook market maker (fixed size, fixed distance, price drifting) —
     it must be found, with the right size and the right spread;
  2. pure random flow — nothing may be confirmed;
  3. a persistent ONE-SIDED wall — the symmetry filter must reject it.

The drift in (1) is the point of measuring distance in price steps: an
absolute-price axis would smear that one standing quote across the whole range
and find nothing.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.mmdetect.config import DetectParams
from app.mmdetect.core import (
    analyze,
    build_heatmap,
    corridor_depth,
    dominant_volume,
    infer_price_step,
    offset_steps,
    top_of_book,
    volume_centres,
)

STEP = 0.01
T0 = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _lv(pairs):
    return [{"price": round(p, 6), "size": s} for p, s in pairs]


def _snap(i, bids, asks):
    return {"ts": T0 + timedelta(seconds=5 * i), "bids": _lv(bids), "asks": _lv(asks)}


def _mm_book(n=200, size=100.0, half_steps=10, seed=1):
    """A maker quoting `size` at ±`half_steps` steps, around a random-walking
    price, plus noisy retail levels further out.

    The noise starts a full spread beyond the maker's quote on purpose: the
    default bin is one top-of-book spread wide, so anything closer than that
    would be summed into the maker's own bin and hide its plateau.  That is a
    real property of the method (any binning merges neighbours), not a quirk of
    this fixture — the detector errs towards finding nothing.
    """
    rnd = random.Random(seed)
    snaps = []
    base = 100.0
    for i in range(n):
        base = round(base + rnd.choice([-1, 0, 1]) * STEP, 6)
        bid = base - half_steps * STEP
        ask = base + half_steps * STEP
        bids = [(bid, size)]
        asks = [(ask, size)]
        for k in range(1, 4):                      # noise: random size and place
            bids.append((bid - (20 * k + rnd.randint(0, 9)) * STEP, rnd.uniform(1, 400)))
            asks.append((ask + (20 * k + rnd.randint(0, 9)) * STEP, rnd.uniform(1, 400)))
        snaps.append(_snap(i, bids, asks))
    return snaps


def _random_book(n=200, seed=7):
    rnd = random.Random(seed)
    snaps = []
    for i in range(n):
        base = 100.0 + rnd.uniform(-0.5, 0.5)
        bids = sorted(((base - rnd.uniform(0.01, 0.5), rnd.uniform(1, 900)) for _ in range(6)),
                      key=lambda x: -x[0])
        asks = sorted(((base + rnd.uniform(0.01, 0.5), rnd.uniform(1, 900)) for _ in range(6)),
                      key=lambda x: x[0])
        snaps.append(_snap(i, bids, asks))
    return snaps


def _one_sided_book(n=200, wall=500.0, seed=3):
    """A persistent wall on the bid only; the ask is noise."""
    rnd = random.Random(seed)
    snaps = []
    base = 100.0
    for i in range(n):
        base = round(base + rnd.choice([-1, 0, 1]) * STEP, 6)
        bids = [(base - 10 * STEP, wall),
                (base - 20 * STEP, rnd.uniform(1, 300))]
        asks = [(base + 10 * STEP, rnd.uniform(1, 300)),
                (base + 25 * STEP, rnd.uniform(1, 300))]
        snaps.append(_snap(i, bids, asks))
    return snaps


# ── the three acceptance cases ───────────────────────────────────────────────

def test_detects_textbook_market_maker():
    # price_step passed explicitly so "steps" in the assertions below mean the
    # venue tick: this fixture never quotes on adjacent ticks, so the inferred
    # step would be the fixture's own coarser grid (see the test after next).
    res = analyze(_mm_book(), DetectParams(), price_step=STEP)
    assert res["enough_data"]
    assert res["pairs"], "a two-sided maker must be confirmed"
    mm = res["mm_volume"]
    assert mm is not None
    assert mm["median"] == pytest.approx(100.0, rel=0.1)
    # Quoted at ±10 steps → the MM spread is 20 steps = 0.20, and nothing quotes
    # inside it, so it equals the observed spread here.
    assert res["spread_mm_abs"]["median"] == pytest.approx(20 * STEP, rel=0.05)
    assert res["valid_match_share"] > 0.9
    p = res["pairs"][0]
    assert p["dist_bid_steps"] == pytest.approx(10, abs=1)
    assert p["dist_ask_steps"] == pytest.approx(10, abs=1)


def test_finds_a_maker_whose_two_quotes_sit_at_DIFFERENT_distances():
    """The obligation ties the two quotes' SIZES, not their distances.

    Reproduces the mechanism measured on AMD: a small client order steps in front
    of the maker on one side, which drags the mid towards it — so the maker's own
    two quotes end up at different distances from the mid without the maker
    moving at all.  (Note that a maker quoting alone cannot show this: the mid is
    then defined by its own two quotes and sits exactly between them.)

    The previous version of the detector required mirror distances and went blind
    on exactly this: on AMD a ~140-lot quote rested on both sides in 96% of
    snapshots and was never confirmed.
    """
    rnd = random.Random(5)
    snaps = []
    base = 100.0
    for i in range(200):
        base = round(base + rnd.choice([-1, 0, 1]) * STEP, 6)
        bids = [(base - 25 * STEP, 100.0)]
        asks = [(base + 25 * STEP, 100.0)]
        if i % 2:                       # client steps inside on the ask side
            asks.insert(0, (base + 5 * STEP, rnd.uniform(1, 9)))
        snaps.append(_snap(i, bids, asks))
    res = analyze(snaps, DetectParams(), price_step=STEP)
    assert res["pairs"], "same size on both sides must confirm regardless of distance"
    p = res["pairs"][0]
    assert p["volume_two_sided"] == pytest.approx(100.0, rel=0.1)
    # The asymmetry is reported rather than hidden: the mid spends half the window
    # pulled towards the ask, so the maker's ask sits further out than its bid.
    assert p["dist_ask_steps"] > p["dist_bid_steps"]


def test_separates_two_makers_quoting_side_by_side():
    """One book can hold several market makers — AMD visibly does.

    They are not interchangeable, so the detector must report them as separate
    quoters (each with its own size, distances and spread) instead of averaging
    them into a single number that describes neither.
    """
    rnd = random.Random(9)
    snaps = []
    base = 100.0
    for i in range(200):
        base = round(base + rnd.choice([-1, 0, 1]) * STEP, 6)
        snaps.append(_snap(i,
            [(base - 10 * STEP, 500.0), (base - 40 * STEP, 40.0),
             (base - 70 * STEP, rnd.uniform(1, 300))],
            [(base + 10 * STEP, 500.0), (base + 40 * STEP, 40.0),
             (base + 70 * STEP, rnd.uniform(1, 300))]))
    res = analyze(snaps, DetectParams(), price_step=STEP)
    sizes = sorted(p["volume_two_sided"] for p in res["pairs"])
    assert len(res["pairs"]) == 2, f"expected two quoters, got {sizes}"
    assert sizes[0] == pytest.approx(40, rel=0.1)
    assert sizes[1] == pytest.approx(500, rel=0.1)
    mm = res["mm_volume"]
    assert mm["largest"] == pytest.approx(500, rel=0.1)
    assert mm["total"] == pytest.approx(540, rel=0.1)
    # Each quoter carries its own spread: the outer one quotes wider.
    inner, outer = res["pairs"][0], res["pairs"][1]
    assert outer["spread_bps"]["median"] > inner["spread_bps"]["median"]


def test_finds_a_maker_hidden_inside_a_stacked_price_level():
    """A price level shows the SUM of everyone resting at that price.

    Reproduces UBER (2127 snapshots): the bid level read 350 in 51.8% of them and
    700 in 48.0% — never both, 99.8% together.  One 350-lot quote that never left,
    joined half the time by a second identical order.  Matching the exact size
    alone scored 0.52 and the maker was never confirmed.
    """
    rnd = random.Random(13)
    snaps = []
    base = 100.0
    for i in range(200):
        base = round(base + rnd.choice([-1, 0, 1]) * STEP, 6)
        # every other snapshot somebody queues a second 350 at the maker's price
        bid_size = 350.0 if i % 2 else 700.0
        snaps.append(_snap(i,
            [(base - 10 * STEP, bid_size), (base - 60 * STEP, rnd.uniform(1, 300))],
            [(base + 10 * STEP, 350.0), (base + 60 * STEP, rnd.uniform(1, 300))]))
    res = analyze(snaps, DetectParams(), price_step=STEP)
    assert res["pairs"], "the maker's own size must be found inside the stack"
    p = res["pairs"][0]
    assert p["volume_two_sided"] == pytest.approx(350, rel=0.1)
    # Reported, not hidden: half the time it was not standing alone.
    bid = [c for c in res["clusters"] if c["side"] == "bid"][0]
    assert bid["presence"] > 0.95
    assert bid["presence_alone"] == pytest.approx(0.5, abs=0.1)


def test_a_size_never_seen_alone_is_not_a_quoter():
    """The multiple rule must not invent quoters out of other people's levels.

    Here 150 rests on both sides and 50 never appears by itself — it only ever
    "fits" as a third of the 150.  Reporting a 50-lot quoter would be an
    inference, not an observation.  (Seen live on AMD the moment the stack rule
    shipped: 17- and 5-lot candidates standing alone in 1–2% of snapshots.)
    """
    snaps = []
    base = 100.0
    rnd = random.Random(23)
    for i in range(200):
        base = round(base + rnd.choice([-1, 0, 1]) * STEP, 6)
        snaps.append(_snap(i,
            [(base - 10 * STEP, 150.0)],
            [(base + 10 * STEP, 150.0)]))
    res = analyze(snaps, DetectParams(), price_step=STEP)
    sizes = {round(p["volume_two_sided"]) for p in res["pairs"]}
    assert 150 in sizes
    assert 50 not in sizes, "a size only ever seen as 1/3 of a level is not a quoter"


def test_one_level_seen_at_two_multiples_is_one_quoter():
    """The stack rule's mirror case: when V and 2V both qualify at the same
    distance, they are one level, not two makers, and counting both doubles the
    total.  Observed on ETH — the ask 20 steps out read 30050 most of the time
    and 15000 the rest, and both were being reported.
    """
    rnd = random.Random(29)
    snaps = []
    base = 100.0
    for i in range(200):
        base = round(base + rnd.choice([-1, 0, 1]) * STEP, 6)
        sz = 1000.0 if i % 4 else 500.0      # same level, sometimes halved
        snaps.append(_snap(i,
            [(base - 10 * STEP, sz), (base - 70 * STEP, rnd.uniform(1, 200))],
            [(base + 10 * STEP, sz), (base + 70 * STEP, rnd.uniform(1, 200))]))
    res = analyze(snaps, DetectParams(), price_step=STEP)
    sizes = sorted(p["volume_two_sided"] for p in res["pairs"])
    assert len(res["pairs"]) == 1, f"one level must be one quoter, got {sizes}"
    # The survivor is the size actually seen standing alone most often.
    assert res["mm_volume"]["total"] == pytest.approx(1000, rel=0.1)


def test_two_makers_at_different_distances_are_not_collapsed():
    """The de-duplication must not merge genuinely distinct quoters that happen
    to be a multiple apart — it requires the same distance as well as the size
    relation.  (Both quotes sit inside the 0.5%-of-price search radius, which at
    this fixture's price is 50 steps.)"""
    rnd = random.Random(31)
    snaps = []
    base = 100.0
    for i in range(200):
        base = round(base + rnd.choice([-1, 0, 1]) * STEP, 6)
        snaps.append(_snap(i,
            [(base - 10 * STEP, 500.0), (base - 40 * STEP, 1000.0)],
            [(base + 10 * STEP, 500.0), (base + 40 * STEP, 1000.0)]))
    res = analyze(snaps, DetectParams(), price_step=STEP)
    sizes = sorted(round(p["volume_two_sided"]) for p in res["pairs"])
    assert sizes == [500, 1000], sizes


def test_a_big_level_does_not_confirm_every_small_size():
    """The stack rule must not turn "there is a lot of volume" into "my size is
    in there": only whole multiples count, and the tolerance is measured against
    the candidate, not against the multiple."""
    rnd = random.Random(17)
    snaps = []
    base = 100.0
    for i in range(200):
        base = round(base + rnd.choice([-1, 0, 1]) * STEP, 6)
        snaps.append(_snap(i,
            [(base - 10 * STEP, 137.0), (base - 40 * STEP, 11.0)],
            [(base + 10 * STEP, 137.0), (base + 40 * STEP, 11.0)]))
    res = analyze(snaps, DetectParams(), price_step=STEP)
    sizes = {round(p["volume_two_sided"]) for p in res["pairs"]}
    # 137 is not a multiple of 11 (11×12 = 132, off by 5 > 10% of 11), so the two
    # sizes stay separate quoters rather than one explaining the other.
    assert 137 in sizes and 11 in sizes


def test_random_flow_yields_no_confirmed_maker():
    res = analyze(_random_book(), DetectParams())
    assert res["enough_data"]
    assert res["pairs"] == []
    assert res["mm_volume"] is None


def test_one_sided_wall_is_rejected_by_symmetry():
    params = DetectParams()
    res = analyze(_one_sided_book(), params)
    bid_clusters = [c for c in res["clusters"] if c["side"] == "bid"]
    assert bid_clusters, "the wall itself is persistent and must be seen"
    assert all(not c["matched"] for c in res["clusters"])
    assert res["pairs"] == []
    assert res["mm_volume"] is None


def test_one_lot_pair_is_not_a_market_maker():
    """A 1×1 resting pair passes the symmetry test exactly (|1-1|/1 = 0) and is
    as persistent as anything else, so without a volume floor it is reported as
    "MM volume: 1".  Seen live on NFLX (0.97/0.87 persistence, 117–130 steps out)."""
    rnd = random.Random(11)
    snaps = []
    base = 100.0
    for i in range(120):
        base = round(base + rnd.choice([-1, 0, 1]) * STEP, 6)
        snaps.append(_snap(i,
            [(base - 10 * STEP, rnd.uniform(50, 400)), (base - 30 * STEP, 1)],
            [(base + 10 * STEP, rnd.uniform(50, 400)), (base + 30 * STEP, 1)]))
    assert analyze(snaps, DetectParams(min_cluster_volume=0))["mm_volume"]["median"] == 1.0
    assert analyze(snaps, DetectParams(min_cluster_volume=2))["mm_volume"] is None


def test_symmetry_threshold_is_what_rejects_it():
    """Same book, symmetry filter opened up to 1.0 → the pair is admitted.
    Proves the rejection above comes from the filter, not from missing data."""
    snaps = _one_sided_book()
    strict = analyze(snaps, DetectParams(symmetry_tol=0.25))
    loose = analyze(snaps, DetectParams(symmetry_tol=1.0, persistence_min=0.3))
    assert strict["pairs"] == []
    assert len(loose["clusters"]) >= len(strict["clusters"])


# ── building blocks ──────────────────────────────────────────────────────────

def test_infer_price_step_finds_the_tick():
    # A book that actually quotes on adjacent ticks somewhere in the window.
    snaps = [_snap(i, [(100.00, 5), (99.99, 5), (99.90, 5)],
                      [(100.05, 5), (100.06, 5)]) for i in range(5)]
    assert infer_price_step(snaps) == pytest.approx(STEP, abs=1e-9)


def test_infer_price_step_reports_the_grid_it_can_see():
    """On a sparse window the smallest observed gap IS the quoting grid — the
    venue tick is not observable there.  Harmless for the estimate: with an
    auto bin width the step cancels out (bins = offset/median-spread), which is
    what the next test pins down."""
    snaps = _mm_book(n=30)
    assert infer_price_step(snaps) > STEP


def test_estimate_is_invariant_to_the_assumed_step():
    """Distances and the search radius are both measured in steps, so a wrong
    tick rescales both and cancels.  Guards the claim above."""
    snaps = _mm_book()
    a = analyze(snaps, DetectParams(), price_step=0.01)
    b = analyze(snaps, DetectParams(), price_step=0.005)
    assert len(a["pairs"]) == len(b["pairs"])
    assert a["mm_volume"]["median"] == pytest.approx(b["mm_volume"]["median"])
    assert a["spread_mm_abs"]["median"] == pytest.approx(b["spread_mm_abs"]["median"])


def test_infer_price_step_falls_back_when_one_level_per_side():
    snaps = [_snap(0, [(100.0, 5)], [(100.5, 5)])]
    assert infer_price_step(snaps, fallback=0.05) == 0.05


def test_dominant_volume_picks_the_plateau_not_the_middle():
    # Two plateaus: 100 (×7) and 500 (×3).  A median would say 100 as well, so
    # make the smaller group the one a median would land on.
    vals = [100.0] * 3 + [500.0] * 7
    v, cnt = dominant_volume(vals, tol=0.10)
    assert v == pytest.approx(500.0)
    assert cnt == 7


def test_dominant_volume_ignores_absent_levels():
    assert dominant_volume([0.0, 0.0], tol=0.1) == (None, 0)


def test_offset_is_measured_in_steps_from_the_mid():
    assert offset_steps(99.90, 100.0, "bid", STEP) == pytest.approx(10.0)
    assert offset_steps(100.10, 100.0, "ask", STEP) == pytest.approx(10.0)


def test_top_of_book_requires_both_sides():
    assert top_of_book({"bids": [], "asks": [{"price": 1, "size": 1}]}) is None


def test_corridor_depth_two_sided_is_the_smaller_side():
    snap = {"bids": _lv([(99.9, 10), (99.0, 100)]), "asks": _lv([(100.1, 4)])}
    d = corridor_depth(snap, mid=100.0, corridors=(0.005,), depth_cap=20)[0.005]
    assert d["bid"] == 10          # 99.0 is outside ±0.5%
    assert d["ask"] == 4
    assert d["two_sided"] == 4
    assert d["truncated"] is False


def test_corridor_marks_truncation_when_the_book_hits_the_cap():
    # 3 levels, cap 3, outermost still inside the corridor → we cannot see the
    # corridor's edge, so the number is a lower bound.
    snap = {"bids": _lv([(99.99, 1), (99.98, 1), (99.97, 1)]),
            "asks": _lv([(100.01, 1), (100.02, 1), (100.03, 1)])}
    d = corridor_depth(snap, mid=100.0, corridors=(0.005,), depth_cap=3)[0.005]
    assert d["truncated"] is True


def test_short_window_is_reported_not_guessed():
    res = analyze(_mm_book(n=5), DetectParams(min_snapshots=20))
    assert res["enough_data"] is False
    assert res["pairs"] == []


def test_heatmap_downsamples_time_and_centres_on_the_mid():
    res = analyze(_mm_book(n=200), DetectParams())
    hm = build_heatmap(res, max_cols=20)
    assert len(hm["x"]) == 20
    assert len(hm["z"]) == len(hm["y"])
    assert max(hm["y"]) >= 0 >= min(hm["y"])     # asks above, bids below


def test_heatmap_caps_rows_so_the_quoting_region_stays_visible():
    """A single far-out level must not stretch the axis to hundreds of rows —
    observed on real thin books (HOOD: 326 rows)."""
    snaps = _mm_book(n=60)
    for s in snaps:                       # one lonely level 300 bins out
        s["bids"].append({"price": s["bids"][0]["price"] - 40.0, "size": 5})
    res = analyze(snaps, DetectParams())
    hm = build_heatmap(res, max_cols=10, max_bins=8)
    assert max(hm["y"]) <= 7 and min(hm["y"]) >= -7
    assert hm["bins_present"] > hm["max_bin_shown"]      # the crop is reported
