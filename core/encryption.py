"""Application-level authenticated encryption with key rotation support."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from core.errors import ConfigurationError


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: str
    key_id: str


def _configured_keys() -> list[bytes]:
    raw = os.getenv("CONTENTPILOT_ENCRYPTION_KEYS", "").strip()
    if not raw:
        raise ConfigurationError(
            "CONTENTPILOT_ENCRYPTION_KEYS is required for encrypted credentials.",
            user_action="Generate one or more Fernet keys and store them in the deployment secret manager.",
        )

    keys: list[bytes] = []
    for item in raw.split(","):
        value = item.strip().encode("ascii", errors="strict")
        try:
            decoded = base64.urlsafe_b64decode(value)
        except Exception as exc:
            raise ConfigurationError("An encryption key is not valid URL-safe base64.") from exc
        if len(decoded) != 32:
            raise ConfigurationError("Each encryption key must decode to exactly 32 bytes.")
        keys.append(value)

    if len(set(keys)) != len(keys):
        raise ConfigurationError("Encryption keys must be unique.")
    return keys


def active_key_id() -> str:
    key = _configured_keys()[0]
    return hashlib.sha256(key).hexdigest()[:16]


def encrypt_text(plaintext: str, *, associated_context: str = "") -> EncryptedValue:
    """Encrypt UTF-8 text. Optional context is bound inside the encrypted payload."""
    if plaintext is None:
        raise ValueError("plaintext cannot be None")
    context = str(associated_context or "")
    payload = f"v1\n{context}\n{plaintext}".encode("utf-8")
    key = _configured_keys()[0]
    token = Fernet(key).encrypt(payload).decode("ascii")
    return EncryptedValue(ciphertext=token, key_id=hashlib.sha256(key).hexdigest()[:16])


def decrypt_text(ciphertext: str, *, associated_context: str = "") -> str:
    """Decrypt using the active key and any retained rotation keys."""
    if not ciphertext:
        raise ValueError("ciphertext is required")
    try:
        plaintext = MultiFernet([Fernet(key) for key in _configured_keys()]).decrypt(
            ciphertext.encode("ascii")
        )
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ConfigurationError(
            "Encrypted data could not be authenticated or decrypted.",
            user_action="Verify the encryption key order and retain previous keys during rotation.",
        ) from exc

    decoded = plaintext.decode("utf-8")
    parts = decoded.split("\n", 2)
    if len(parts) != 3 or parts[0] != "v1":
        raise ConfigurationError("Encrypted data has an unsupported format.")
    expected_context = str(associated_context or "")
    if parts[1] != expected_context:
        raise ConfigurationError("Encrypted data context validation failed.")
    return parts[2]


def rotate_ciphertext(ciphertext: str, *, associated_context: str = "") -> EncryptedValue:
    """Decrypt with any retained key and re-encrypt with the active key."""
    plaintext = decrypt_text(ciphertext, associated_context=associated_context)
    return encrypt_text(plaintext, associated_context=associated_context)
