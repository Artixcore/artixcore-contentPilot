"""Workspace lead-intelligence and pipeline interface."""

from __future__ import annotations

import streamlit as st
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser
from core.error_handler import handle_exception
from core.lead_service import (
    LEAD_PRIORITIES,
    LEAD_STATUSES,
    create_lead,
    list_assignable_members,
    list_leads,
    update_lead,
)
from core.tenancy import WorkspaceContext
from ui.notifications import show_error_from_dict, show_success


def _handle(exc: Exception, context: str) -> None:
    show_error_from_dict(handle_exception(exc, context=context))


def render_leads(
    session: Session,
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
) -> None:
    st.header("Lead Intelligence")
    st.caption(
        "Deterministic scoring identifies buying intent, enterprise signals, urgency, support requests, and common spam patterns."
    )

    intake_tab, pipeline_tab = st.tabs(["Add Lead", "Pipeline"])

    with intake_tab:
        if not workspace.can("content:write"):
            st.info("Your workspace role can view leads but cannot create or update them.")
        else:
            with st.form("lead_intake_form"):
                columns = st.columns(2)
                source = columns[0].text_input("Source", value="manual", max_chars=100)
                name = columns[1].text_input("Name", max_chars=255)
                email = columns[0].text_input("Email", max_chars=320)
                phone = columns[1].text_input("Phone", max_chars=64)
                company = columns[0].text_input("Company", max_chars=255)
                external_id = columns[1].text_input("External ID", max_chars=255)
                message = st.text_area("Message or inquiry", max_chars=50_000)
                submitted = st.form_submit_button("Create lead", type="primary")
            if submitted:
                try:
                    lead = create_lead(
                        session,
                        context=workspace,
                        actor=user,
                        source=source,
                        name=name,
                        message=message,
                        email=email or None,
                        phone=phone or None,
                        company=company or None,
                        external_id=external_id or None,
                    )
                    show_success(
                        f"Lead saved with score {lead.score}/100 and {lead.priority} priority."
                    )
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    _handle(exc, "lead.create")

    with pipeline_tab:
        filters = st.columns(3)
        status_filter = filters[0].selectbox(
            "Status", ["all", *sorted(LEAD_STATUSES)], key="lead_status_filter"
        )
        priority_filter = filters[1].selectbox(
            "Priority", ["all", *sorted(LEAD_PRIORITIES)], key="lead_priority_filter"
        )
        search = filters[2].text_input("Search", max_chars=200, key="lead_search")

        try:
            leads = list_leads(
                session,
                context=workspace,
                status=status_filter,
                priority=priority_filter,
                search=search,
            )
            members = list_assignable_members(session, context=workspace)
        except Exception as exc:
            session.rollback()
            _handle(exc, "lead.list")
            return

        member_options = {"Unassigned": None}
        for membership, account in members:
            member_options[f"{account.display_name} ({account.email})"] = membership.user_id

        metrics = st.columns(4)
        metrics[0].metric("Visible leads", len(leads))
        metrics[1].metric("Urgent", sum(item.priority == "urgent" for item in leads))
        metrics[2].metric("Qualified", sum(item.status == "qualified" for item in leads))
        metrics[3].metric("Won", sum(item.status == "won" for item in leads))

        if not leads:
            st.info("No leads match the selected filters.")
            return

        for lead in leads:
            with st.container(border=True):
                columns = st.columns([3, 1, 1, 1])
                columns[0].markdown(f"### {lead.name}")
                columns[0].caption(
                    " · ".join(
                        value for value in [lead.company, lead.email, lead.phone, lead.source] if value
                    )
                )
                columns[1].metric("Score", f"{lead.score}/100")
                columns[2].write(lead.classification.title())
                columns[3].write(lead.priority.title())
                st.write(lead.message)

                if workspace.can("content:write"):
                    edit_columns = st.columns(3)
                    status = edit_columns[0].selectbox(
                        "Lead status",
                        sorted(LEAD_STATUSES),
                        index=sorted(LEAD_STATUSES).index(lead.status),
                        key=f"lead_status_{lead.id}",
                    )
                    priority = edit_columns[1].selectbox(
                        "Lead priority",
                        sorted(LEAD_PRIORITIES),
                        index=sorted(LEAD_PRIORITIES).index(lead.priority),
                        key=f"lead_priority_{lead.id}",
                    )
                    current_member_label = next(
                        (
                            label
                            for label, user_id in member_options.items()
                            if user_id == lead.assigned_user_id
                        ),
                        "Unassigned",
                    )
                    assignee = edit_columns[2].selectbox(
                        "Assignee",
                        list(member_options),
                        index=list(member_options).index(current_member_label),
                        key=f"lead_assignee_{lead.id}",
                    )
                    if st.button("Save lead", key=f"lead_save_{lead.id}", type="primary"):
                        try:
                            update_lead(
                                session,
                                context=workspace,
                                actor=user,
                                lead_id=lead.id,
                                status=status,
                                priority=priority,
                                assigned_user_id=member_options[assignee],
                            )
                            show_success("Lead updated.")
                            st.rerun()
                        except Exception as exc:
                            session.rollback()
                            _handle(exc, "lead.update")
