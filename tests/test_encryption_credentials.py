"""Regression tests for authenticated encryption and credential storage."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from core.auth import ROLE_OWNER, create_user
from core.credential_store import (
    list_credential_metadata,
    rotate_credential_key,
    set_credential_active,
    store_credential,
)
from core.encryption import decrypt_text, encrypt_text
from core.errors import ConfigurationError
from core.security_models import EncryptedCredential


def _owner(db_session):
    return create_user(
        db_session,
        email="vault-owner@example.com",
        display_name="Vault Owner",
        password="VaultSecure!4832",
        role=ROLE_OWNER,
    )


def test_encryption_round_trip_and_context_binding():
    encrypted = encrypt_text("super-secret-value", associated_context="credential:test")
    assert encrypted.ciphertext != "super-secret-value"
    assert decrypt_text(
        encrypted.ciphertext,
        associated_context="credential:test",
    ) == "super-secret-value"

    with pytest.raises(ConfigurationError, match="context validation"):
        decrypt_text(encrypted.ciphertext, associated_context="credential:other")


def test_credential_store_never_persists_plaintext(db_session):
    owner = _owner(db_session)
    model = store_credential(
        db_session,
        name="linkedin.access_token",
        secret_value="linkedin-secret-token-123456",
        credential_type="oauth_access_token",
        actor=owner,
    )
    stored = db_session.scalar(
        select(EncryptedCredential).where(EncryptedCredential.id == model.id)
    )
    assert stored is not None
    assert "linkedin-secret-token-123456" not in stored.ciphertext
    assert stored.version == 1
    assert stored.is_active is True


def test_credential_rotation_versions_and_status(db_session):
    owner = _owner(db_session)
    model = store_credential(
        db_session,
        name="meta.client_secret",
        secret_value="first-secret-value-123",
        credential_type="client_secret",
        actor=owner,
    )
    updated = store_credential(
        db_session,
        name="meta.client_secret",
        secret_value="second-secret-value-456",
        credential_type="client_secret",
        actor=owner,
    )
    assert updated.id == model.id
    assert updated.version == 2

    reencrypted = rotate_credential_key(
        db_session,
        name="meta.client_secret",
        actor=owner,
    )
    assert reencrypted.version == 3

    inactive = set_credential_active(
        db_session,
        name="meta.client_secret",
        active=False,
        actor=owner,
    )
    assert inactive.is_active is False
    assert len(list_credential_metadata(db_session, actor=owner)) == 1


def test_missing_encryption_key_fails_closed(monkeypatch):
    monkeypatch.delenv("CONTENTPILOT_ENCRYPTION_KEYS", raising=False)
    with pytest.raises(ConfigurationError, match="CONTENTPILOT_ENCRYPTION_KEYS"):
        encrypt_text("secret", associated_context="test")
