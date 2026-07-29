"""Native authentication, RBAC, secure sessions, lockout, and optional TOTP MFA."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.encryption import decrypt_text, encrypt_text
from core.errors import AppError, ConfigurationError, ValidationAppError
from core.security_models import AuthSession, UserAccount

ROLE_OWNER = "owner"
ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN = "admin"
ROLE_REVIEWER = "reviewer"
ROLE_CONTENT_CREATOR = "content_creator"
ROLE_VIEWER = "viewer"

ROLES = frozenset(
    {
        ROLE_OWNER,
        ROLE_SUPER_ADMIN,
        ROLE_ADMIN,
        ROLE_REVIEWER,
        ROLE_CONTENT_CREATOR,
        ROLE_VIEWER,
    }
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_OWNER: frozenset({"*"}),
    ROLE_SUPER_ADMIN: frozenset(
        {
            "read",
            "create_content",
            "edit_content",
            "approve_content",
            "publish_content",
            "manage_brand",
            "manage_chatbot",
            "manage_integrations",
            "manage_users",
            "view_audit",
            "export_data",
            "manage_security",
        }
    ),
    ROLE_ADMIN: frozenset(
        {
            "read",
            "create_content",
            "edit_content",
            "approve_content",
            "publish_content",
            "manage_brand",
            "manage_chatbot",
            "manage_integrations",
            "export_data",
        }
    ),
    ROLE_REVIEWER: frozenset({"read", "edit_content", "approve_content", "export_data"}),
    ROLE_CONTENT_CREATOR: frozenset({"read", "create_content", "edit_content"}),
    ROLE_VIEWER: frozenset({"read"}),
}

_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4, hash_len=32, salt_len=16)
_DUMMY_HASH: str | None = None


class AuthenticationError(AppError):
    default_error_code = "AUTHENTICATION_ERROR"
    default_user_action = "Check your credentials and try again."
    retryable_default = False


class AuthorizationError(AppError):
    default_error_code = "AUTHORIZATION_ERROR"
    default_user_action = "Ask an administrator for the required permission."
    retryable_default = False


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    email: str
    display_name: str
    role: str
    mfa_enabled: bool

    def can(self, permission: str) -> bool:
        permissions = ROLE_PERMISSIONS.get(self.role, frozenset())
        return "*" in permissions or permission in permissions


@dataclass(frozen=True)
class AuthTokens:
    session_token: str
    csrf_token: str
    user: AuthenticatedUser
    expires_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def normalize_email(email: object) -> str:
    value = str(email or "").strip().casefold()
    if not value or len(value) > 320 or not _EMAIL_RE.fullmatch(value):
        raise ValidationAppError("Enter a valid email address.")
    return value


def validate_role(role: object) -> str:
    value = str(role or "").strip().lower()
    if value not in ROLES:
        raise ValidationAppError("Select a valid account role.")
    return value


def validate_password(password: object, *, email: str = "") -> str:
    value = str(password or "")
    minimum = 14 if os.getenv("APP_ENV", "development").lower() == "production" else 12
    if len(value) < minimum or len(value) > 256:
        raise ValidationAppError(f"Password must contain between {minimum} and 256 characters.")
    checks = (
        any(char.islower() for char in value),
        any(char.isupper() for char in value),
        any(char.isdigit() for char in value),
        any(not char.isalnum() for char in value),
    )
    if not all(checks):
        raise ValidationAppError(
            "Password must include uppercase, lowercase, number, and special characters."
        )
    local_part = email.split("@", 1)[0].casefold() if "@" in email else ""
    if local_part and len(local_part) >= 4 and local_part in value.casefold():
        raise ValidationAppError("Password cannot contain the email username.")
    return value


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _dummy_verify(password: str) -> None:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _PASSWORD_HASHER.hash("ContentPilot-Dummy-Password-9!")
    verify_password(_DUMMY_HASH, password)


def _session_hours() -> int:
    raw = os.getenv("AUTH_SESSION_HOURS", "8").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError("AUTH_SESSION_HOURS must be an integer.") from exc
    if not 1 <= value <= 168:
        raise ConfigurationError("AUTH_SESSION_HOURS must be between 1 and 168.")
    return value


def _max_failed_logins() -> int:
    raw = os.getenv("AUTH_MAX_FAILED_LOGINS", "5").strip()
    try:
        value = int(raw)
    except ValueError:
        return 5
    return min(max(value, 3), 20)


def _lock_minutes() -> int:
    raw = os.getenv("AUTH_LOCK_MINUTES", "15").strip()
    try:
        value = int(raw)
    except ValueError:
        return 15
    return min(max(value, 5), 1_440)


def _to_authenticated(user: UserAccount) -> AuthenticatedUser:
    return AuthenticatedUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        mfa_enabled=user.mfa_enabled,
    )


def create_user(
    session: Session,
    *,
    email: str,
    display_name: str,
    password: str,
    role: str = ROLE_VIEWER,
    actor: AuthenticatedUser | None = None,
) -> AuthenticatedUser:
    if actor is not None and not actor.can("manage_users"):
        raise AuthorizationError("You do not have permission to create users.")

    normalized_email = normalize_email(email)
    clean_name = str(display_name or "").strip()
    if not 2 <= len(clean_name) <= 255:
        raise ValidationAppError("Display name must contain between 2 and 255 characters.")
    clean_role = validate_role(role)
    if clean_role == ROLE_OWNER and actor is not None and actor.role != ROLE_OWNER:
        raise AuthorizationError("Only an owner can create another owner account.")
    clean_password = validate_password(password, email=normalized_email)

    existing = session.scalar(
        select(UserAccount.id).where(func.lower(UserAccount.email) == normalized_email)
    )
    if existing:
        raise ValidationAppError("An account with this email already exists.")

    user = UserAccount(
        email=normalized_email,
        display_name=clean_name,
        password_hash=hash_password(clean_password),
        role=clean_role,
        is_active=True,
    )
    try:
        session.add(user)
        session.flush()
        log_audit_event(
            session,
            action="user.created",
            actor_user_id=actor.id if actor else user.id,
            actor_email=actor.email if actor else user.email,
            resource_type="user",
            resource_id=user.id,
            event_data={"email": user.email, "role": user.role},
        )
        session.commit()
        session.refresh(user)
        return _to_authenticated(user)
    except Exception:
        session.rollback()
        raise


def bootstrap_owner(session: Session) -> AuthenticatedUser | None:
    """Create the first owner from deployment secrets when no account exists."""
    existing = session.scalar(select(UserAccount).order_by(UserAccount.id.asc()).limit(1))
    if existing:
        return _to_authenticated(existing)

    email = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip()
    display_name = os.getenv("BOOTSTRAP_ADMIN_NAME", "ContentPilot Owner").strip()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    password_hash = os.getenv("BOOTSTRAP_ADMIN_PASSWORD_HASH", "").strip()

    if not email or not (password or password_hash):
        if os.getenv("APP_ENV", "development").strip().lower() == "production":
            raise ConfigurationError(
                "The first owner account is not configured.",
                user_action=(
                    "Set BOOTSTRAP_ADMIN_EMAIL and either BOOTSTRAP_ADMIN_PASSWORD_HASH or "
                    "BOOTSTRAP_ADMIN_PASSWORD using deployment secrets."
                ),
            )
        return None

    normalized_email = normalize_email(email)
    if password_hash:
        if not password_hash.startswith("$argon2"):
            raise ConfigurationError("BOOTSTRAP_ADMIN_PASSWORD_HASH must be an Argon2 hash.")
        final_hash = password_hash
    else:
        final_hash = hash_password(validate_password(password, email=normalized_email))

    user = UserAccount(
        email=normalized_email,
        display_name=display_name[:255] or "ContentPilot Owner",
        password_hash=final_hash,
        role=ROLE_OWNER,
        is_active=True,
    )
    try:
        session.add(user)
        session.flush()
        log_audit_event(
            session,
            action="user.bootstrap_owner_created",
            actor_user_id=user.id,
            actor_email=user.email,
            resource_type="user",
            resource_id=user.id,
            event_data={"role": ROLE_OWNER},
        )
        session.commit()
        session.refresh(user)
        return _to_authenticated(user)
    except Exception:
        session.rollback()
        raise


def authenticate_user(
    session: Session,
    *,
    email: str,
    password: str,
    totp_code: str = "",
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> AuthTokens:
    normalized_email = normalize_email(email)
    now = _utc_now()
    user = session.scalar(
        select(UserAccount).where(func.lower(UserAccount.email) == normalized_email).limit(1)
    )
    if user is None:
        _dummy_verify(password)
        log_audit_event(
            session,
            action="auth.login",
            actor_email=normalized_email,
            outcome="failure",
            event_data={"reason": "invalid_credentials"},
        )
        session.commit()
        raise AuthenticationError("Invalid email, password, or authentication code.")

    locked_until = _aware(user.locked_until)
    if locked_until and locked_until > now:
        log_audit_event(
            session,
            action="auth.login",
            actor_user_id=user.id,
            actor_email=user.email,
            outcome="blocked",
            event_data={"reason": "account_locked"},
        )
        session.commit()
        raise AuthenticationError("This account is temporarily locked. Try again later.")

    valid = user.is_active and verify_password(user.password_hash, password)
    if valid and user.mfa_enabled:
        try:
            secret = decrypt_text(
                user.mfa_secret_encrypted or "",
                associated_context=f"user:{user.id}:mfa",
            )
            valid = pyotp.TOTP(secret).verify(str(totp_code or "").strip(), valid_window=1)
        except Exception:
            valid = False

    if not valid:
        user.failed_login_count = int(user.failed_login_count or 0) + 1
        if user.failed_login_count >= _max_failed_logins():
            user.locked_until = now + timedelta(minutes=_lock_minutes())
            user.failed_login_count = 0
        log_audit_event(
            session,
            action="auth.login",
            actor_user_id=user.id,
            actor_email=user.email,
            outcome="failure",
            event_data={"reason": "invalid_credentials"},
        )
        session.commit()
        raise AuthenticationError("Invalid email, password, or authentication code.")

    if _PASSWORD_HASHER.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now

    raw_session = secrets.token_urlsafe(48)
    raw_csrf = secrets.token_urlsafe(32)
    expires_at = now + timedelta(hours=_session_hours())
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=_hash_token(raw_session),
        csrf_hash=_hash_token(raw_csrf),
        user_agent_hash=_fingerprint(user_agent),
        ip_hash=_fingerprint(ip_address),
        expires_at=expires_at,
    )
    try:
        session.add(auth_session)
        session.flush()
        log_audit_event(
            session,
            action="auth.login",
            actor_user_id=user.id,
            actor_email=user.email,
            resource_type="auth_session",
            resource_id=auth_session.id,
            event_data={"role": user.role},
        )
        session.commit()
        return AuthTokens(
            session_token=raw_session,
            csrf_token=raw_csrf,
            user=_to_authenticated(user),
            expires_at=expires_at,
        )
    except Exception:
        session.rollback()
        raise


def resolve_session(
    session: Session,
    session_token: str | None,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> AuthenticatedUser | None:
    if not session_token:
        return None
    now = _utc_now()
    auth_session = session.scalar(
        select(AuthSession).where(AuthSession.token_hash == _hash_token(session_token)).limit(1)
    )
    if auth_session is None or auth_session.revoked_at is not None:
        return None
    if (_aware(auth_session.expires_at) or now) <= now:
        return None
    if auth_session.user_agent_hash and auth_session.user_agent_hash != _fingerprint(user_agent):
        return None
    if auth_session.ip_hash and ip_address and auth_session.ip_hash != _fingerprint(ip_address):
        return None

    user = session.get(UserAccount, auth_session.user_id)
    if user is None or not user.is_active:
        return None
    auth_session.last_seen_at = now
    session.commit()
    return _to_authenticated(user)


def validate_csrf(session: Session, session_token: str, csrf_token: str) -> bool:
    if not session_token or not csrf_token:
        return False
    stored = session.scalar(
        select(AuthSession.csrf_hash).where(
            AuthSession.token_hash == _hash_token(session_token),
            AuthSession.revoked_at.is_(None),
        )
    )
    return bool(stored and secrets.compare_digest(stored, _hash_token(csrf_token)))


def logout(session: Session, session_token: str, *, actor: AuthenticatedUser | None = None) -> None:
    if not session_token:
        return
    now = _utc_now()
    auth_session = session.scalar(
        select(AuthSession).where(AuthSession.token_hash == _hash_token(session_token)).limit(1)
    )
    if auth_session and auth_session.revoked_at is None:
        auth_session.revoked_at = now
        auth_session.revoke_reason = "logout"
        log_audit_event(
            session,
            action="auth.logout",
            actor_user_id=actor.id if actor else auth_session.user_id,
            actor_email=actor.email if actor else None,
            resource_type="auth_session",
            resource_id=auth_session.id,
        )
        session.commit()


def revoke_all_user_sessions(
    session: Session,
    user_id: int,
    *,
    actor: AuthenticatedUser,
    reason: str = "administrator_revocation",
) -> int:
    if actor.id != user_id and not actor.can("manage_users"):
        raise AuthorizationError("You do not have permission to revoke these sessions.")
    result = session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_utc_now(), revoke_reason=reason[:255])
    )
    log_audit_event(
        session,
        action="auth.sessions_revoked",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="user",
        resource_id=user_id,
        event_data={"count": result.rowcount or 0, "reason": reason[:255]},
    )
    session.commit()
    return int(result.rowcount or 0)


def require_permission(user: AuthenticatedUser | None, permission: str) -> None:
    if user is None:
        raise AuthenticationError("Sign in to continue.")
    if not user.can(permission):
        raise AuthorizationError("You do not have permission to perform this action.")


def begin_mfa_enrollment(session: Session, user: AuthenticatedUser) -> tuple[str, str]:
    model = session.get(UserAccount, user.id)
    if model is None or not model.is_active:
        raise AuthenticationError("Account is unavailable.")
    secret = pyotp.random_base32()
    encrypted = encrypt_text(secret, associated_context=f"user:{user.id}:mfa")
    model.mfa_secret_encrypted = encrypted.ciphertext
    model.mfa_enabled = False
    session.commit()
    issuer = os.getenv("TOTP_ISSUER", "Artixcore ContentPilot")[:64]
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=issuer)
    return secret, uri


def confirm_mfa_enrollment(session: Session, user: AuthenticatedUser, code: str) -> None:
    model = session.get(UserAccount, user.id)
    if model is None or not model.mfa_secret_encrypted:
        raise AuthenticationError("MFA enrollment has not been started.")
    secret = decrypt_text(
        model.mfa_secret_encrypted,
        associated_context=f"user:{user.id}:mfa",
    )
    if not pyotp.TOTP(secret).verify(str(code or "").strip(), valid_window=1):
        raise AuthenticationError("The authentication code is invalid.")
    model.mfa_enabled = True
    log_audit_event(
        session,
        action="auth.mfa_enabled",
        actor_user_id=user.id,
        actor_email=user.email,
        resource_type="user",
        resource_id=user.id,
    )
    session.commit()
