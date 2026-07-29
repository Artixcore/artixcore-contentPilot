"""Campaign, content-template, and publishing-calendar interface."""

from __future__ import annotations

from datetime import datetime, time, timezone

import streamlit as st
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser
from core.campaign_service import (
    add_campaign_item,
    archive_template,
    create_campaign,
    create_template,
    list_calendar_items,
    list_campaigns,
    list_templates,
    update_campaign_item_status,
    update_campaign_status,
)
from core.error_handler import handle_exception
from core.models import PLATFORMS
from core.tenancy import WorkspaceContext
from ui.notifications import show_error_from_dict, show_success

_CAMPAIGN_STATUSES = ["draft", "active", "paused", "completed", "archived"]
_ITEM_STATUSES = [
    "planned",
    "draft",
    "pending_approval",
    "approved",
    "scheduled",
    "published",
    "cancelled",
    "failed",
]
_CONTENT_TYPES = ["post", "article", "carousel", "video", "story", "email", "ad"]


def _handle(exc: Exception, context: str) -> None:
    show_error_from_dict(handle_exception(exc, context=context))


def _combine_date_time(date_value, time_value) -> datetime:
    return datetime.combine(date_value, time_value, tzinfo=timezone.utc)


def render_campaigns(
    session: Session,
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
) -> None:
    st.header("Campaigns and Calendar")
    st.caption(f"Workspace: {workspace.workspace_name}")

    campaigns_tab, calendar_tab, templates_tab = st.tabs(
        ["Campaigns", "Calendar", "Templates"]
    )

    with campaigns_tab:
        campaigns = list_campaigns(session, include_archived=True)
        if workspace.can("content:write"):
            with st.expander("Create campaign", expanded=not campaigns):
                with st.form("create_campaign_form"):
                    name = st.text_input("Campaign name", max_chars=255)
                    goal = st.text_input("Goal", max_chars=512)
                    description = st.text_area("Description", max_chars=20_000)
                    platforms = st.multiselect("Platforms", list(PLATFORMS))
                    start_date = st.date_input("Start date")
                    end_date = st.date_input("End date")
                    posts_per_week = st.number_input(
                        "Posts per week", min_value=1, max_value=100, value=3
                    )
                    submitted = st.form_submit_button("Create campaign", type="primary")
                if submitted:
                    try:
                        create_campaign(
                            session,
                            context=workspace,
                            actor=user,
                            name=name,
                            goal=goal,
                            description=description,
                            platforms=platforms,
                            start_date=_combine_date_time(start_date, time.min),
                            end_date=_combine_date_time(end_date, time.max),
                            posts_per_week=int(posts_per_week),
                        )
                        show_success("Campaign created.")
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        _handle(exc, "campaign.create")

        for campaign in campaigns:
            with st.container(border=True):
                columns = st.columns([4, 2, 2])
                columns[0].markdown(f"### {campaign.name}")
                columns[0].caption(campaign.goal or "No goal provided")
                columns[1].write(campaign.status.replace("_", " ").title())
                columns[2].write(f"{campaign.posts_per_week} posts/week")
                if workspace.can("content:write"):
                    status = st.selectbox(
                        "Campaign status",
                        _CAMPAIGN_STATUSES,
                        index=_CAMPAIGN_STATUSES.index(campaign.status),
                        key=f"campaign_status_{campaign.id}",
                    )
                    if st.button("Update status", key=f"campaign_update_{campaign.id}"):
                        try:
                            update_campaign_status(
                                session,
                                context=workspace,
                                actor=user,
                                campaign_id=campaign.id,
                                status=status,
                            )
                            show_success("Campaign status updated.")
                            st.rerun()
                        except Exception as exc:
                            session.rollback()
                            _handle(exc, "campaign.status")

                    with st.expander("Add calendar item"):
                        with st.form(f"campaign_item_form_{campaign.id}"):
                            item_title = st.text_input("Title", max_chars=255)
                            platform = st.selectbox("Platform", list(PLATFORMS))
                            content_type = st.selectbox("Content type", _CONTENT_TYPES)
                            brief = st.text_area("Brief", max_chars=20_000)
                            schedule_enabled = st.checkbox("Schedule now")
                            scheduled_date = st.date_input("Date", key=f"item_date_{campaign.id}")
                            scheduled_time = st.time_input(
                                "Time", value=time(hour=9), key=f"item_time_{campaign.id}"
                            )
                            add_item = st.form_submit_button("Add item", type="primary")
                        if add_item:
                            try:
                                add_campaign_item(
                                    session,
                                    context=workspace,
                                    actor=user,
                                    campaign_id=campaign.id,
                                    title=item_title,
                                    platform=platform,
                                    content_type=content_type,
                                    brief=brief,
                                    scheduled_at=(
                                        _combine_date_time(scheduled_date, scheduled_time)
                                        if schedule_enabled
                                        else None
                                    ),
                                )
                                show_success("Calendar item added.")
                                st.rerun()
                            except Exception as exc:
                                session.rollback()
                                _handle(exc, "campaign.item.create")

    with calendar_tab:
        items = list_calendar_items(session)
        if not items:
            st.info("No calendar items are planned yet.")
        for item in items:
            with st.container(border=True):
                columns = st.columns([3, 2, 2, 2])
                columns[0].markdown(f"**{item.title}**")
                columns[0].caption(item.brief[:240] if item.brief else "No brief")
                columns[1].write(item.platform.title())
                columns[2].write(item.scheduled_at or "Unscheduled")
                columns[3].write(item.status.replace("_", " ").title())
                if workspace.can("content:write"):
                    new_status = st.selectbox(
                        "Status",
                        _ITEM_STATUSES,
                        index=_ITEM_STATUSES.index(item.status),
                        key=f"calendar_status_{item.id}",
                        label_visibility="collapsed",
                    )
                    if st.button("Save item status", key=f"calendar_save_{item.id}"):
                        try:
                            update_campaign_item_status(
                                session,
                                context=workspace,
                                actor=user,
                                item_id=item.id,
                                status=new_status,
                            )
                            show_success("Calendar item updated.")
                            st.rerun()
                        except Exception as exc:
                            session.rollback()
                            _handle(exc, "campaign.item.status")

    with templates_tab:
        templates = list_templates(session, active_only=False)
        if workspace.can("content:write"):
            with st.expander("Create template", expanded=not templates):
                with st.form("create_template_form"):
                    template_name = st.text_input("Template name", max_chars=255)
                    template_platform = st.selectbox("Platform", list(PLATFORMS))
                    category = st.text_input("Category", value="general", max_chars=100)
                    body = st.text_area("Template body", max_chars=50_000)
                    hashtags_text = st.text_input(
                        "Default hashtags", help="Comma-separated without needing the # symbol."
                    )
                    default_cta = st.text_input("Default CTA", max_chars=512)
                    create_button = st.form_submit_button("Create template", type="primary")
                if create_button:
                    try:
                        hashtags = [
                            value.strip().lstrip("#")
                            for value in hashtags_text.split(",")
                            if value.strip()
                        ]
                        create_template(
                            session,
                            context=workspace,
                            actor=user,
                            name=template_name,
                            platform=template_platform,
                            category=category,
                            body_template=body,
                            hashtags=hashtags,
                            default_cta=default_cta,
                        )
                        show_success("Template created.")
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        _handle(exc, "template.create")

        for template in templates:
            with st.container(border=True):
                st.markdown(f"**{template.name}**")
                st.caption(
                    f"{template.platform.title()} · {template.category.title()} · {template.status.title()}"
                )
                st.code(template.body_template[:2_000], language=None)
                if template.status == "active" and workspace.can("content:write"):
                    if st.button("Archive", key=f"template_archive_{template.id}"):
                        try:
                            archive_template(
                                session,
                                context=workspace,
                                actor=user,
                                template_id=template.id,
                            )
                            show_success("Template archived.")
                            st.rerun()
                        except Exception as exc:
                            session.rollback()
                            _handle(exc, "template.archive")


def render(session: Session) -> None:
    """Compatibility wrapper for older direct imports.

    The authenticated application uses render_campaigns so workspace context is
    always available. This wrapper fails closed rather than bypassing tenancy.
    """
    raise RuntimeError("Campaigns must be rendered through the authenticated workspace route.")
