"""Validation boundaries for the allowlisted Apify Instagram connector."""

from __future__ import annotations

import os
import re

from core.errors import ConfigurationError, ValidationAppError
from core.validation import normalize_text

_ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?$")
_INSTAGRAM_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_MAX_POSTS_PER_PROFILE = 100


def validate_instagram_handle(value: object, *, field: str = "Instagram handle") -> str:
    handle = normalize_text(
        value,
        field=field,
        min_length=1,
        max_length=31,
        allow_newlines=False,
    ).lstrip("@").lower()
    if not _INSTAGRAM_HANDLE_RE.fullmatch(handle):
        raise ValidationAppError(
            f"{field} may contain only letters, numbers, periods, and underscores."
        )
    if handle.startswith(".") or handle.endswith(".") or ".." in handle:
        raise ValidationAppError(f"{field} contains an invalid period placement.")
    return handle


def _normalize_actor_id(value: object, *, field: str = "Apify actor ID") -> str:
    actor_id = normalize_text(
        value,
        field=field,
        min_length=3,
        max_length=255,
        allow_newlines=False,
    )
    if not _ACTOR_ID_RE.fullmatch(actor_id):
        raise ValidationAppError(
            f"{field} must be an actor name or owner/actor pair using safe characters."
        )
    return actor_id


def allowed_actor_ids() -> frozenset[str]:
    raw = os.getenv("APIFY_ALLOWED_ACTOR_IDS", "apify/instagram-scraper")
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if not values or len(values) > 10:
        raise ConfigurationError(
            "APIFY_ALLOWED_ACTOR_IDS must contain between 1 and 10 actor IDs."
        )
    return frozenset(
        _normalize_actor_id(value, field="Allowed Apify actor ID") for value in values
    )


def validate_actor_id(value: object) -> str:
    actor_id = _normalize_actor_id(value)
    if actor_id not in allowed_actor_ids():
        raise ValidationAppError(
            "The selected Apify actor is not allowlisted for ContentPilot."
        )
    return actor_id


def validate_posts_per_profile(value: object) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationAppError("Posts per profile must be an integer.") from exc
    if not 1 <= result <= _MAX_POSTS_PER_PROFILE:
        raise ValidationAppError(
            f"Posts per profile must be between 1 and {_MAX_POSTS_PER_PROFILE}."
        )
    return result


def normalize_competitor_handles(
    values: list[object] | tuple[object, ...],
    *,
    own_handle: str,
) -> list[str]:
    own = validate_instagram_handle(
        own_handle,
        field="Your Instagram handle",
    )
    normalized: list[str] = []
    seen = {own.casefold()}
    for value in values:
        if value is None or not str(value).strip():
            continue
        handle = validate_instagram_handle(value, field="Competitor handle")
        key = handle.casefold()
        if key in seen:
            if key == own.casefold():
                raise ValidationAppError(
                    "Your own Instagram handle cannot be a competitor."
                )
            continue
        seen.add(key)
        normalized.append(handle)
    if len(normalized) > 5:
        raise ValidationAppError("You can configure at most 5 competitor handles.")
    return normalized


def parse_competitor_text(value: object, *, own_handle: str) -> list[str]:
    text = normalize_text(
        value,
        field="Competitor handles",
        min_length=0,
        max_length=1_000,
    )
    parts = [item.strip() for item in re.split(r"[,\n;]+", text) if item.strip()]
    return normalize_competitor_handles(parts, own_handle=own_handle)
