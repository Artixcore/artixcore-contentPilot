"""Deterministic lead intake, scoring, classification, assignment, and status services."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import AuthenticatedUser, normalize_email
from core.errors import ValidationAppError
from core.notifications import create_notification
from core.product_models import LeadRecord
from core.security_models import UserAccount
from core.tenant_models import WorkspaceMembership
from core.tenancy import WorkspaceContext, require_workspace_permission

LEAD_STATUSES = frozenset(
    {"new", "qualified", "contacted", "proposal", "won", "lost", "spam", "archived"}
)
LEAD_PRIORITIES = frozenset({"low", "medium", "high", "urgent"})
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")
_PHONE_RE = re.compile(r"^[0-9+() .-]{6,64}$")
_ENTERPRISE_TERMS = {
    "enterprise", "company", "team", "employees", "organization", "agency",
    "saas", "platform", "multiple stores", "multi vendor",
}
_BUYING_TERMS = {
    "budget", "quote", "proposal", "price", "cost", "hire", "build", "need",
    "launch", "contract", "timeline", "deadline",
}
_URGENCY_TERMS = {"urgent", "asap", "immediately", "this week", "today", "deadline"}
_SUPPORT_TERMS = {"bug", "broken", "error", "issue", "support", "refund", "complaint"}
_SPAM_TERMS = {"crypto giveaway", "guest post", "buy followers", "seo backlinks", "casino", "loan offer"}


def _clean_text(value: object, *, field: str, minimum: int = 0, maximum: int = 255) -> str:
    clean = " ".join(str(value or "").strip().split())
    if not minimum <= len(clean) <= maximum:
        raise ValidationAppError(f"{field} must contain between {minimum} and {maximum} characters.")
    return clean


def _safe_json(value: Any, *, field: str, maximum: int = 20_000) -> str:
    try:
        serialized = json.dumps(
            value if value is not None else {}, ensure_ascii=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise ValidationAppError(f"{field} must be JSON serializable.") from exc
    if len(serialized) > maximum:
        raise ValidationAppError(f"{field} is too large.")
    return serialized


def _normalize_source(value: object) -> str:
    source = re.sub(r"[^a-z0-9_.-]+", "-", str(value or "").strip().lower()).strip("-")
    if not _SOURCE_RE.fullmatch(source):
        raise ValidationAppError(
            "Lead source must contain lowercase letters, numbers, dots, hyphens, or underscores."
        )
    return source


def _normalize_phone(value: object | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    phone = str(value).strip()
    if not _PHONE_RE.fullmatch(phone):
        raise ValidationAppError("Phone number contains unsupported characters.")
    return phone


def classify_and_score(
    *, message: str, email: str | None, phone: str | None, company: str | None
) -> tuple[str, str, int, list[str]]:
    """Return classification, priority, score, and evidence tags without using AI."""
    haystack = " ".join(filter(None, [message, company])).casefold()
    tags: list[str] = []
    score = 10
    if any(term in haystack for term in _SPAM_TERMS):
        return "spam", "low", 0, ["spam_pattern"]
    classification = "support" if any(term in haystack for term in _SUPPORT_TERMS) else "sales"
    if classification == "support":
        tags.append("support_signal")
    enterprise_hits = sum(term in haystack for term in _ENTERPRISE_TERMS)
    buying_hits = sum(term in haystack for term in _BUYING_TERMS)
    urgency_hits = sum(term in haystack for term in _URGENCY_TERMS)
    if email:
        score += 15
        tags.append("email_present")
    if phone:
        score += 15
        tags.append("phone_present")
    if company:
        score += 10
        tags.append("company_present")
    if enterprise_hits:
        score += min(20, enterprise_hits * 5)
        tags.append("enterprise_signal")
    if buying_hits:
        score += min(25, buying_hits * 5)
        tags.append("buying_intent")
    if urgency_hits:
        score += min(15, urgency_hits * 5)
        tags.append("urgency_signal")
    if len(message.strip()) >= 120:
        score += 5
        tags.append("detailed_request")
    score = min(max(score, 0), 100)
    priority = "urgent" if score >= 80 or urgency_hits >= 2 else "high" if score >= 60 else "medium" if score >= 30 else "low"
    if classification == "support" and score < 60:
        priority = "medium"
    return classification, priority, score, sorted(set(tags))


def create_lead(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser | None,
    source: str,
    name: str,
    message: str,
    email: str | None = None,
    phone: str | None = None,
    company: str | None = None,
    external_id: str | None = None,
    metadata: dict | None = None,
) -> LeadRecord:
    if actor is not None:
        require_workspace_permission(context, "content:write")
    safe_source = _normalize_source(source)
    safe_name = _clean_text(name, field="Lead name", minimum=2, maximum=255)
    safe_message = str(message or "").strip()
    if not 1 <= len(safe_message) <= 50_000:
        raise ValidationAppError("Lead message must contain between 1 and 50,000 characters.")
    safe_email = normalize_email(email) if email and str(email).strip() else None
    safe_phone = _normalize_phone(phone)
    safe_company = _clean_text(company, field="Company", maximum=255) or None
    safe_external_id = _clean_text(external_id, field="External ID", maximum=255) or None

    if safe_external_id:
        existing = session.scalar(
            select(LeadRecord).where(
                LeadRecord.source == safe_source, LeadRecord.external_id == safe_external_id
            )
        )
        if existing:
            return existing
    elif safe_email:
        existing = session.scalar(
            select(LeadRecord)
            .where(
                func.lower(LeadRecord.email) == safe_email,
                LeadRecord.status.notin_(("lost", "spam", "archived")),
            )
            .order_by(LeadRecord.created_at.desc())
        )
        if existing and existing.message == safe_message:
            return existing

    classification, priority, score, evidence = classify_and_score(
        message=safe_message, email=safe_email, phone=safe_phone, company=safe_company
    )
    status = "spam" if classification == "spam" else "qualified" if score >= 60 else "new"
    model = LeadRecord(
        source=safe_source,
        external_id=safe_external_id,
        name=safe_name,
        email=safe_email,
        phone=safe_phone,
        company=safe_company,
        message=safe_message,
        classification=classification,
        status=status,
        priority=priority,
        score=score,
        tags_json=json.dumps(evidence, separators=(",", ":")),
        metadata_json=_safe_json(metadata or {}, field="Lead metadata"),
    )
    session.add(model)
    session.flush()
    log_audit_event(
        session,
        action="lead.created",
        actor_user_id=actor.id if actor else None,
        actor_email=actor.email if actor else None,
        resource_type="lead",
        resource_id=model.id,
        event_data={
            "source": safe_source,
            "classification": classification,
            "priority": priority,
            "score": score,
        },
    )
    if priority == "urgent" and status != "spam":
        create_notification(
            session,
            recipient_user_id=None,
            severity="warning",
            title="Urgent lead requires attention",
            message=f"{safe_name} scored {score}/100 from {safe_source}.",
            action_label="Open Leads",
            action_page="Leads",
            deduplication_key=f"urgent-lead:{model.id}",
            commit=False,
        )
    session.commit()
    session.refresh(model)
    return model


def list_leads(
    session: Session,
    *,
    context: WorkspaceContext,
    status: str | None = None,
    priority: str | None = None,
    assigned_user_id: int | None = None,
    search: str | None = None,
    limit: int = 300,
) -> list[LeadRecord]:
    require_workspace_permission(context, "workspace:read")
    query = select(LeadRecord)
    if status and status != "all":
        safe_status = str(status).strip().lower()
        if safe_status not in LEAD_STATUSES:
            raise ValidationAppError("Lead status filter is invalid.")
        query = query.where(LeadRecord.status == safe_status)
    if priority and priority != "all":
        safe_priority = str(priority).strip().lower()
        if safe_priority not in LEAD_PRIORITIES:
            raise ValidationAppError("Lead priority filter is invalid.")
        query = query.where(LeadRecord.priority == safe_priority)
    if assigned_user_id is not None:
        query = query.where(LeadRecord.assigned_user_id == int(assigned_user_id))
    if search and str(search).strip():
        value = f"%{str(search).strip()[:200]}%"
        query = query.where(
            or_(
                LeadRecord.name.ilike(value),
                LeadRecord.email.ilike(value),
                LeadRecord.company.ilike(value),
                LeadRecord.message.ilike(value),
            )
        )
    return list(
        session.scalars(
            query.order_by(LeadRecord.score.desc(), LeadRecord.created_at.desc()).limit(
                min(max(int(limit), 1), 1_000)
            )
        ).all()
    )


def list_assignable_members(
    session: Session, *, context: WorkspaceContext
) -> list[tuple[WorkspaceMembership, UserAccount]]:
    require_workspace_permission(context, "workspace:read")
    return list(
        session.execute(
            select(WorkspaceMembership, UserAccount)
            .join(UserAccount, UserAccount.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == context.workspace_id,
                WorkspaceMembership.status == "active",
                UserAccount.is_active.is_(True),
            )
            .order_by(UserAccount.display_name.asc())
        ).all()
    )


def update_lead(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    lead_id: int,
    status: str,
    priority: str,
    assigned_user_id: int | None = None,
) -> LeadRecord:
    require_workspace_permission(context, "content:write")
    safe_status = str(status or "").strip().lower()
    safe_priority = str(priority or "").strip().lower()
    if safe_status not in LEAD_STATUSES:
        raise ValidationAppError("Select a valid lead status.")
    if safe_priority not in LEAD_PRIORITIES:
        raise ValidationAppError("Select a valid lead priority.")
    if assigned_user_id is not None:
        membership = session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == context.workspace_id,
                WorkspaceMembership.user_id == int(assigned_user_id),
                WorkspaceMembership.status == "active",
            )
        )
        account = session.get(UserAccount, int(assigned_user_id))
        if membership is None or account is None or not account.is_active:
            raise ValidationAppError("Assigned user is not an active member of this workspace.")
    lead = session.get(LeadRecord, int(lead_id))
    if lead is None:
        raise ValidationAppError("Lead was not found.")
    lead.status = safe_status
    lead.priority = safe_priority
    lead.assigned_user_id = int(assigned_user_id) if assigned_user_id is not None else None
    if safe_status in {"contacted", "proposal", "won", "lost"}:
        lead.last_contacted_at = datetime.now(timezone.utc)
    log_audit_event(
        session,
        action="lead.updated",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="lead",
        resource_id=lead.id,
        event_data={
            "status": safe_status,
            "priority": safe_priority,
            "assigned_user_id": assigned_user_id,
        },
    )
    session.commit()
    session.refresh(lead)
    return lead
