from __future__ import annotations

import json

import pytest

from core.apify_instagram import (
    normalize_apify_items,
    normalize_competitor_handles,
    validate_actor_id,
    validate_instagram_handle,
    validate_posts_per_profile,
)
from core.content_agent_team import _validate_agent_payload
from core.errors import ValidationAppError


def test_instagram_handle_validation() -> None:
    assert validate_instagram_handle("@Artixcore") == "artixcore"
    with pytest.raises(ValidationAppError):
        validate_instagram_handle("bad handle")
    with pytest.raises(ValidationAppError):
        validate_instagram_handle("bad..handle")


def test_competitor_validation_rejects_owned_handle() -> None:
    with pytest.raises(ValidationAppError):
        normalize_competitor_handles(
            ["Artixcore"],
            own_handle="artixcore",
        )


def test_posts_per_profile_is_bounded() -> None:
    assert validate_posts_per_profile("25") == 25
    with pytest.raises(ValidationAppError):
        validate_posts_per_profile(0)
    with pytest.raises(ValidationAppError):
        validate_posts_per_profile(101)


def test_actor_id_requires_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "APIFY_ALLOWED_ACTOR_IDS",
        "apify/instagram-scraper",
    )
    assert validate_actor_id("apify/instagram-scraper") == (
        "apify/instagram-scraper"
    )
    with pytest.raises(ValidationAppError):
        validate_actor_id("untrusted/actor")


def test_apify_normalization_filters_and_bounds_records() -> None:
    items = [
        {
            "id": "post-1",
            "ownerUsername": "artixcore",
            "caption": "Build carefully #AI #Security",
            "url": "https://www.instagram.com/p/example/",
            "displayUrl": "https://cdn.example.com/image.jpg",
            "timestamp": "2026-08-01T10:00:00Z",
            "likesCount": 100,
            "commentsCount": 10,
            "sharesCount": 5,
            "videoViewCount": 1000,
            "type": "Video",
        },
        {
            "id": "ignored",
            "ownerUsername": "not-requested",
            "caption": "Ignore me",
        },
    ]
    records = normalize_apify_items(
        items,
        requested_handles=["artixcore"],
    )
    assert len(records) == 1
    record = records[0]
    assert record.handle == "artixcore"
    assert record.likes_count == 100
    assert record.comments_count == 10
    assert record.shares_count == 5
    assert record.engagement_rate == 11.5
    assert record.hashtags == ("AI", "Security")
    assert record.raw_digest


def test_agent_payload_is_json_and_bounded() -> None:
    raw = json.dumps(
        {
            "summary": "Completed",
            "ideas": [
                {
                    "title": "Safe AI",
                    "evidence": "Post metrics",
                }
            ],
            "unsafe key!": "normalized",
        }
    )
    payload = _validate_agent_payload("ideator", raw)
    assert payload["summary"] == "Completed"
    assert payload["ideas"][0]["title"] == "Safe AI"
    assert payload["unsafe_key_"] == "normalized"
