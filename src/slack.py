"""Assemble Slack Block Kit messages and send them (batches when over the block limit)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

_TAIPEI_TZ = ZoneInfo("Asia/Taipei")

_MAX_BLOCKS_PER_MESSAGE = 50
_BLOCKS_PER_ITEM = 4  # section(title) + context(source/date) + section(summary) + divider
_ITEMS_PER_BATCH = _MAX_BLOCKS_PER_MESSAGE // _BLOCKS_PER_ITEM

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0
_HTTP_TIMEOUT = 10.0


class SlackSendError(Exception):
    pass


@dataclass
class PushItem:
    title_zh: str
    url: str
    source_name: str
    published: datetime | None
    summary_zh: str


def _format_date(published: datetime | None) -> str:
    if published is None:
        return "日期不明"
    return published.astimezone(_TAIPEI_TZ).strftime("%Y-%m-%d")


def _build_item_blocks(item: PushItem) -> list[dict[str, Any]]:
    date_str = _format_date(item.published)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{item.url}|{item.title_zh}>*",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{item.source_name} ・ {date_str}",
                }
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": item.summary_zh,
            },
        },
        {"type": "divider"},
    ]


def _chunk_items(items: list[PushItem], batch_size: int = _ITEMS_PER_BATCH) -> list[list[PushItem]]:
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def _post_with_retry(webhook_url: str, payload: dict[str, Any]) -> None:
    last_error: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            return
        except Exception as exc:
            last_error = exc
            if attempt >= _MAX_RETRIES:
                break
            backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            logger.warning("Slack send failed, retrying in %.1fs (attempt %d)", backoff, attempt)
            time.sleep(backoff)
    raise SlackSendError(f"Slack message send failed: {last_error}") from last_error


def post_news_items(webhook_url: str, items: list[PushItem]) -> list[PushItem]:
    """Send items in batches, respecting Slack's 50-block-per-payload limit.

    Best-effort delivery: a failed batch doesn't stop other batches from being
    attempted. Returns the items that were actually sent successfully, so the
    caller can persist only those to seen.json and decide whether to exit with
    a non-zero code if any batch ultimately failed.
    """
    succeeded: list[PushItem] = []
    for batch in _chunk_items(items):
        blocks: list[dict[str, Any]] = []
        for item in batch:
            blocks.extend(_build_item_blocks(item))
        try:
            _post_with_retry(webhook_url, {"blocks": blocks})
            succeeded.extend(batch)
        except SlackSendError:
            logger.error("Slack batch send ultimately failed, skipping this batch of %d item(s)", len(batch), exc_info=True)
    return succeeded


def post_no_news_message(webhook_url: str) -> None:
    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "今天沒有新的 UBI 相關消息。",
                },
            }
        ]
    }
    _post_with_retry(webhook_url, payload)
