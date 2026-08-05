"""Bounded normalization of untrusted Apify Instagram response records."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from core.apify_instagram_types import InstagramPostRecord
from core.apify_instagram_validation import validate_instagram_handle
from core.errors import ValidationAppError

_MAX_PROFILES = 6
_MAX_POSTS_PER_PROFILE = 100


def _bounded_non_negative_int(value: Any) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return min(max(result, 0), 2_147_483_647)


def _clean_external_text(value: Any, *, maximum: int) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    return text.strip()[:maximum]


def _optional_text(value: Any, *, maximum: int) -> str | None:
    text = _clean_external_text(value, maximum=maximum)
    return text or None


def _safe_https_url(
    value: Any,
    *,
    maximum: int,
    allowed_domain: str | None = None,
) -> str | None:
    text = _optional_text(value, maximum=maximum)
    if not text:
        return None
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    hostname = parsed.hostname.rstrip(".").lower()
    if allowed_domain and not (
        hostname == allowed_domain or hostname.endswith(f".{allowed_domain}")
    ):
        return None
    return text


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_hashtags(item: dict[str, Any], caption: str) -> tuple[str, ...]:
    raw = item.get("hashtags")
    values: list[str] = []
    if isinstance(raw, list):
        values.extend(str(part) for part in raw)
    elif isinstance(raw, str):
        values.extend(re.split(r"[,\s]+", raw))
    values.extend(re.findall(r"#([\w.]{1,64})", caption, flags=re.UNICODE))

    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = value.strip().lstrip("#")[:64]
        if not tag or any(ch.isspace() for ch in tag):
            continue
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(tag)
        if len(unique) >= 30:
            break
    return tuple(unique)


def _derive_handle(item: dict[str, Any], fallback_handles: list[str]) -> str | None:
    for key in ("ownerUsername", "username", "ownerUserName", "profileName"):
        value = item.get(key)
        if value:
            try:
                return validate_instagram_handle(value)
            except ValidationAppError:
                pass
    for key in ("inputUrl", "url", "profileUrl"):
        value = str(item.get(key) or "")
        match = re.search(r"instagram\.com/([A-Za-z0-9._]{1,30})", value)
        if match:
            try:
                return validate_instagram_handle(match.group(1))
            except ValidationAppError:
                pass
    if len(fallback_handles) == 1:
        return fallback_handles[0]
    return None


def normalize_apify_items(
    items: list[Any],
    *,
    requested_handles: list[str],
) -> list[InstagramPostRecord]:
    records: list[InstagramPostRecord] = []
    requested = [validate_instagram_handle(handle) for handle in requested_handles]
    allowed = {handle.casefold() for handle in requested}

    for raw in items[: _MAX_PROFILES * _MAX_POSTS_PER_PROFILE]:
        if not isinstance(raw, dict):
            continue
        handle = _derive_handle(raw, requested)
        if not handle or handle.casefold() not in allowed:
            continue

        caption = _clean_external_text(
            raw.get("caption") or raw.get("text") or "",
            maximum=20_000,
        )
        external_source = (
            raw.get("id")
            or raw.get("shortCode")
            or raw.get("shortcode")
            or raw.get("url")
            or raw.get("displayUrl")
        )
        canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        external_id = _clean_external_text(external_source or digest, maximum=255) or digest

        likes = _bounded_non_negative_int(raw.get("likesCount", raw.get("likes")))
        comments = _bounded_non_negative_int(
            raw.get("commentsCount", raw.get("comments"))
        )
        shares = _bounded_non_negative_int(raw.get("sharesCount", raw.get("shares")))
        views = _bounded_non_negative_int(
            raw.get(
                "videoViewCount",
                raw.get("videoPlayCount", raw.get("viewsCount")),
            )
        )
        interactions = likes + comments + shares
        engagement_rate = round((interactions / views) * 100, 4) if views > 0 else 0.0
        engagement_rate = min(max(engagement_rate, 0.0), 100_000.0)

        content_type = str(
            raw.get("type") or raw.get("productType") or "post"
        ).strip().lower()
        if content_type not in {
            "post",
            "image",
            "video",
            "sidecar",
            "carousel",
            "reel",
        }:
            content_type = "post"

        metadata = {
            "owner_full_name": _optional_text(raw.get("ownerFullName"), maximum=255),
            "location_name": _optional_text(raw.get("locationName"), maximum=255),
            "is_sponsored": bool(raw.get("isSponsored", False)),
            "music_info": _optional_text(raw.get("musicInfo"), maximum=1_000),
        }
        metadata = {key: value for key, value in metadata.items() if value is not None}

        records.append(
            InstagramPostRecord(
                handle=handle,
                external_id=external_id,
                content_type=content_type,
                caption=caption,
                permalink=_safe_https_url(
                    raw.get("url"),
                    maximum=1_024,
                    allowed_domain="instagram.com",
                ),
                thumbnail_url=_safe_https_url(
                    raw.get("displayUrl") or raw.get("thumbnailUrl"),
                    maximum=2_048,
                ),
                published_at=_parse_datetime(
                    raw.get("timestamp") or raw.get("takenAtIso") or raw.get("takenAt")
                ),
                likes_count=likes,
                comments_count=comments,
                shares_count=shares,
                views_count=views,
                engagement_rate=engagement_rate,
                hashtags=_extract_hashtags(raw, caption),
                raw_digest=digest,
                source_metadata=metadata,
            )
        )
    return records
