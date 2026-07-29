"""Configurable OAuth 2.0 authorization-code and refresh flows with PKCE."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import AuthenticatedUser
from core.credential_store import get_credential_value_internal, store_credential
from core.encryption import decrypt_text, encrypt_text
from core.errors import ExternalAPIError, ValidationAppError
from core.operations_models import IntegrationConnection
from core.outbound_http import request_json_limited, validate_outbound_https
from core.product_models import OAuthAuthorizationState
from core.tenancy import WorkspaceContext, require_workspace_permission
from core.validation import normalize_text, validate_http_url, validate_positive_id

_SUPPORTED_PROVIDERS = frozenset({"linkedin", "x", "meta"})
_ACCOUNT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,99}$")


@dataclass(frozen=True)
class OAuthProviderConfig:
    provider: str
    authorization_url: str
    token_url: str
    client_id: str
    client_secret: str
    default_scopes: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    token_auth_method: str
    authorize_params: dict[str, str]
    token_params: dict[str, str]


@dataclass(frozen=True)
class OAuthStartResult:
    authorization_url: str
    expires_at: datetime
    provider: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _provider(value: object) -> str:
    provider = str(value or "").strip().lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValidationAppError("Select a supported OAuth provider.")
    return provider


def _env_prefix(provider: str) -> str:
    return f"OAUTH_{provider.upper()}"


def _json_object_env(name: str) -> dict[str, str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationAppError(f"{name} must contain valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ValidationAppError(f"{name} must be a JSON object.")
    result: dict[str, str] = {}
    for key, value in parsed.items():
        clean_key = normalize_text(
            key, field=f"{name} key", min_length=1, max_length=100, allow_newlines=False
        )
        clean_value = normalize_text(
            value, field=f"{name} value", max_length=1_000, allow_newlines=False
        )
        result[clean_key] = clean_value
    return result


def get_provider_config(provider: str) -> OAuthProviderConfig:
    safe_provider = _provider(provider)
    prefix = _env_prefix(safe_provider)
    authorization_url = os.getenv(f"{prefix}_AUTHORIZATION_URL", "").strip()
    token_url = os.getenv(f"{prefix}_TOKEN_URL", "").strip()
    client_id = os.getenv(f"{prefix}_CLIENT_ID", "").strip()
    client_secret = os.getenv(f"{prefix}_CLIENT_SECRET", "")
    scopes = tuple(
        value.strip()
        for value in os.getenv(f"{prefix}_SCOPES", "").replace(",", " ").split()
        if value.strip()
    )
    allowed_hosts = tuple(
        value.strip().lower().rstrip(".")
        for value in os.getenv(f"{prefix}_ALLOWED_HOSTS", "").split(",")
        if value.strip()
    )
    auth_method = os.getenv(f"{prefix}_TOKEN_AUTH_METHOD", "client_secret_post").strip().lower()
    if auth_method not in {"client_secret_post", "client_secret_basic", "none"}:
        raise ValidationAppError(f"{prefix}_TOKEN_AUTH_METHOD is invalid.")
    missing = [
        name
        for name, value in (
            (f"{prefix}_AUTHORIZATION_URL", authorization_url),
            (f"{prefix}_TOKEN_URL", token_url),
            (f"{prefix}_CLIENT_ID", client_id),
            (f"{prefix}_ALLOWED_HOSTS", allowed_hosts),
        )
        if not value
    ]
    if missing:
        raise ValidationAppError(
            f"OAuth provider {safe_provider} is not configured. Missing: {', '.join(missing)}."
        )
    if auth_method != "none" and not client_secret:
        raise ValidationAppError(f"{prefix}_CLIENT_SECRET is required for this token auth method.")
    authorization_url = validate_outbound_https(
        authorization_url,
        field=f"{safe_provider} authorization endpoint",
        allowed_hosts=allowed_hosts,
    )
    token_url = validate_outbound_https(
        token_url,
        field=f"{safe_provider} token endpoint",
        allowed_hosts=allowed_hosts,
    )
    return OAuthProviderConfig(
        provider=safe_provider,
        authorization_url=authorization_url,
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        default_scopes=scopes,
        allowed_hosts=allowed_hosts,
        token_auth_method=auth_method,
        authorize_params=_json_object_env(f"{prefix}_AUTHORIZE_PARAMS_JSON"),
        token_params=_json_object_env(f"{prefix}_TOKEN_PARAMS_JSON"),
    )


def configured_provider_status() -> dict[str, bool]:
    result: dict[str, bool] = {}
    for provider in sorted(_SUPPORTED_PROVIDERS):
        try:
            get_provider_config(provider)
            result[provider] = True
        except Exception:
            result[provider] = False
    return result


def _redirect_uri(value: object) -> str:
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    uri = validate_http_url(
        value,
        field="OAuth redirect URI",
        required=True,
        allow_private=app_env in {"development", "test"},
    )
    if app_env in {"staging", "production"} and not uri.lower().startswith("https://"):
        raise ValidationAppError("OAuth redirect URI must use HTTPS outside development.")
    return uri


def _account_key(value: object) -> str:
    key = str(value or "").strip().lower()
    key = re.sub(r"[^a-z0-9._-]+", "-", key).strip("-")
    if not _ACCOUNT_KEY_RE.fullmatch(key):
        raise ValidationAppError(
            "Account key must contain 2 to 100 lowercase letters, numbers, dots, underscores, or hyphens."
        )
    return key


def _scope_list(values: list[str] | tuple[str, ...], defaults: tuple[str, ...]) -> list[str]:
    source = values or list(defaults)
    result: list[str] = []
    for value in source:
        scope = normalize_text(
            value, field="OAuth scope", min_length=1, max_length=200, allow_newlines=False
        )
        if scope not in result:
            result.append(scope)
        if len(result) > 100:
            raise ValidationAppError("Too many OAuth scopes were requested.")
    if not result:
        raise ValidationAppError("At least one OAuth scope is required.")
    return result


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _state_context(workspace_id: int, state_hash: str) -> str:
    return f"oauth-state:{workspace_id}:{state_hash}"


def begin_authorization(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    provider: str,
    redirect_uri: str,
    account_key: str,
    display_name: str,
    scopes: list[str] | None = None,
) -> OAuthStartResult:
    require_workspace_permission(context, "integrations:write")
    config = get_provider_config(provider)
    safe_redirect = _redirect_uri(redirect_uri)
    safe_account_key = _account_key(account_key)
    safe_display_name = normalize_text(
        display_name,
        field="Connection display name",
        min_length=2,
        max_length=255,
        allow_newlines=False,
    )
    requested_scopes = _scope_list(scopes or [], config.default_scopes)
    raw_state = secrets.token_urlsafe(48)
    state_hash = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
    verifier = secrets.token_urlsafe(64)
    encrypted_verifier = encrypt_text(
        verifier, associated_context=_state_context(context.workspace_id, state_hash)
    )
    expires_at = _utc_now() + timedelta(minutes=10)
    state_metadata = {
        "scopes": requested_scopes,
        "account_key": safe_account_key,
        "display_name": safe_display_name,
    }
    model = OAuthAuthorizationState(
        provider=config.provider,
        state_hash=state_hash,
        pkce_verifier_encrypted=encrypted_verifier.ciphertext,
        redirect_uri=safe_redirect,
        requested_scopes_json=json.dumps(state_metadata, separators=(",", ":")),
        status="pending",
        requested_by_user_id=actor.id,
        expires_at=expires_at,
    )
    session.add(model)
    session.flush()
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": safe_redirect,
        "state": raw_state,
        "scope": " ".join(requested_scopes),
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
        **config.authorize_params,
    }
    authorization_url = f"{config.authorization_url}?{urlencode(params)}"
    log_audit_event(
        session,
        action="oauth.authorization_started",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="oauth_authorization_state",
        resource_id=model.id,
        event_data={
            "provider": config.provider,
            "account_key": safe_account_key,
            "scopes": requested_scopes,
        },
    )
    session.commit()
    return OAuthStartResult(
        authorization_url=authorization_url,
        expires_at=expires_at,
        provider=config.provider,
    )


def _state_metadata(model: OAuthAuthorizationState) -> dict:
    try:
        metadata = json.loads(model.requested_scopes_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValidationAppError("OAuth authorization metadata is invalid.") from exc
    if not isinstance(metadata, dict):
        raise ValidationAppError("OAuth authorization metadata is invalid.")
    return metadata


def _token_request(
    config: OAuthProviderConfig,
    *,
    grant_type: str,
    parameters: dict[str, str],
) -> dict:
    data = {
        "grant_type": grant_type,
        "client_id": config.client_id,
        **parameters,
        **config.token_params,
    }
    auth: tuple[str, str] | None = None
    if config.token_auth_method == "client_secret_post":
        data["client_secret"] = config.client_secret
    elif config.token_auth_method == "client_secret_basic":
        auth = (config.client_id, config.client_secret)
        data.pop("client_id", None)
    status_code, response = request_json_limited(
        "POST",
        config.token_url,
        allowed_hosts=config.allowed_hosts,
        data=data,
        headers={"Accept": "application/json"},
        auth=auth,
        timeout_seconds=20,
        max_response_bytes=256 * 1024,
    )
    if not 200 <= status_code < 300:
        retryable = status_code == 429 or status_code >= 500
        error_name = str(response.get("error") or "token_exchange_failed")[:100]
        raise ExternalAPIError(
            f"OAuth token endpoint returned HTTP {status_code} ({error_name}).",
            service=config.provider,
            retryable=retryable,
        )
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or len(access_token) < 8:
        raise ExternalAPIError(
            "OAuth provider response did not include a usable access token.",
            service=config.provider,
            retryable=False,
        )
    return response


def _token_expiry(response: dict) -> datetime | None:
    value = response.get("expires_in")
    if value is None:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ExternalAPIError(
            "OAuth provider returned an invalid token expiry.", retryable=False
        ) from exc
    if not 1 <= seconds <= 10 * 365 * 24 * 3600:
        raise ExternalAPIError("OAuth token expiry is outside the supported range.", retryable=False)
    return _utc_now() + timedelta(seconds=seconds)


def _credential_names(provider: str, account_key: str) -> tuple[str, str]:
    prefix = f"oauth.{provider}.{account_key}"
    return f"{prefix}.access_token", f"{prefix}.refresh_token"


def complete_authorization(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    raw_state: str,
    authorization_code: str,
) -> IntegrationConnection:
    require_workspace_permission(context, "integrations:write")
    state_value = str(raw_state or "").strip()
    code = str(authorization_code or "").strip()
    if not 32 <= len(state_value) <= 300 or not 2 <= len(code) <= 10_000:
        raise ValidationAppError("OAuth callback state or authorization code is invalid.")
    state_hash = hashlib.sha256(state_value.encode("utf-8")).hexdigest()
    model = session.scalar(
        select(OAuthAuthorizationState).where(
            OAuthAuthorizationState.state_hash == state_hash,
            OAuthAuthorizationState.status == "pending",
        )
    )
    if model is None:
        raise ValidationAppError("OAuth state is invalid, expired, or already consumed.")
    if model.requested_by_user_id != actor.id:
        raise ValidationAppError("OAuth authorization was started by a different user.")
    expiry = model.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= _utc_now():
        model.status = "expired"
        session.commit()
        raise ValidationAppError("OAuth authorization state has expired.")

    config = get_provider_config(model.provider)
    metadata = _state_metadata(model)
    account_key = _account_key(metadata.get("account_key"))
    display_name = normalize_text(
        metadata.get("display_name"),
        field="Connection display name",
        min_length=2,
        max_length=255,
        allow_newlines=False,
    )
    verifier = decrypt_text(
        model.pkce_verifier_encrypted,
        associated_context=_state_context(context.workspace_id, state_hash),
    )
    try:
        response = _token_request(
            config,
            grant_type="authorization_code",
            parameters={
                "code": code,
                "redirect_uri": model.redirect_uri,
                "code_verifier": verifier,
            },
        )
        access_name, refresh_name = _credential_names(config.provider, account_key)
        store_credential(
            session,
            name=access_name,
            secret_value=str(response["access_token"]),
            credential_type="oauth_access_token",
            actor=actor,
            commit=False,
        )
        refresh_token = response.get("refresh_token")
        if isinstance(refresh_token, str) and len(refresh_token) >= 8:
            store_credential(
                session,
                name=refresh_name,
                secret_value=refresh_token,
                credential_type="oauth_refresh_token",
                actor=actor,
                commit=False,
            )
        else:
            refresh_name = ""
        connection = session.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.platform == config.provider,
                IntegrationConnection.account_key == account_key,
            )
        )
        if connection is None:
            connection = IntegrationConnection(
                platform=config.provider,
                account_key=account_key,
                display_name=display_name,
                status="connected",
                created_by_user_id=actor.id,
            )
            session.add(connection)
        connection.display_name = display_name
        connection.status = "connected"
        connection.access_credential_name = access_name
        connection.refresh_credential_name = refresh_name or None
        connection.external_account_id = str(
            response.get("user_id") or response.get("account_id") or response.get("id") or ""
        )[:255] or None
        connection.token_expires_at = _token_expiry(response)
        connection.last_success_at = _utc_now()
        connection.last_error_code = None
        connection.last_error_message = None
        model.status = "consumed"
        model.consumed_at = _utc_now()
        log_audit_event(
            session,
            action="oauth.authorization_completed",
            actor_user_id=actor.id,
            actor_email=actor.email,
            resource_type="integration_connection",
            resource_id=connection.id,
            event_data={
                "provider": config.provider,
                "account_key": account_key,
                "refresh_token_present": bool(refresh_name),
            },
        )
        session.commit()
        session.refresh(connection)
        return connection
    except Exception:
        session.rollback()
        retry_state = session.scalar(
            select(OAuthAuthorizationState).where(
                OAuthAuthorizationState.state_hash == state_hash
            )
        )
        if retry_state is not None and retry_state.status == "pending":
            retry_state.status = "revoked"
            session.commit()
        raise


def refresh_connection_token(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    connection_id: int,
) -> IntegrationConnection:
    require_workspace_permission(context, "integrations:write")
    connection = session.get(
        IntegrationConnection, validate_positive_id(connection_id, field="Connection ID")
    )
    if connection is None:
        raise ValidationAppError("Integration connection was not found.")
    if not connection.refresh_credential_name:
        raise ValidationAppError("This connection has no refresh token.")
    config = get_provider_config(connection.platform)
    refresh_token = get_credential_value_internal(
        session, name=connection.refresh_credential_name
    )
    response = _token_request(
        config,
        grant_type="refresh_token",
        parameters={"refresh_token": refresh_token},
    )
    store_credential(
        session,
        name=connection.access_credential_name or "",
        secret_value=str(response["access_token"]),
        credential_type="oauth_access_token",
        actor=actor,
        commit=False,
    )
    new_refresh = response.get("refresh_token")
    if isinstance(new_refresh, str) and len(new_refresh) >= 8:
        store_credential(
            session,
            name=connection.refresh_credential_name,
            secret_value=new_refresh,
            credential_type="oauth_refresh_token",
            actor=actor,
            commit=False,
        )
    connection.token_expires_at = _token_expiry(response)
    connection.status = "connected"
    connection.last_success_at = _utc_now()
    connection.last_error_code = None
    connection.last_error_message = None
    log_audit_event(
        session,
        action="oauth.token_refreshed",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="integration_connection",
        resource_id=connection.id,
        event_data={"provider": connection.platform},
    )
    session.commit()
    session.refresh(connection)
    return connection


def revoke_pending_authorizations(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    provider: str | None = None,
) -> int:
    require_workspace_permission(context, "integrations:write")
    query = select(OAuthAuthorizationState).where(
        OAuthAuthorizationState.status == "pending"
    )
    if provider:
        query = query.where(OAuthAuthorizationState.provider == _provider(provider))
    models = list(session.scalars(query).all())
    for model in models:
        model.status = "revoked"
    log_audit_event(
        session,
        action="oauth.pending_states_revoked",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="oauth_authorization_state",
        event_data={"count": len(models), "provider": provider},
    )
    session.commit()
    return len(models)
