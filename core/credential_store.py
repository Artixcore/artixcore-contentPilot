"""Encrypted integration credential storage with rotation and audit trails."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import AuthenticatedUser, require_permission
from core.encryption import decrypt_text, encrypt_text, rotate_ciphertext
from core.errors import ValidationAppError
from core.security_models import EncryptedCredential
from core.validation import normalize_text


def _normalize_name(value: object) -> str:
    name = normalize_text(
        value,
        field="Credential name",
        min_length=2,
        max_length=255,
        allow_newlines=False,
    ).lower()
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    if any(char not in allowed for char in name):
        raise ValidationAppError(
            "Credential names may contain lowercase letters, numbers, dots, underscores, and hyphens."
        )
    return name


def list_credential_metadata(
    session: Session,
    *,
    actor: AuthenticatedUser,
) -> list[EncryptedCredential]:
    require_permission(actor, "manage_security")
    return list(
        session.scalars(
            select(EncryptedCredential).order_by(EncryptedCredential.credential_name.asc())
        ).all()
    )


def store_credential(
    session: Session,
    *,
    name: str,
    secret_value: str,
    credential_type: str,
    actor: AuthenticatedUser,
) -> EncryptedCredential:
    if not (actor.can("manage_security") or actor.can("manage_integrations")):
        require_permission(actor, "manage_security")
    clean_name = _normalize_name(name)
    clean_type = normalize_text(
        credential_type or "secret",
        field="Credential type",
        min_length=2,
        max_length=100,
        allow_newlines=False,
    ).lower()
    secret = str(secret_value or "")
    if not 8 <= len(secret) <= 100_000:
        raise ValidationAppError("Credential value must contain between 8 and 100,000 characters.")

    context = f"credential:{clean_name}"
    encrypted = encrypt_text(secret, associated_context=context)
    model = session.scalar(
        select(EncryptedCredential).where(
            EncryptedCredential.credential_name == clean_name
        )
    )
    action = "credential.created"
    if model is None:
        model = EncryptedCredential(
            credential_name=clean_name,
            ciphertext=encrypted.ciphertext,
            key_id=encrypted.key_id,
            credential_type=clean_type,
            is_active=True,
            version=1,
            created_by_user_id=actor.id,
        )
        session.add(model)
    else:
        action = "credential.rotated"
        model.ciphertext = encrypted.ciphertext
        model.key_id = encrypted.key_id
        model.credential_type = clean_type
        model.is_active = True
        model.version = int(model.version or 0) + 1
        model.rotated_at = datetime.now(timezone.utc)

    try:
        session.flush()
        log_audit_event(
            session,
            action=action,
            actor_user_id=actor.id,
            actor_email=actor.email,
            resource_type="encrypted_credential",
            resource_id=model.id,
            event_data={
                "credential_name": clean_name,
                "credential_type": clean_type,
                "version": model.version,
                "key_id": model.key_id,
            },
        )
        session.commit()
        session.refresh(model)
        return model
    except Exception:
        session.rollback()
        raise


def reveal_credential(
    session: Session,
    *,
    name: str,
    actor: AuthenticatedUser,
) -> str:
    require_permission(actor, "manage_security")
    clean_name = _normalize_name(name)
    model = session.scalar(
        select(EncryptedCredential).where(
            EncryptedCredential.credential_name == clean_name,
            EncryptedCredential.is_active.is_(True),
        )
    )
    if model is None:
        raise ValidationAppError("Credential was not found or is inactive.")
    value = decrypt_text(model.ciphertext, associated_context=f"credential:{clean_name}")
    log_audit_event(
        session,
        action="credential.revealed",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="encrypted_credential",
        resource_id=model.id,
        event_data={"credential_name": clean_name},
    )
    session.commit()
    return value


def rotate_credential_key(
    session: Session,
    *,
    name: str,
    actor: AuthenticatedUser,
) -> EncryptedCredential:
    require_permission(actor, "manage_security")
    clean_name = _normalize_name(name)
    model = session.scalar(
        select(EncryptedCredential).where(
            EncryptedCredential.credential_name == clean_name
        )
    )
    if model is None:
        raise ValidationAppError("Credential was not found.")
    encrypted = rotate_ciphertext(
        model.ciphertext,
        associated_context=f"credential:{clean_name}",
    )
    model.ciphertext = encrypted.ciphertext
    model.key_id = encrypted.key_id
    model.version = int(model.version or 0) + 1
    model.rotated_at = datetime.now(timezone.utc)
    log_audit_event(
        session,
        action="credential.key_rotated",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="encrypted_credential",
        resource_id=model.id,
        event_data={"credential_name": clean_name, "key_id": model.key_id},
    )
    session.commit()
    session.refresh(model)
    return model


def set_credential_active(
    session: Session,
    *,
    name: str,
    active: bool,
    actor: AuthenticatedUser,
) -> EncryptedCredential:
    require_permission(actor, "manage_security")
    clean_name = _normalize_name(name)
    model = session.scalar(
        select(EncryptedCredential).where(
            EncryptedCredential.credential_name == clean_name
        )
    )
    if model is None:
        raise ValidationAppError("Credential was not found.")
    model.is_active = bool(active)
    log_audit_event(
        session,
        action="credential.status_updated",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="encrypted_credential",
        resource_id=model.id,
        event_data={"credential_name": clean_name, "active": model.is_active},
    )
    session.commit()
    session.refresh(model)
    return model
