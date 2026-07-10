"""Load config/sources.yaml and fetch each RSS / Google News source."""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import feedparser
import requests
import trafilatura
import yaml

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15.0
_USER_AGENT = (
    "Mozilla/5.0 (compatible; UBITaiwanNewsBot/1.0; "
    "+https://github.com/ubi-taiwan)"
)

_GOOGLE_NEWS_TEMPLATE = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
_ARTICLE_TEXT_MAX_CHARS = 6000


@dataclass
class NewsItem:
    title: str
    link: str
    published: datetime | None
    source_name: str
    raw_summary: str


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _fetch_feed_text(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _parse_entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        struct_time = getattr(entry, key, None)
        if struct_time:
            return datetime.fromtimestamp(calendar.timegm(struct_time), tz=timezone.utc)
    return None


def _parse_feed(feed_text: str, source_name: str) -> list[NewsItem]:
    parsed = feedparser.parse(feed_text)
    items: list[NewsItem] = []
    for entry in parsed.entries:
        link = getattr(entry, "link", None)
        title = getattr(entry, "title", None)
        if not link or not title:
            continue
        items.append(
            NewsItem(
                title=title,
                link=link,
                published=_parse_entry_datetime(entry),
                source_name=source_name,
                raw_summary=getattr(entry, "summary", "") or getattr(entry, "description", ""),
            )
        )
    return items


def fetch_rss_source(name: str, url: str) -> list[NewsItem]:
    feed_text = _fetch_feed_text(url)
    return _parse_feed(feed_text, name)


def build_google_news_url(query: str, lang: str, geo: str, ceid: str) -> str:
    return _GOOGLE_NEWS_TEMPLATE.format(q=quote(query), hl=lang, gl=geo, ceid=ceid)


def fetch_google_news_source(name: str, query: str, lang: str, geo: str, ceid: str) -> list[NewsItem]:
    url = build_google_news_url(query, lang, geo, ceid)
    feed_text = _fetch_feed_text(url)
    return _parse_feed(feed_text, name)


@dataclass
class ArticleContent:
    text: str | None
    # The article page's own publish-date metadata, when trafilatura can find one. This is
    # often more trustworthy than an RSS feed's pubDate: Google News in particular sometimes
    # reports a recent crawl/republish date for an old article, which would otherwise slip
    # past the freshness filter as if it were new.
    published: datetime | None


def fetch_article_content(url: str) -> ArticleContent | None:
    """Best-effort full-text + true-publish-date fetch for one article. Returns None on any
    fetch failure (paywall, anti-bot block, network error) so the caller can fall back to the
    RSS excerpt/date instead."""
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException:
        logger.info("Failed to fetch article page for full-text extraction: %s", url)
        return None

    text = trafilatura.extract(resp.text)

    published = None
    try:
        metadata = trafilatura.extract_metadata(resp.text)
    except Exception:
        metadata = None
    if metadata and metadata.date:
        try:
            published = datetime.fromisoformat(metadata.date).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return ArticleContent(
        text=text[:_ARTICLE_TEXT_MAX_CHARS] if text else None,
        published=published,
    )


def collect_all_items(config_path: Path) -> list[NewsItem]:
    """Fetch every configured source in turn. A single source failure is logged and skipped."""
    config = load_config(config_path)
    all_items: list[NewsItem] = []

    for source in config.get("rss_sources", []) or []:
        name = source.get("name", "unnamed source")
        url = source.get("url", "")
        try:
            items = fetch_rss_source(name, url)
            logger.info("[source: %s] fetched %d item(s)", name, len(items))
            all_items.extend(items)
        except Exception:
            logger.warning("[source: %s] fetch failed, skipping this source", name, exc_info=True)

    for source in config.get("google_news", []) or []:
        name = source.get("name", "unnamed source")
        try:
            items = fetch_google_news_source(
                name,
                query=source.get("query", ""),
                lang=source.get("lang", "en-US"),
                geo=source.get("geo", "US"),
                ceid=source.get("ceid", "US:en"),
            )
            logger.info("[source: %s] fetched %d item(s)", name, len(items))
            all_items.extend(items)
        except Exception:
            logger.warning("[source: %s] fetch failed, skipping this source", name, exc_info=True)

    return all_items
