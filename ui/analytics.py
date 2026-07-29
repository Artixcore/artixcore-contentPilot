"""Workspace analytics dashboard."""

from __future__ import annotations

from datetime import datetime, time, timezone

import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session

from core.analytics_service import get_analytics_summary, get_platform_breakdown
from core.error_handler import handle_exception
from core.tenancy import WorkspaceContext
from ui.notifications import show_error_from_dict


def _datetime_at(date_value, time_value: time) -> datetime:
    return datetime.combine(date_value, time_value, tzinfo=timezone.utc)


def render_analytics(session: Session, workspace: WorkspaceContext) -> None:
    st.header("Analytics")
    st.caption(f"Workspace metrics for {workspace.workspace_name}")

    filters = st.columns(2)
    start_date = filters[0].date_input("Start date", key="analytics_start")
    end_date = filters[1].date_input("End date", key="analytics_end")
    start = _datetime_at(start_date, time.min)
    end = _datetime_at(end_date, time.max)

    try:
        summary = get_analytics_summary(
            session,
            context=workspace,
            start=start,
            end=end,
        )
        breakdown = get_platform_breakdown(
            session,
            context=workspace,
            start=start,
            end=end,
        )
    except Exception as exc:
        session.rollback()
        show_error_from_dict(handle_exception(exc, context="analytics.dashboard"))
        return

    first = st.columns(5)
    first[0].metric("Posts", summary.posts_created)
    first[1].metric("Published", summary.posts_published)
    first[2].metric("Scheduled", summary.scheduled_items)
    first[3].metric("Active campaigns", summary.active_campaigns)
    first[4].metric("Failed posts", summary.posts_failed)

    second = st.columns(5)
    second[0].metric("Impressions", summary.total_impressions)
    second[1].metric("Reach", summary.total_reach)
    second[2].metric("Engagements", summary.total_engagements)
    second[3].metric("Engagement rate", f"{summary.engagement_rate:.2f}%")
    second[4].metric("CTR", f"{summary.click_through_rate:.2f}%")

    third = st.columns(5)
    third[0].metric("Leads", summary.leads_created)
    third[1].metric("Qualified", summary.qualified_leads)
    third[2].metric("Won", summary.won_leads)
    third[3].metric("Lead conversion", f"{summary.lead_conversion_rate:.2f}%")
    third[4].metric("Job success", f"{summary.job_success_rate:.2f}%")

    st.subheader("Operational usage")
    usage = st.columns(3)
    usage[0].metric("Successful jobs", summary.jobs_succeeded)
    usage[1].metric("Failed jobs", summary.jobs_failed)
    usage[2].metric("Estimated cost", f"${summary.estimated_cost:,.4f}")
    st.caption(f"Recorded usage quantity: {summary.usage_quantity:,}")

    st.subheader("Platform performance")
    if breakdown:
        frame = pd.DataFrame(breakdown)
        st.dataframe(frame, use_container_width=True, hide_index=True)
        chart_data = frame.set_index("platform")[["impressions", "reach", "engagements", "clicks"]]
        st.bar_chart(chart_data)
    else:
        st.info("No platform analytics have been recorded for the selected period.")
