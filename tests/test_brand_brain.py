"""Regression tests for Brand Brain ingestion, retrieval, and draft-only generation."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

import core.brand_brain as brand_brain
from core.auth import ROLE_OWNER, create_user
from core.brand_brain import (
    add_knowledge_document,
    extract_upload_text,
    generate_brand_draft,
    retrieve_knowledge,
)
from core.models import Post
from core.product_models import BrandKnowledgeDocument, UsageEvent
from core.tenant_migration import backfill_legacy_workspace
from core.tenant_runtime import bind_workspace
from core.tenancy import bootstrap_default_tenant, create_organization
from core.errors import ValidationAppError


def _tenant(db_session):
    owner = create_user(
        db_session,
        email="brand-owner@example.com",
        display_name="Brand Owner",
        password="BrandOwnerSecure!3582",
        role=ROLE_OWNER,
    )
    context = bootstrap_default_tenant(db_session, owner)
    backfill_legacy_workspace(db_session, context.workspace_id)
    bind_workspace(db_session, context)
    return owner, context


def test_knowledge_checksum_retrieval_and_workspace_isolation(db_session):
    owner, first = _tenant(db_session)
    document = add_knowledge_document(
        db_session,
        context=first,
        actor=owner,
        title="Dealzyro Product Brief",
        source_type="manual",
        content_text=(
            "Dealzyro is a multi-vendor commerce platform for merchants. "
            "It includes inventory, POS, invoicing, reporting, HR, and marketing automation."
        ),
    )
    duplicate = add_knowledge_document(
        db_session,
        context=first,
        actor=owner,
        title="Duplicate copy",
        source_type="manual",
        content_text=document.content_text,
    )
    assert duplicate.id == document.id
    matches = retrieve_knowledge(
        db_session,
        context=first,
        query="merchant inventory and POS platform",
    )
    assert matches
    assert matches[0].document_id == document.id

    second = create_organization(
        db_session,
        actor=owner,
        name="Second Brand Organization",
        slug="second-brand-organization",
        workspace_name="Second Brand",
        workspace_slug="second-brand",
    )
    bind_workspace(db_session, second)
    assert retrieve_knowledge(
        db_session,
        context=second,
        query="merchant inventory and POS platform",
    ) == []
    assert db_session.scalar(select(BrandKnowledgeDocument.id)) is None


def test_upload_validation_rejects_forged_pdf_and_parses_json():
    with pytest.raises(ValidationAppError, match="signature"):
        extract_upload_text("fake.pdf", b"not-a-real-pdf")
    text, source_type = extract_upload_text(
        "knowledge.json", json.dumps({"service": "SaaS development"}).encode("utf-8")
    )
    assert source_type == "json"
    assert "SaaS development" in text


def test_brand_brain_generation_is_draft_only_and_metered(db_session, monkeypatch):
    owner, context = _tenant(db_session)
    add_knowledge_document(
        db_session,
        context=context,
        actor=owner,
        title="Artixcore Services",
        source_type="manual",
        content_text=(
            "Artixcore builds secure SaaS platforms, business software, automation, "
            "web applications, and mobile applications for companies."
        ),
    )
    monkeypatch.setattr(
        brand_brain,
        "_call_provider",
        lambda _system, _user: (
            "openai",
            "test-model",
            json.dumps(
                {
                    "content": "Build secure business software with Artixcore. Book a consultation.",
                    "hashtags": ["Artixcore", "SaaS"],
                    "quality_notes": "Grounded in the workspace service brief.",
                }
            ),
            120,
            40,
        ),
    )
    result = generate_brand_draft(
        db_session,
        context=context,
        actor=owner,
        platform="linkedin",
        topic="Secure SaaS development",
        goal="Generate qualified conversations",
        tone="Professional",
        language="English",
        cta="Book a consultation",
    )
    assert result.post.status == "draft"
    assert result.post.published_at is None
    assert result.post.approved_at is None
    assert result.matches
    saved = db_session.get(Post, result.post.id)
    assert saved.status == "draft"
    usage = db_session.scalar(
        select(UsageEvent).where(UsageEvent.event_type == "brand_brain.generation")
    )
    assert usage is not None
    assert usage.quantity == 160
