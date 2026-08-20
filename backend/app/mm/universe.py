"""
MM FORTS futures universe — resolved from ISS once per day.

For every MM group (ISS FORTS collection) this picks, per underlying
(ASSETCODE), the **front-month** contract (nearest expiry, still trading,
calendar spreads excluded) and attaches everything the feed / spread math needs:

  - ``secid``      — Finam streams it as ``<secid>@RTSX``;
  - ``step_ratio`` — STEPPRICE / MINSTEP, the rouble value of one price unit for
    one contract.  Used both to measure the 1 000 000 ₽ depth target and to
    convert the spread — denomination-agnostic (works for ₽, $ and points), so
    no lot / usdrub factors are needed;
  - ``currency``   — display symbol for the absolute-spread axis (₽ / $ / пт …);
  - liquidity gate — drop underlyings whose rouble open interest is below
    ``MM_MIN_OI_RUB`` so dead books aren't streamed.

Two ISS calls per rebuild: one per collection (front-month + currency), plus a
single FORTS market snapshot (STEPPRICE / MINSTEP / open interest for every
contract).  All blocking (ISS is sync) — run from a thread executor.
"""

import asyncio
import logging
import re
from datetime import date, datetime, timezone

from app.moex.fetcher import _ISS_BASE, _get, _make_session
from app.mm.config import (
    MM_COLLECTION,
    MM_EXTRA_ASSETS,
    MM_GROUP_IDS,
    MM_MIN_OI_RUB,
    quote_symbol,
)

log = logging.getLogger(__name__)

_SUFFIX_RE = re.compile(r"-\d+\.\d+$")     # strips "-3.26" from "Si-3.26"


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _forts_snapshot(session) -> dict[str, dict]:
    """SECID → {step_ratio, price, oi} for every current FORTS contract, from a
    single market snapshot (STEPPRICE/MINSTEP from the securities block, open
    interest + last price from the marketdata block)."""
    url = f"{_ISS_BASE}/engines/futures/markets/forts/securities.json"
    data = _get(session, url, {
        "securities.columns": "SECID,SHORTNAME,ASSETCODE,LASTTRADEDATE,MINSTEP,STEPPRICE,PREVSETTLEPRICE",
        "marketdata.columns": "SECID,OPENPOSITION,LAST",
    })
    out: dict[str, dict] = {}
    scols = data["securities"]["columns"]
    for row in data["securities"]["data"]:
        r = dict(zip(scols, row))
        minstep = _num(r.get("MINSTEP"))
        out[r["SECID"]] = {
            "step_ratio": (_num(r.get("STEPPRICE")) / minstep) if minstep > 0 else 0.0,
            "price":      _num(r.get("PREVSETTLEPRICE")),
            "oi":         0.0,
            "assetcode":  r.get("ASSETCODE"),
            "shortname":  r.get("SHORTNAME"),
            "lsttrade":   r.get("LASTTRADEDATE"),
        }
    md = data.get("marketdata", {})
    for row in md.get("data", []):
        r = dict(zip(md["columns"], row))
        e = out.get(r["SECID"])
        if not e:
            continue
        e["oi"] = _num(r.get("OPENPOSITION"))
        if _num(r.get("LAST")) > 0:
            e["price"] = _num(r.get("LAST"))
    return out


def _collection_front_months(session, collection: str) -> list[dict]:
    """Front-month contract per underlying in a collection: the still-trading
    single future with the nearest expiry.  Calendar spreads (TYPE
    ``futures_spread``) and expired/untraded contracts are excluded.

    ISS paginates this endpoint at ~100 rows and ignores oversized ``limit``
    (500→100), so we page with ``start`` until a short/empty page.  The shares
    collection is ~2500 rows (many expired series per underlying) — a single
    request silently truncated it and dropped underlyings past the first page
    (e.g. NASD/SPYF)."""
    url = f"{_ISS_BASE}/securitygroups/futures_forts/collections/{collection}/securities.json"
    today = date.today().isoformat()
    front: dict[str, dict] = {}
    start = 0
    while True:
        data = _get(session, url, {
            "iss.only":           "securities",
            "securities.columns": "SECID,SHORTNAME,ASSETCODE,LSTTRADE,TYPE,IS_TRADED,FACEUNIT,UNIT",
            "start":              start,
            "limit":              100,
        })
        page = data["securities"]["data"]
        if not page:
            break
        cols = data["securities"]["columns"]
        for row in page:
            r = dict(zip(cols, row))
            if r.get("TYPE") != "futures" or r.get("IS_TRADED") != 1:
                continue
            lst = r.get("LSTTRADE")
            if not lst or lst < today:
                continue
            ac = r.get("ASSETCODE")
            if not ac:
                continue
            cur = front.get(ac)
            if cur is None or lst < cur["LSTTRADE"]:
                front[ac] = r
        if len(page) < 100:
            break
        start += len(page)
    return list(front.values())


def _security_units(session, secid: str) -> dict:
    """FACEUNIT / UNIT of a single contract (the market snapshot has neither) —
    needed to label the absolute-spread axis."""
    url = f"{_ISS_BASE}/securities/{secid}.json"
    data = _get(session, url, {"iss.only": "description"})
    d = data["description"]
    vals = {dict(zip(d["columns"], row))["name"]: dict(zip(d["columns"], row))["value"]
            for row in d["data"]}
    return {"FACEUNIT": vals.get("FACEUNIT"), "UNIT": vals.get("UNIT")}


def _extra_front_months(session, snap: dict[str, dict], group_id: str) -> list[dict]:
    """Front-month rows for this group's ``MM_EXTRA_ASSETS`` underlyings, taken
    from the market snapshot instead of the (incomplete) ISS collection.  Shaped
    like ``_collection_front_months`` rows so both feed the same filter."""
    today = date.today().isoformat()
    front: dict[str, tuple[str, dict]] = {}
    for secid, meta in snap.items():
        ac = meta.get("assetcode")
        if not ac or MM_EXTRA_ASSETS.get(ac) != group_id:
            continue
        lst = meta.get("lsttrade")
        if not lst or lst < today:
            continue
        cur = front.get(ac)
        if cur is None or lst < cur[1]["lsttrade"]:
            front[ac] = (secid, meta)
    return [{
        "SECID":     secid,
        "SHORTNAME": meta.get("shortname"),
        "ASSETCODE": ac,
        "LSTTRADE":  meta["lsttrade"],
        **_security_units(session, secid),
    } for ac, (secid, meta) in front.items()]


def build_universe() -> list[dict]:
    """Rebuild the full MM universe from ISS (blocking).  Returns the flat list
    of instruments across all groups, liquidity-filtered."""
    out: list[dict] = []
    with _make_session() as session:
        snap = _forts_snapshot(session)
        for gid in MM_GROUP_IDS:
            kept = 0
            rows = _collection_front_months(session, MM_COLLECTION[gid])
            seen = {r["ASSETCODE"] for r in rows}
            rows += [r for r in _extra_front_months(session, snap, gid)
                     if r["ASSETCODE"] not in seen]     # ISS may add them later
            for r in rows:
                secid = r["SECID"]
                meta = snap.get(secid)
                if not meta or meta["step_ratio"] <= 0:
                    continue
                oi_rub = meta["oi"] * meta["price"] * meta["step_ratio"]
                if oi_rub < MM_MIN_OI_RUB:
                    continue
                short = r.get("SHORTNAME") or secid
                out.append({
                    "ticker":     r["ASSETCODE"],
                    "name":       _SUFFIX_RE.sub("", short) or r["ASSETCODE"],
                    "group":      gid,
                    "secid":      secid,
                    "step_ratio": meta["step_ratio"],
                    "currency":   quote_symbol(r.get("FACEUNIT"), r.get("UNIT")),
                    "expiry":     r.get("LSTTRADE"),
                })
                kept += 1
            log.info("MM universe: group=%s kept %d underlyings", gid, kept)
    return out


# ── daily-refreshed in-memory cache ───────────────────────────────────────────
_universe: list[dict] = []
_universe_day: date | None = None
_lock = asyncio.Lock()


async def ensure_universe() -> list[dict]:
    """Return the cached universe, rebuilding once per UTC day.  A failed rebuild
    keeps the last good universe (empty only before the first success)."""
    global _universe, _universe_day
    today = datetime.now(timezone.utc).date()
    if _universe and _universe_day == today:
        return _universe
    async with _lock:
        if _universe and _universe_day == today:      # won the race
            return _universe
        loop = asyncio.get_event_loop()
        try:
            built = await loop.run_in_executor(None, build_universe)
        except Exception as exc:  # noqa: BLE001 — keep last good universe
            log.warning("MM universe rebuild failed: %s", exc)
            return _universe
        if built:
            _universe, _universe_day = built, today
    return _universe


def get_universe() -> list[dict]:
    """Cached universe (may be empty before the first ensure_universe())."""
    return _universe


def get_group(group_id: str) -> list[dict]:
    return [i for i in _universe if i["group"] == group_id]
