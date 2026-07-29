"""Shared safeguards for outbound HTTPS calls."""

from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx

from core.errors import ExternalAPIError, ValidationAppError
from core.validation import validate_http_url


def normalize_allowed_hosts(values: str | list[str] | set[str]) -> set[str]:
    raw_values = values.split(",") if isinstance(values, str) else list(values)
    hosts = {str(value).strip().lower().rstrip(".") for value in raw_values if str(value).strip()}
    for host in hosts:
        if len(host) > 253 or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for character in host):
            raise ValidationAppError("Outbound host allowlist contains an invalid hostname.")
    return hosts


def require_public_dns(hostname: str, port: int) -> set[str]:
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ExternalAPIError(
            "Outbound hostname could not be resolved.", service=hostname, retryable=True
        ) from exc
    addresses = {entry[4][0] for entry in results}
    if not addresses:
        raise ExternalAPIError(
            "Outbound hostname has no usable address.", service=hostname, retryable=True
        )
    for raw_address in addresses:
        address = ipaddress.ip_address(raw_address)
        if any(
            (
                address.is_private,
                address.is_loopback,
                address.is_link_local,
                address.is_multicast,
                address.is_reserved,
                address.is_unspecified,
            )
        ):
            raise ValidationAppError("Outbound hostname resolved to a private or reserved address.")
    return addresses


def validate_outbound_https(
    value: object,
    *,
    field: str,
    allowed_hosts: str | list[str] | set[str],
) -> str:
    url = validate_http_url(value, field=field, required=True, allow_private=False)
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValidationAppError(f"{field} must use HTTPS.")
    if parsed.username or parsed.password:
        raise ValidationAppError(f"{field} cannot contain embedded credentials.")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    allowlist = normalize_allowed_hosts(allowed_hosts)
    if not allowlist or hostname not in allowlist:
        raise ValidationAppError(f"{field} hostname is not present in the deployment allowlist.")
    require_public_dns(hostname, parsed.port or 443)
    return url


def request_json_limited(
    method: str,
    url: str,
    *,
    allowed_hosts: str | list[str] | set[str],
    data: dict | None = None,
    headers: dict[str, str] | None = None,
    auth: httpx.Auth | tuple[str, str] | None = None,
    timeout_seconds: float = 15.0,
    max_response_bytes: int = 256 * 1024,
) -> tuple[int, dict]:
    safe_url = validate_outbound_https(
        url, field="Outbound endpoint", allowed_hosts=allowed_hosts
    )
    timeout = httpx.Timeout(
        min(max(float(timeout_seconds), 1.0), 60.0),
        connect=min(max(float(timeout_seconds) / 2.0, 1.0), 15.0),
    )
    body = bytearray()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream(
                method.upper(),
                safe_url,
                data=data,
                headers=headers,
                auth=auth,
            ) as response:
                status_code = response.status_code
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise ExternalAPIError(
                            "Outbound response exceeded the configured size limit.",
                            service=urlsplit(safe_url).hostname or "external_service",
                            retryable=False,
                        )
    except ExternalAPIError:
        raise
    except httpx.TimeoutException as exc:
        raise ExternalAPIError(
            "Outbound request timed out.",
            service=urlsplit(safe_url).hostname or "external_service",
            retryable=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise ExternalAPIError(
            "Outbound request failed.",
            service=urlsplit(safe_url).hostname or "external_service",
            retryable=True,
        ) from exc
    try:
        parsed = json.loads(bytes(body).decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalAPIError(
            "Outbound service returned an invalid JSON response.",
            service=urlsplit(safe_url).hostname or "external_service",
            retryable=False,
        ) from exc
    if not isinstance(parsed, dict):
        raise ExternalAPIError(
            "Outbound service returned an unexpected response shape.",
            service=urlsplit(safe_url).hostname or "external_service",
            retryable=False,
        )
    return status_code, parsed
