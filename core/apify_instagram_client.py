"""Hardened network client for the allowlisted Apify Instagram actor."""

from __future__ import annotations

import json
import os
import re

import httpx

from core.apify_instagram_normalize import normalize_apify_items
from core.apify_instagram_types import InstagramPostRecord
from core.apify_instagram_validation import (
    validate_actor_id,
    validate_instagram_handle,
    validate_posts_per_profile,
)
from core.errors import ConfigurationError, ExternalAPIError, ValidationAppError

_APIFY_BASE_URL = "https://api.apify.com/v2"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,256}$")
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_MAX_PROFILES = 6
_MAX_POSTS_PER_PROFILE = 100


class ApifyInstagramClient:
    """Run an allowlisted Apify actor against a bounded set of Instagram profiles."""

    def __init__(self, token: str | None = None):
        self.token = (token or os.getenv("APIFY_API_TOKEN", "")).strip()

    def _validated_token(self) -> str:
        if not self.token:
            raise ConfigurationError(
                "APIFY_API_TOKEN is required to sync Instagram data.",
                user_action=(
                    "Add the Apify API token to the deployment secret manager or .env file. "
                    "Never paste it into the dashboard."
                ),
            )
        if not _TOKEN_RE.fullmatch(self.token):
            raise ConfigurationError(
                "APIFY_API_TOKEN has an invalid format.",
                user_action="Replace it with a valid Apify API token.",
            )
        return self.token

    def fetch_posts(
        self,
        *,
        handles: list[str],
        actor_id: str,
        posts_per_profile: int,
    ) -> list[InstagramPostRecord]:
        if not handles or len(handles) > _MAX_PROFILES:
            raise ValidationAppError(
                "Instagram sync requires between 1 and 6 profiles."
            )
        safe_handles = [validate_instagram_handle(handle) for handle in handles]
        if len({handle.casefold() for handle in safe_handles}) != len(safe_handles):
            raise ValidationAppError("Instagram sync profile handles must be unique.")
        safe_actor = validate_actor_id(actor_id)
        safe_limit = validate_posts_per_profile(posts_per_profile)
        token = self._validated_token()

        actor_path = safe_actor.replace("/", "~")
        endpoint = (
            f"{_APIFY_BASE_URL}/acts/{actor_path}/run-sync-get-dataset-items"
        )
        payload = {
            "directUrls": [
                f"https://www.instagram.com/{handle}/" for handle in safe_handles
            ],
            "resultsType": "posts",
            "resultsLimit": min(
                safe_limit * len(safe_handles),
                _MAX_PROFILES * _MAX_POSTS_PER_PROFILE,
            ),
            "addParentData": False,
            "skipPinnedPosts": True,
        }

        try:
            with httpx.Client(
                timeout=httpx.Timeout(150.0, connect=10.0),
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "Artixcore-ContentPilot/1.0",
                },
            ) as client:
                with client.stream("POST", endpoint, json=payload) as response:
                    status_code = response.status_code
                    if status_code in {401, 403}:
                        raise ConfigurationError(
                            "Apify rejected the configured API token or actor access.",
                            user_action=(
                                "Verify APIFY_API_TOKEN and actor permissions in Apify."
                            ),
                        )
                    if status_code == 429:
                        raise ExternalAPIError(
                            "Apify rate limit reached.",
                            service="apify",
                            reason="The Apify API returned HTTP 429.",
                            retryable=True,
                        )
                    if status_code >= 500:
                        raise ExternalAPIError(
                            "Apify is temporarily unavailable.",
                            service="apify",
                            reason=f"The Apify API returned HTTP {status_code}.",
                            retryable=True,
                        )
                    if not response.is_success:
                        raise ExternalAPIError(
                            "Apify Instagram sync failed.",
                            service="apify",
                            reason=f"The Apify API returned HTTP {status_code}.",
                            retryable=False,
                        )

                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_RESPONSE_BYTES:
                            raise ExternalAPIError(
                                "Apify returned more data than ContentPilot can safely process.",
                                service="apify",
                                reason="Response exceeded the 10 MB safety limit.",
                                retryable=False,
                            )
                    response_content = bytes(content)
        except (ConfigurationError, ExternalAPIError):
            raise
        except httpx.TimeoutException as exc:
            raise ExternalAPIError(
                "Apify Instagram sync timed out.",
                service="apify",
                reason="The Apify actor did not finish within the configured timeout.",
                original_exception=exc,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ExternalAPIError(
                "Apify Instagram sync could not connect.",
                service="apify",
                reason=f"Network client error: {type(exc).__name__}",
                original_exception=exc,
                retryable=True,
            ) from exc

        try:
            data = json.loads(response_content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExternalAPIError(
                "Apify returned an invalid JSON response.",
                service="apify",
                reason="The actor response was not valid UTF-8 JSON.",
                original_exception=exc,
                retryable=True,
            ) from exc
        if not isinstance(data, list):
            raise ExternalAPIError(
                "Apify returned an unexpected response shape.",
                service="apify",
                reason="Expected a JSON list of Instagram post records.",
                retryable=False,
            )
        return normalize_apify_items(data, requested_handles=safe_handles)
