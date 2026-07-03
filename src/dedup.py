"""URL normalization, Google News redirect resolution, and seen.json read/write/prune."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote

import requests

logger = logging.getLogger(__name__)

_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_NAMES = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ocid"}

_HTTP_TIMEOUT = 10.0
_MAX_RATE_LIMIT_RETRIES = 2
_RATE_LIMIT_BACKOFF_SECONDS = 3.0
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

_PROTOBUF_PREFIX = bytes([0x08, 0x13, 0x22]).decode("latin1")
_PROTOBUF_SUFFIX = bytes([0xD2, 0x01, 0x00]).decode("latin1")


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    if lowered in _TRACKING_PARAM_NAMES:
        return True
    return any(lowered.startswith(prefix) for prefix in _TRACKING_PARAM_PREFIXES)


def _strip_tracking_params(url: str) -> str:
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking_param(k)]
    new_query = urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, strip tracking params, drop trailing slash."""
    url = _strip_tracking_params(url.strip())
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _extract_base64_id(google_news_url: str) -> str | None:
    path = urlsplit(google_news_url).path.split("/")
    if len(path) < 2 or path[-2] not in ("articles", "read"):
        return None
    return path[-1]


def _decode_base64_payload(base64_str: str) -> str | None:
    """Try offline decoding first (no HTTP). Returns the URL directly if it's embedded in the payload."""
    try:
        decoded_bytes = base64.urlsafe_b64decode(base64_str + "==")
        decoded_str = decoded_bytes.decode("latin1")
    except Exception:
        return None

    if decoded_str.startswith(_PROTOBUF_PREFIX):
        decoded_str = decoded_str[len(_PROTOBUF_PREFIX):]
    if decoded_str.endswith(_PROTOBUF_SUFFIX):
        decoded_str = decoded_str[: -len(_PROTOBUF_SUFFIX)]

    if not decoded_str:
        return None

    length = bytearray(decoded_str, "latin1")[0]
    if length >= 0x80:
        decoded_str = decoded_str[2 : length + 1]
    else:
        decoded_str = decoded_str[1 : length + 1]

    # An "AU_yqL" prefix means this isn't the article URL yet, but an internal ID
    # that requires fetching a signature from Google to decode further.
    if decoded_str.startswith("AU_yqL"):
        return None
    return decoded_str


def _get_with_rate_limit_retry(session: requests.Session, *args, **kwargs) -> requests.Response:
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        resp = session.get(*args, timeout=_HTTP_TIMEOUT, **kwargs)
        if resp.status_code != 429 or attempt == _MAX_RATE_LIMIT_RETRIES:
            return resp
        time.sleep(_RATE_LIMIT_BACKOFF_SECONDS * (attempt + 1))
    return resp


def _post_with_rate_limit_retry(session: requests.Session, *args, **kwargs) -> requests.Response:
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        resp = session.post(*args, timeout=_HTTP_TIMEOUT, **kwargs)
        if resp.status_code != 429 or attempt == _MAX_RATE_LIMIT_RETRIES:
            return resp
        time.sleep(_RATE_LIMIT_BACKOFF_SECONDS * (attempt + 1))
    return resp


def _fetch_decoding_params(session: requests.Session, base64_str: str) -> tuple[str, str] | None:
    resp = _get_with_rate_limit_retry(session, f"https://news.google.com/articles/{base64_str}")
    resp.raise_for_status()
    sig_match = re.search(r'data-n-a-sg="([^"]+)"', resp.text)
    ts_match = re.search(r'data-n-a-ts="([^"]+)"', resp.text)
    if not sig_match or not ts_match:
        return None
    return sig_match.group(1), ts_match.group(1)


def _fetch_batchexecute_url(session: requests.Session, base64_str: str, signature: str, timestamp: str) -> str | None:
    payload = (
        '["Fbv4je","[\\"garturlreq\\",[[\\"X\\",\\"X\\",[\\"X\\",\\"X\\"],null,null,1,1,'
        '\\"US:en\\",null,1,null,null,null,null,null,0,1],\\"X\\",\\"X\\",1,[1,1,1],1,1,'
        f'null,0,0,null,0],\\"{base64_str}\\",{timestamp},\\"{signature}\\"]",null,"generic"]'
    )
    body = "f.req=" + quote(f"[[{payload}]]", safe="")
    resp = _post_with_rate_limit_retry(
        session,
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data=body,
    )
    resp.raise_for_status()
    header = '\\"garturlres\\",\\"'
    footer = '\\",'
    text = resp.text
    if header not in text:
        return None
    start = text.split(header, 1)[1]
    if footer not in start:
        return None
    return start.split(footer, 1)[0]


def resolve_google_news_url(url: str) -> str:
    """Resolve a Google News RSS redirect link into the original article URL.

    Falls back to the original URL on any failure so the pipeline never breaks.
    """
    if "news.google.com" not in urlsplit(url).netloc:
        return url

    base64_str = _extract_base64_id(url)
    if not base64_str:
        return url

    try:
        direct = _decode_base64_payload(base64_str)
        if direct:
            return direct

        with requests.Session() as session:
            session.headers.update({"User-Agent": _USER_AGENT})
            params = _fetch_decoding_params(session, base64_str)
            if not params:
                logger.warning("Could not fetch Google News decoding params, falling back to original link: %s", url)
                return url
            signature, timestamp = params
            resolved = _fetch_batchexecute_url(session, base64_str, signature, timestamp)
            if not resolved:
                logger.warning("Google News redirect resolution failed, falling back to original link: %s", url)
                return url
            return resolved
    except Exception:
        logger.warning("Google News redirect resolution raised an exception, falling back to original link: %s", url, exc_info=True)
        return url


def compute_hash(normalized_url: str) -> str:
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


def load_seen(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read seen.json, treating as empty set: %s", path, exc_info=True)
        return {}


def save_seen(path: Path, seen: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def prune_seen(seen: dict[str, str], max_age_days: int = 60) -> dict[str, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    pruned: dict[str, str] = {}
    for url_hash, iso_ts in seen.items():
        try:
            ts = datetime.fromisoformat(iso_ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts >= cutoff:
            pruned[url_hash] = iso_ts
    return pruned
