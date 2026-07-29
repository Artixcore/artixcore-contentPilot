"""Central input validation helpers for application and integration boundaries."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePath
from urllib.parse import urlsplit

from core.errors import FileUploadError, ValidationAppError

_DISALLOWED_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_HASHTAG_RE = re.compile(r"^[^\s#]{1,64}$", re.UNICODE)

ALLOWED_UPLOAD_TYPES: dict[str, frozenset[str]] = {
    ".png": frozenset({"image/png"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".webp": frozenset({"image/webp"}),
    ".pdf": frozenset({"application/pdf"}),
    ".txt": frozenset({"text/plain"}),
    ".csv": frozenset({"text/csv", "application/csv", "application/vnd.ms-excel"}),
}


@dataclass(frozen=True)
class ValidatedUpload:
    filename: str
    extension: str
    content_type: str
    size: int


def normalize_text(
    value: object,
    *,
    field: str,
    min_length: int = 0,
    max_length: int = 10_000,
    allow_newlines: bool = True,
) -> str:
    """Normalize Unicode and enforce safe, bounded text input."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)

    if _DISALLOWED_CONTROL_RE.search(text):
        raise ValidationAppError(f"{field} contains unsupported control characters.")
    if not allow_newlines and ("\n" in text or "\r" in text):
        raise ValidationAppError(f"{field} must be a single line.")

    text = text.strip()
    if len(text) < min_length:
        raise ValidationAppError(f"{field} must contain at least {min_length} character(s).")
    if len(text) > max_length:
        raise ValidationAppError(f"{field} cannot exceed {max_length} characters.")
    return text


def validate_choice(value: object, *, field: str, allowed: set[str] | frozenset[str]) -> str:
    normalized = normalize_text(
        value,
        field=field,
        min_length=1,
        max_length=128,
        allow_newlines=False,
    ).lower()
    if normalized not in allowed:
        raise ValidationAppError(f"Unsupported {field.lower()} selection.")
    return normalized


def validate_hashtag(value: object) -> str:
    hashtag = normalize_text(
        value,
        field="Hashtag",
        min_length=1,
        max_length=64,
        allow_newlines=False,
    ).lstrip("#")
    if not _HASHTAG_RE.fullmatch(hashtag):
        raise ValidationAppError("Hashtags cannot contain spaces or additional # characters.")
    return hashtag


def validate_positive_id(value: object, *, field: str = "ID") -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationAppError(f"{field} must be a positive integer.") from exc
    if result <= 0:
        raise ValidationAppError(f"{field} must be a positive integer.")
    return result


def _validate_public_host(hostname: str, *, field: str) -> None:
    host = hostname.rstrip(".").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValidationAppError(f"{field} must use a public hostname.")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValidationAppError(f"{field} contains an invalid hostname.") from exc
        if "." not in ascii_host or len(ascii_host) > 253:
            raise ValidationAppError(f"{field} must use a valid public hostname.")
        return

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
        raise ValidationAppError(f"{field} cannot point to a private or reserved network address.")


def validate_http_url(
    value: object,
    *,
    field: str = "URL",
    required: bool = True,
    allow_private: bool = False,
) -> str:
    """Validate an HTTP(S) URL and block obvious SSRF destinations by default."""
    url = normalize_text(
        value,
        field=field,
        min_length=1 if required else 0,
        max_length=2048,
        allow_newlines=False,
    )
    if not url and not required:
        return ""

    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise ValidationAppError(f"{field} contains an invalid port or URL structure.") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValidationAppError(f"{field} must begin with http:// or https://.")
    if not parsed.hostname:
        raise ValidationAppError(f"{field} must include a hostname.")
    if parsed.username or parsed.password:
        raise ValidationAppError(f"{field} cannot contain embedded credentials.")
    if not allow_private:
        _validate_public_host(parsed.hostname, field=field)
    return url


def sanitize_filename(filename: object) -> str:
    """Return a conservative filename and reject path traversal attempts."""
    raw = normalize_text(
        filename,
        field="Filename",
        min_length=1,
        max_length=255,
        allow_newlines=False,
    )
    if "/" in raw or "\\" in raw or PurePath(raw).name != raw or raw in {".", ".."}:
        raise FileUploadError("Filename contains an invalid path.")

    cleaned = _SAFE_FILENAME_RE.sub("_", raw).strip(" ._")
    if not cleaned or cleaned.startswith("."):
        raise FileUploadError("Filename is invalid after sanitization.")
    return cleaned[:255]


def _matches_magic(extension: str, content: bytes) -> bool:
    if extension == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if extension in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if extension == ".pdf":
        return content.startswith(b"%PDF-")
    if extension == ".webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    return True


def validate_upload(
    *,
    filename: object,
    content: bytes,
    content_type: object = "",
    max_size_bytes: int = 10 * 1024 * 1024,
    allowed_types: dict[str, frozenset[str]] | None = None,
) -> ValidatedUpload:
    """Validate filename, extension, declared MIME type, size, and common file signatures."""
    safe_name = sanitize_filename(filename)
    extension = PurePath(safe_name).suffix.lower()
    allowed = allowed_types or ALLOWED_UPLOAD_TYPES
    if extension not in allowed:
        raise FileUploadError("This file type is not allowed.")
    if not isinstance(content, bytes) or not content:
        raise FileUploadError("The uploaded file is empty or unreadable.")
    if len(content) > max_size_bytes:
        raise FileUploadError("The uploaded file exceeds the allowed size limit.")

    mime = normalize_text(
        content_type,
        field="Content type",
        min_length=0,
        max_length=255,
        allow_newlines=False,
    ).lower()
    if mime and mime not in allowed[extension]:
        raise FileUploadError("The file content type does not match its extension.")
    if not _matches_magic(extension, content):
        raise FileUploadError("The file signature does not match its extension.")

    return ValidatedUpload(
        filename=safe_name,
        extension=extension,
        content_type=mime or next(iter(allowed[extension])),
        size=len(content),
    )
