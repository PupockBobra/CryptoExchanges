"""
Auto-ingest of the СПБ Биржа funding CSVs from the @beststocks_neo channel.

The channel posts two documents per trading day — one for the US-stock perps,
one for the crypto indices — both named ``Итоговый фандинг DD-MM-YYYY.csv`` with
the columns the manual upload already understands.  This loop downloads them
into memory, parses them with ``app.spb.funding.parse_funding_csv`` and upserts,
so ``POST /api/spb/funding/upload`` stays as a fallback rather than the only way
in.

Why a *user* session and not a bot: the Bot API cannot read a third-party
channel the bot isn't an admin of, and the public web preview (``t.me/s/…``)
renders the document's name but exposes no download URL.  MTProto under a user
account is the only route.

⚠️ ``TELEGRAM_SESSION_PATH`` is full access to that Telegram account — anyone
holding the file reads its chats and writes as it.  Keep it out of git and out
of the image (bind-mount at runtime) and prefer a throwaway account.

One-time interactive login (asks for phone + the code Telegram sends):

    cd backend && .venv/bin/python -m app.spb.funding_tg login
    # in Docker: docker compose exec backend python -m app.spb.funding_tg login

A one-off pass from the CLI (useful for the first, long historical scan — it has
no HTTP timeout over it, unlike the refresh endpoint):

    .venv/bin/python -m app.spb.funding_tg ingest [days]

Everything is a no-op when ``TELEGRAM_API_ID`` / ``TELEGRAM_API_HASH`` are unset
or the session file is missing, so a deploy without credentials keeps working.
"""

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.db.timescale import get_spb_funding_latest_date, upsert_spb_funding
from app.spb.funding import parse_funding_csv

log = logging.getLogger(__name__)

# Documents we want.  The date lives in the name (``parse_funding_csv`` reads it
# from there); a trailing `` (1)`` — added when both of a day's files land in one
# download folder — is tolerated, as are the channel's other attachments being
# skipped outright.
_FILE_RE = re.compile(r"^Итоговый фандинг \d{2}-\d{2}-\d{4}.*\.csv$", re.IGNORECASE)

# Re-scan this many days back from the newest stored day: a file can be posted
# late, and re-ingesting a day is free (upsert on (date, ticker)).
_LOOKBACK_DAYS = 3
# How far back to scan when the table is empty.
_INITIAL_LOOKBACK_DAYS = 180

# Wall-clock cadence, like the sibling SPB loops: poll often, act hourly, so a
# host suspend is recovered on the next poll instead of sleeping through it.
_POLL_SEC = 300
_REFRESH_SEC = 3600
# Let the boot rush (ETL backfills, market loads) pass before connecting.
_STARTUP_DELAY_SEC = 120


def wants_file(name: str | None) -> bool:
    """True for the channel's daily funding CSVs, False for its other posts."""
    return bool(name and _FILE_RE.match(name.strip()))


def window_start(latest: date | None, today: date) -> date:
    """First day to scan: a few days behind what we have, or a long backfill
    when the table is empty."""
    if latest is None:
        return today - timedelta(days=_INITIAL_LOOKBACK_DAYS)
    return latest - timedelta(days=_LOOKBACK_DAYS)


def _configured() -> bool:
    return bool(settings.telegram_api_id and settings.telegram_api_hash)


def _document_name(message) -> str | None:
    """Attached document's filename, or None when the message has no document."""
    doc = getattr(message, "document", None)
    if doc is None:
        return None
    for attr in getattr(doc, "attributes", []):
        name = getattr(attr, "file_name", None)
        if name:
            return name
    return None


async def ingest_funding_from_telegram(days: int | None = None) -> dict:
    """
    Download and upsert every funding CSV posted since the scan window's start.

    ``days`` overrides the window (used by the CLI for the first historical
    pass).  Returns a report: files seen/accepted, rows written, per-file errors.
    """
    if not _configured():
        return {"ok": False, "reason": "TELEGRAM_API_ID/TELEGRAM_API_HASH not set"}

    session = Path(settings.telegram_session_path)
    if not session.exists():
        return {"ok": False, "reason": f"no session at {session} — run `python -m app.spb.funding_tg login`"}

    # Imported lazily: telethon pulls in crypto/network machinery that a deploy
    # without Telegram credentials has no reason to load.
    from telethon import TelegramClient

    if days is not None:
        start = date.today() - timedelta(days=days)
    else:
        start = window_start(await get_spb_funding_latest_date(), date.today())
    offset = datetime.combine(start, time.min, tzinfo=timezone.utc)

    rows: list[tuple] = []
    results: list[dict] = []
    seen = 0

    client = TelegramClient(str(session), settings.telegram_api_id, settings.telegram_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"ok": False, "reason": "session exists but is not authorized — re-run `login`"}

        # reverse=True walks forward in time from `offset`, so a long first pass
        # ingests oldest-first and a short one touches only the newest days.
        async for msg in client.iter_messages(
            settings.telegram_funding_channel, offset_date=offset, reverse=True
        ):
            name = _document_name(msg)
            if not wants_file(name):
                continue
            seen += 1
            data = await client.download_media(msg, file=bytes)
            parsed, error = parse_funding_csv(name, data.decode("utf-8-sig", errors="replace"))
            if error:
                results.append({"name": name, "ok": False, "error": error})
                continue
            rows.extend(parsed)
            results.append({"name": name, "ok": True, "rows": len(parsed), "date": str(parsed[0][0])})
    finally:
        await client.disconnect()

    saved = await upsert_spb_funding(rows)
    if saved:
        # The funding heatmap is TTL-cached — show what just landed.
        from app.api.cache import clear_cache
        clear_cache()

    accepted = sum(1 for r in results if r["ok"])
    return {
        "ok": True,
        "since": str(start),
        "files": seen,
        "accepted": accepted,
        "saved": saved,
        "results": results,
    }


async def spb_funding_tg_loop() -> None:
    """Hourly ingest pass; never raises, so a Telegram outage can't kill boot."""
    if not _configured():
        log.info("SPB funding TG: TELEGRAM_API_ID/HASH unset — auto-ingest disabled")
        return

    await asyncio.sleep(_STARTUP_DELAY_SEC)
    last_run = datetime.min.replace(tzinfo=timezone.utc)
    while True:
        now = datetime.now(timezone.utc)
        if (now - last_run).total_seconds() >= _REFRESH_SEC:
            last_run = now
            try:
                res = await ingest_funding_from_telegram()
                if not res.get("ok"):
                    log.warning("SPB funding TG: %s", res.get("reason"))
                else:
                    log.info(
                        "SPB funding TG: %d file(s) since %s → %d row(s) saved",
                        res["accepted"], res["since"], res["saved"],
                    )
            except Exception as e:                      # noqa: BLE001 — loop must survive
                log.warning("SPB funding TG pass failed: %r", e)
        await asyncio.sleep(_POLL_SEC)


def _login() -> None:
    """Interactive one-time login that writes the session file."""
    from telethon import TelegramClient

    if not _configured():
        raise SystemExit("set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first")
    session = Path(settings.telegram_session_path)
    session.parent.mkdir(parents=True, exist_ok=True)
    with TelegramClient(str(session), settings.telegram_api_id, settings.telegram_api_hash) as client:
        me = client.get_me()
        print(f"authorized as {me.username or me.first_name} (id {me.id})")
        print(f"session written to {session} — keep it secret, chmod 600")


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        _login()
    elif cmd == "ingest":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else None
        print(asyncio.run(ingest_funding_from_telegram(days)))
    else:
        raise SystemExit("usage: python -m app.spb.funding_tg login | ingest [days]")
