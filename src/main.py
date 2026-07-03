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
        seen_hashes_this_run.add(url_hash)
        candidates.append((url_hash, item))
        item.link = normalized

    logger.info("After dedup and time filtering: %d item(s) to judge", len(candidates))

    summarizer = summarize.get_summarizer()
    push_items: list[slack.PushItem] = []
    hash_by_url: dict[str, str] = {}

    for url_hash, item in candidates:
        result = summarizer.summarize(item.title, item.raw_summary, item.source_name)
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
                published=item.published,
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
