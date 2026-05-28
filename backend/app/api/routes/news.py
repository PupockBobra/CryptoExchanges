"""
CoinDesk news feed — fetches RSS, caches in Redis for 15 minutes.
"""
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx
from fastapi import APIRouter, Query

from app.redis_client import get_redis

log = logging.getLogger(__name__)
router = APIRouter()

COINDESK_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"
CACHE_KEY    = "news:coindesk"
CACHE_TTL    = 900   # 15 minutes

_NS = {
    "dc":      "http://purl.org/dc/elements/1.1/",
    "media":   "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom":    "http://www.w3.org/2005/Atom",
}


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _parse_rss(xml_text: str) -> list[dict]:
    root    = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []

    articles: list[dict] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        url   = (item.findtext("link")  or "").strip()
        if not title or not url:
            continue

        # Description — strip HTML, cap at 300 chars
        description = _strip_html(item.findtext("description") or "")
        if len(description) > 300:
            description = description[:297] + "…"

        # Publication date
        published_at = ""
        raw_date = item.findtext("pubDate") or ""
        if raw_date:
            try:
                published_at = parsedate_to_datetime(raw_date).isoformat()
            except Exception:
                published_at = raw_date

        # Author via dc:creator
        author = (item.findtext(f"{{{_NS['dc']}}}creator") or "").strip()

        # Categories
        categories = [
            c.text.strip()
            for c in item.findall("category")
            if c.text and c.text.strip()
        ]

        # Image: media:content → media:thumbnail → enclosure
        image_url = ""
        for tag in (
            f"{{{_NS['media']}}}content",
            f"{{{_NS['media']}}}thumbnail",
        ):
            el = item.find(tag)
            if el is not None:
                image_url = el.get("url", "")
                if image_url:
                    break
        if not image_url:
            enc = item.find("enclosure")
            if enc is not None and "image" in (enc.get("type") or ""):
                image_url = enc.get("url", "")

        guid       = item.findtext("guid") or url
        article_id = hashlib.md5(guid.encode()).hexdigest()[:12]

        articles.append({
            "id":           article_id,
            "title":        title,
            "url":          url,
            "description":  description,
            "published_at": published_at,
            "author":       author,
            "categories":   categories,
            "image_url":    image_url,
            "source":       "CoinDesk",
        })

    return articles


async def _fetch_coindesk() -> list[dict]:
    r = await get_redis()

    cached = await r.get(CACHE_KEY)
    if cached:
        return json.loads(cached)

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(
            COINDESK_RSS,
            headers={"User-Agent": "Mozilla/5.0 CryptoTracker/1.0"},
        )
        resp.raise_for_status()

    articles = _parse_rss(resp.text)
    await r.setex(CACHE_KEY, CACHE_TTL, json.dumps(articles))
    return articles


@router.get("")
async def get_news(limit: int = Query(50, ge=1, le=100)):
    """Return latest CoinDesk articles, cached 15 min."""
    try:
        articles = await _fetch_coindesk()
        return {
            "articles": articles[:limit],
            "total":    len(articles),
            "source":   "CoinDesk",
        }
    except Exception as exc:
        log.warning("News fetch failed: %s", exc)
        return {"articles": [], "total": 0, "source": "CoinDesk", "error": str(exc)}


@router.post("/refresh")
async def refresh_news():
    """Force-clear cache and re-fetch from CoinDesk."""
    r = await get_redis()
    await r.delete(CACHE_KEY)
    articles = await _fetch_coindesk()
    return {"status": "refreshed", "count": len(articles)}
