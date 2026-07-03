"""LLM relevance judgment + title translation + summary. Provider is swappable; Gemini is implemented here."""

from __future__ import annotations

import abc
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0

SYSTEM_PROMPT = """\
You are a news editing assistant for UBI Taiwan. You help internal colleagues keep track of news \
related to Universal Basic Income (UBI) / basic income / guaranteed income.

Rules:
1. Always respond in Taiwanese Traditional Chinese (台灣繁體中文). Use plain, matter-of-fact wording \
— no marketing tone, no exclamatory language.
2. Keep the summary to 2-3 sentences, focused on "what happened" and "how it relates to UBI / basic \
income / guaranteed income".
3. No hallucination: answer only based on the title and excerpt provided by the user. If information \
is insufficient, say so plainly rather than guessing at numbers or conclusions.
4. Judge whether this news item is genuinely related to UBI / universal basic income / guaranteed \
income, filtering out noise that only matched on keywords (e.g. articles purely about other welfare \
programs, stocks, or unrelated income topics).
5. Output pure JSON only — no markdown code fence (e.g. ```json), and no surrounding explanation text.

Output format (must conform to this JSON schema):
{
  "relevant": true or false,
  "title_zh": "title in Traditional Chinese",
  "summary_zh": "2-3 sentence summary in Traditional Chinese"
}
"""


def _build_user_prompt(title: str, raw_summary: str, source_name: str) -> str:
    clean_summary = re.sub(r"<[^>]+>", " ", raw_summary or "").strip()
    clean_summary = re.sub(r"\s+", " ", clean_summary)
    return (
        f"Source: {source_name}\n"
        f"Original title: {title}\n"
        f"Original excerpt: {clean_summary or '(no excerpt available)'}\n"
    )


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def safe_parse_json(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_strip_code_fence(text))
    except (json.JSONDecodeError, TypeError):
        logger.warning("LLM response could not be parsed as JSON: %r", text)
        return None

    if not isinstance(data, dict):
        return None
    if "relevant" not in data or "title_zh" not in data or "summary_zh" not in data:
        logger.warning("LLM response JSON is missing required fields: %r", data)
        return None
    return data


@dataclass
class SummaryResult:
    relevant: bool
    title_zh: str
    summary_zh: str


class BaseSummarizer(abc.ABC):
    @abc.abstractmethod
    def summarize(self, title: str, raw_summary: str, source_name: str) -> SummaryResult | None:
        """Returns None if the call or parsing failed; caller should skip this item without aborting the run."""


class GeminiSummarizer(BaseSummarizer):
    def __init__(self, api_key: str, model: str) -> None:
        from google import genai  # lazy import so a missing SDK doesn't break unrelated imports

        self._genai = genai
        self._types = __import__("google.genai.types", fromlist=["types"])
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def summarize(self, title: str, raw_summary: str, source_name: str) -> SummaryResult | None:
        prompt = _build_user_prompt(title, raw_summary, source_name)

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=prompt,
                    config=self._types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                    ),
                )
                data = safe_parse_json(response.text or "")
                if data is None:
                    return None
                return SummaryResult(
                    relevant=bool(data.get("relevant", False)),
                    title_zh=str(data.get("title_zh", "")),
                    summary_zh=str(data.get("summary_zh", "")),
                )
            except Exception:
                if attempt >= _MAX_RETRIES:
                    logger.warning("LLM call failed (retried %d times), skipping item: %s", attempt, title, exc_info=True)
                    return None
                backoff = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning("LLM call failed, retrying in %.1fs (attempt %d): %s", backoff, attempt, title)
                time.sleep(backoff)
        return None


def get_summarizer() -> BaseSummarizer:
    api_key = os.environ["GEMINI_API_KEY"]
    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
    return GeminiSummarizer(api_key=api_key, model=model)
