"""Operational control center for jobs, integrations, and webhook receipts."""

from __future__ import annotations

import json

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser, require_permission
from core.error_handler import handle_exception
from core.integrations import (
    list_connections,
    queue_health_check,
    set_connection_status,
    upsert_connection,
)
from core.jobs import cancel_job, list_jobs, retry_dead_letter_job
from core.operations_models import WebhookReceipt
from core.security_models import EncryptedCredential
from ui.components import page_header, section_title
from ui.notifications import show_error_from_dict, show_info, show_success


def _error(exc: Exception, context: str) -> None:
    show_error_from_dict(handle_exception(exc, context=context))


def _render_jobs(session: Session, user: AuthenticatedUser) -> None:
    section_title("Background Jobs")
    status = st.selectbox(
        "Status Filter",
        ["all", "queued", "running", "succeeded", "dead_letter", "cancelled"],
    )
    try:
        jobs = list_jobs(
            session,
            actor=user,
            status=None if status == "all" else status,
            limit=250,
        )
    except Exception as exc:
        session.rollback()
        _error(exc, "operations.jobs.list")
        return

    rows = []
    for job in jobs:
        try:
            payload = json.loads(job.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        rows.append(
            {
                "ID": job.id,
                "Type": job.job_type,
                "Status": job.status,
                "Priority": job.priority,
                "Attempts": f"{job.attempts}/{job.max_attempts}",
                "Available": job.available_at,
                "Requested By": job.requested_by_user_id,
                "Error": job.error_code,
                "Payload": json.dumps(payload, ensure_ascii=False)[:300],
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)

    manageable = [job for job in jobs if job.status in {"queued", "running", "failed", "dead_letter"}]
    if user.can("manage_security") and manageable:
        selected_id = st.selectbox(
            "Select job",
            [job.id for job in manageable],
            format_func=lambda value: next(
                f"#{job.id} {job.job_type} ({job.status})" for job in manageable if job.id == value
            ),
        )
        selected = next(job for job in manageable if job.id == selected_id)
        c1, c2 = st.columns(2)
        with c1:
            if st.button(
                "Retry Job",
                use_container_width=True,
                disabled=selected.status not in {"failed", "dead_letter"},
            ):
                try:
                    retry_dead_letter_job(session, job_id=selected.id, actor=user)
                    show_success("Job queued for a manual retry.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    _error(exc, "operations.jobs.retry")
        with c2:
            if st.button(
                "Cancel Job",
                use_container_width=True,
                disabled=selected.status not in {"queued", "running"},
            ):
                try:
                    cancel_job(session, job_id=selected.id, actor=user)
                    show_success("Job cancelled.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    _error(exc, "operations.jobs.cancel")


def _render_integrations(session: Session, user: AuthenticatedUser) -> None:
    section_title("Integration Registry")
    show_info(
        "Store tokens in Security > Encrypted Credential Vault first. Connections only reference credential names."
    )
    credentials = list(
        session.scalars(
            select(EncryptedCredential)
            .where(EncryptedCredential.is_active.is_(True))
            .order_by(EncryptedCredential.credential_name.asc())
        ).all()
    )
    credential_names = [""] + [item.credential_name for item in credentials]

    with st.form("integration_connection_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            platform = st.selectbox(
                "Platform",
                ["linkedin", "facebook", "instagram", "twitter", "youtube", "telegram", "website"],
            )
            account_key = st.text_input("Account Key", max_chars=255)
            display_name = st.text_input("Display Name", max_chars=255)
        with c2:
            access_name = st.selectbox("Access Credential", credential_names)
            refresh_name = st.selectbox("Refresh Credential", credential_names)
            external_id = st.text_input("External Account ID", max_chars=255)
        save = st.form_submit_button("Save Connection", type="primary", use_container_width=True)

    if save:
        try:
            connection = upsert_connection(
                session,
                platform=platform,
                account_key=account_key,
                display_name=display_name,
                actor=user,
                access_credential_name=access_name or None,
                refresh_credential_name=refresh_name or None,
                external_account_id=external_id or None,
            )
            show_success(f"Saved {connection.display_name}.")
            st.rerun()
        except Exception as exc:
            session.rollback()
            _error(exc, "operations.integration.save")

    try:
        connections = list_connections(session, actor=user)
    except Exception as exc:
        session.rollback()
        _error(exc, "operations.integration.list")
        return

    st.dataframe(
        [
            {
                "ID": item.id,
                "Platform": item.platform,
                "Name": item.display_name,
                "Account": item.account_key,
                "Status": item.status,
                "Access Credential": item.access_credential_name,
                "Refresh Credential": item.refresh_credential_name,
                "Token Expiry": item.token_expires_at,
                "Last Check": item.last_health_check_at,
                "Error": item.last_error_code,
            }
            for item in connections
        ],
        use_container_width=True,
        hide_index=True,
    )

    if not connections:
        return
    selected_id = st.selectbox(
        "Select connection",
        [item.id for item in connections],
        format_func=lambda value: next(
            f"{item.platform}: {item.display_name}" for item in connections if item.id == value
        ),
    )
    selected = next(item for item in connections if item.id == selected_id)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Queue Health Check", use_container_width=True):
            try:
                job_id = queue_health_check(session, connection_id=selected.id, actor=user)
                show_success(f"Health-check job #{job_id} queued.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                _error(exc, "operations.integration.health")
    with c2:
        new_status = st.selectbox(
            "Set Status",
            ["disconnected", "connecting", "connected", "degraded", "expired", "disabled"],
            index=["disconnected", "connecting", "connected", "degraded", "expired", "disabled"].index(selected.status),
        )
        if st.button("Update Connection Status", use_container_width=True):
            try:
                set_connection_status(
                    session,
                    connection_id=selected.id,
                    status=new_status,
                    actor=user,
                )
                show_success("Connection status updated.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                _error(exc, "operations.integration.status")


def _render_webhooks(session: Session) -> None:
    section_title("Webhook Receipts")
    receipts = list(
        session.scalars(
            select(WebhookReceipt).order_by(WebhookReceipt.created_at.desc()).limit(250)
        ).all()
    )
    st.dataframe(
        [
            {
                "Time": item.created_at,
                "Provider": item.provider,
                "Event ID": item.event_id,
                "Event Type": item.event_type,
                "Signature Valid": item.signature_valid,
                "Status": item.status,
                "Digest": item.payload_digest,
                "Error": item.error_code,
            }
            for item in receipts
        ],
        use_container_width=True,
        hide_index=True,
    )


def render_operations(session: Session, user: AuthenticatedUser) -> None:
    require_permission(user, "manage_integrations")
    page_header(
        "Operations",
        "Monitor durable jobs, integration health, encrypted credential references, and webhook receipts.",
    )
    _render_jobs(session, user)
    _render_integrations(session, user)
    _render_webhooks(session)


def render(session: Session, user: AuthenticatedUser) -> None:
    render_operations(session, user)
