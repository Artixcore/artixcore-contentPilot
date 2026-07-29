"""Regression tests for campaigns, templates, analytics, and lead intelligence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.analytics_service import get_analytics_summary, record_usage_event
from core.auth import ROLE_OWNER, ROLE_VIEWER, create_user
from core.campaign_service import (
    add_campaign_item,
    create_campaign,
    create_template,
    list_calendar_items,
)
from core.lead_service import create_lead, update_lead
from core.models import Post, PostAnalytics
from core.tenant_migration import backfill_legacy_workspace
from core.tenant_runtime import bind_workspace
from core.tenancy import bootstrap_default_tenant
from core.errors import ValidationAppError


def _tenant(db_session):
    owner = create_user(
        db_session,
        email="product-owner@example.com",
        display_name="Product Owner",
        password="ProductOwnerSecure!6941",
        role=ROLE_OWNER,
    )
    context = bootstrap_default_tenant(db_session, owner)
    backfill_legacy_workspace(db_session, context.workspace_id)
    bind_workspace(db_session, context)
    return owner, context


def test_template_campaign_and_calendar_validation(db_session):
    owner, context = _tenant(db_session)
    template = create_template(
        db_session,
        context=context,
        actor=owner,
        name="Launch announcement",
        platform="linkedin",
        category="launch",
        body_template="Introduce the product, explain the outcome, and invite a consultation.",
        hashtags=["SaaS", "Artixcore"],
        default_cta="Book a consultation.",
    )
    assert template.workspace_id == context.workspace_id
    now = datetime.now(timezone.utc)
    campaign = create_campaign(
        db_session,
        context=context,
        actor=owner,
        name="Q3 Launch",
        goal="Generate qualified product conversations.",
        description="A controlled product launch campaign.",
        platforms=["linkedin", "facebook"],
        start_date=now,
        end_date=now + timedelta(days=30),
        posts_per_week=4,
    )
    item = add_campaign_item(
        db_session,
        context=context,
        actor=owner,
        campaign_id=campaign.id,
        title="Launch post",
        platform="linkedin",
        content_type="post",
        brief="Explain the product value without unverifiable claims.",
        scheduled_at=now + timedelta(days=1),
    )
    assert item.status == "scheduled"
    assert list_calendar_items(db_session, campaign_id=campaign.id)[0].id == item.id

    with pytest.raises(ValidationAppError, match="already scheduled"):
        add_campaign_item(
            db_session,
            context=context,
            actor=owner,
            campaign_id=campaign.id,
            title="Collision",
            platform="linkedin",
            content_type="post",
            scheduled_at=now + timedelta(days=1),
        )


def test_lead_scoring_assignment_and_analytics(db_session):
    owner, context = _tenant(db_session)
    viewer = create_user(
        db_session,
        email="lead-viewer@example.com",
        display_name="Lead Viewer",
        password="LeadViewerSecure!6941",
        role=ROLE_VIEWER,
        actor=owner,
    )
    lead = create_lead(
        db_session,
        context=context,
        actor=owner,
        source="website",
        name="Enterprise Buyer",
        email="buyer@example.com",
        phone="+880 1700 000000",
        company="Example Enterprise",
        message=(
            "We need a SaaS platform for our team and want a proposal, budget, timeline, "
            "and launch plan this week. Please contact us urgently."
        ),
    )
    assert lead.score >= 60
    assert lead.priority in {"high", "urgent"}
    assert lead.status == "qualified"

    with pytest.raises(ValidationAppError, match="active member"):
        update_lead(
            db_session,
            context=context,
            actor=owner,
            lead_id=lead.id,
            status="contacted",
            priority="high",
            assigned_user_id=viewer.id,
        )

    post = Post(
        platform="linkedin",
        topic="Analytics post",
        content="Published workspace content.",
        status="published",
    )
    db_session.add(post)
    db_session.flush()
    db_session.add(
        PostAnalytics(
            post_id=post.id,
            platform="linkedin",
            impressions=1_000,
            reach=700,
            likes=70,
            comments=10,
            shares=20,
            clicks=50,
            captured_at=datetime.now(timezone.utc),
        )
    )
    record_usage_event(
        db_session,
        context=context,
        event_type="test.usage",
        quantity=100,
        unit_cost=0.001,
        actor_user_id=owner.id,
    )
    summary = get_analytics_summary(
        db_session,
        context=context,
        start=datetime.now(timezone.utc) - timedelta(days=1),
        end=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    assert summary.posts_published == 1
    assert summary.total_impressions == 1_000
    assert summary.total_engagements == 100
    assert summary.engagement_rate == 10.0
    assert summary.total_clicks == 50
    assert summary.leads_created == 1
    assert summary.usage_quantity == 100
