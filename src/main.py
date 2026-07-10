"""Main pipeline: fetch sources -> dedup -> relevance judgment & summarize -> push to Slack -> update state."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dedup
import slack
import sources
import summarize

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "sources.yaml"
SEEN_PATH = REPO_ROOT / "state" / "seen.json"

SEEN_MAX_AGE_DAYS = 60
DEFAULT_LOOKBACK_DAYS = 3

logger = logging.getLogger("ubi_news")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Env var %s=%r is not a valid integer, falling back to default %d", name, value, default)
        return default


def run() -> int:
    _setup_logging()

    webhook_url = os.environ["SLACK_WEBHOOK_URL"]
    lookback_days = _env_int("LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)
    post_when_empty = _env_bool("POST_WHEN_EMPTY", False)

    raw_items = sources.collect_all_items(CONFIG_PATH)
    logger.info("Fetch complete: %d item(s) (before dedup)", len(raw_items))

    seen = dedup.load_seen(SEEN_PATH)
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    candidates: list[tuple[str, sources.NewsItem]] = []
    seen_hashes_this_run: set[str] = set()

    for item in raw_items:
        if item.published is not None and item.published < cutoff:
            continue
        # Conservative policy for items without a publish date: skip them outright,
        # to avoid mistaking an old article for fresh news.
        if item.published is None:
            logger.info("Skipping item with no publish date: %s", item.title)
            continue

        resolved_link = dedup.resolve_google_news_url(item.link)
        normalized = dedup.normalize_url(resolved_link)
        url_hash = dedup.compute_hash(normalized)

        if url_hash in seen or url_hash in seen_hashes_this_run:
            continue

        # Safety net for when Google News redirect resolution fails (e.g. rate limited):
        # the same story picked up from another source would otherwise get a different
        # hash and be pushed twice. Match on title instead in that case, preferring
        # whichever copy has an actual resolved article URL.
        duplicate_of = None
        for existing_index, (existing_hash, existing_item) in enumerate(candidates):
            if dedup.is_likely_same_story(item.title, existing_item.title):
                duplicate_of = existing_index
                break

        if duplicate_of is not None:
            existing_hash, existing_item = candidates[duplicate_of]
            if dedup.is_unresolved_google_news_link(existing_item.link) and not dedup.is_unresolved_google_news_link(normalized):
                seen_hashes_this_run.discard(existing_hash)
                item.link = normalized
                seen_hashes_this_run.add(url_hash)
                candidates[duplicate_of] = (url_hash, item)
            else:
                logger.info("Skipping duplicate (same story as another candidate this run): %s", item.title)
            continue

        seen_hashes_this_run.add(url_hash)
        item.link = normalized
        candidates.append((url_hash, item))

    logger.info("After dedup and time filtering: %d item(s) to judge", len(candidates))

    summarizer = summarize.get_summarizer()
    push_items: list[slack.PushItem] = []
    hash_by_url: dict[str, str] = {}

    for url_hash, item in candidates:
        article = sources.fetch_article_content(item.link)
        article_text = article.text if article else None
        content = article_text if article_text and len(article_text) > len(item.raw_summary) else item.raw_summary

        # The article page's own date metadata (when available) overrides the RSS pubDate for
        # freshness: Google News occasionally reports a recent crawl date for an old article.
        true_published = article.published if article and article.published else item.published
        if true_published < cutoff:
            logger.info(
                "Skipping stale article (page date %s predates lookback window, RSS said %s): %s",
                true_published.date(), item.published.date() if item.published else None, item.title,
            )
            continue

        result = summarizer.summarize(item.title, content, item.source_name)
        if result is None:
            continue
        if not result.relevant:
            logger.info("Judged not relevant, skipping: %s", item.title)
            continue
        push_items.append(
            slack.PushItem(
                title_zh=result.title_zh,
                url=item.link,
                source_name=item.source_name,
                published=true_published,
                summary_zh=result.summary_zh,
            )
        )
        hash_by_url[item.link] = url_hash

    logger.info("After relevance judgment: %d item(s) ready to push", len(push_items))

    exit_code = 0

    if push_items:
        succeeded = slack.post_news_items(webhook_url, push_items)
        logger.info("Successfully pushed %d / %d item(s)", len(succeeded), len(push_items))
        if len(succeeded) < len(push_items):
            exit_code = 1

        now_iso = datetime.now(timezone.utc).isoformat()
        for item in succeeded:
            url_hash = hash_by_url[item.url]
            seen[url_hash] = now_iso
    elif post_when_empty:
        try:
            slack.post_no_news_message(webhook_url)
            logger.info("No news today; posted the placeholder message")
        except slack.SlackSendError:
            logger.error("Failed to post the 'no news today' message", exc_info=True)
            exit_code = 1
    else:
        logger.info("No news today, and POST_WHEN_EMPTY is false; not posting")

    seen = dedup.prune_seen(seen, max_age_days=SEEN_MAX_AGE_DAYS)
    dedup.save_seen(SEEN_PATH, seen)

    return exit_code


if __name__ == "__main__":
    sys.exit(run())
