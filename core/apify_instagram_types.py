"""Typed normalized records for Instagram intelligence ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class InstagramPostRecord:
    """A bounded, normalized Instagram post record."""

    handle: str
    external_id: str
    content_type: str
    caption: str
    permalink: str | None
    thumbnail_url: str | None
    published_at: datetime | None
    likes_count: int
    comments_count: int
    shares_count: int
    views_count: int
    engagement_rate: float
    hashtags: tuple[str, ...]
    raw_digest: str
    source_metadata: dict[str, Any]
