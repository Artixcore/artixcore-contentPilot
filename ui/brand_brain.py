"""Brand Brain knowledge ingestion, retrieval, and draft-generation interface."""

from __future__ import annotations

import streamlit as st
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser
from core.brand_brain import (
    add_knowledge_document,
    add_uploaded_document,
    archive_knowledge_document,
    generate_brand_draft,
    list_knowledge_documents,
    retrieve_knowledge,
)
from core.error_handler import handle_exception
from core.models import PLATFORMS
from core.tenancy import WorkspaceContext
from ui.notifications import show_error_from_dict, show_success


def _handle(exc: Exception, context: str) -> None:
    show_error_from_dict(handle_exception(exc, context=context))


def render_brand_brain(
    session: Session,
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
) -> None:
    st.header("Brand Brain")
    st.caption(
        "Workspace knowledge is isolated, checksummed, retrieved deterministically, and treated as untrusted reference material. Generated content is always saved as a draft for human review."
    )

    knowledge_tab, search_tab, generate_tab = st.tabs(
        ["Knowledge", "Retrieve", "Generate Draft"]
    )

    with knowledge_tab:
        documents = list_knowledge_documents(
            session, context=workspace, include_archived=True
        )
        if workspace.can("content:write"):
            paste_tab, upload_tab = st.tabs(["Paste Text", "Upload File"])
            with paste_tab:
                with st.form("brand_brain_paste_form"):
                    title = st.text_input("Title", max_chars=255)
                    source_type = st.text_input(
                        "Source type", value="manual", max_chars=100
                    )
                    source_reference = st.text_input(
                        "Source reference", max_chars=1024
                    )
                    content = st.text_area(
                        "Knowledge content", max_chars=2_000_000, height=260
                    )
                    add_text = st.form_submit_button(
                        "Add knowledge", type="primary"
                    )
                if add_text:
                    try:
                        add_knowledge_document(
                            session,
                            context=workspace,
                            actor=user,
                            title=title,
                            source_type=source_type,
                            source_reference=source_reference or None,
                            content_text=content,
                        )
                        show_success("Knowledge document added.")
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        _handle(exc, "brand_brain.add_text")

            with upload_tab:
                uploaded = st.file_uploader(
                    "Upload PDF, TXT, MD, CSV, or JSON",
                    type=["pdf", "txt", "md", "csv", "json"],
                    accept_multiple_files=False,
                    key="brand_brain_upload",
                )
                upload_title = st.text_input(
                    "Optional document title", max_chars=255
                )
                if st.button("Process uploaded document", type="primary"):
                    if uploaded is None:
                        st.warning("Select a document first.")
                    else:
                        try:
                            add_uploaded_document(
                                session,
                                context=workspace,
                                actor=user,
                                filename=uploaded.name,
                                content=uploaded.getvalue(),
                                title=upload_title or None,
                            )
                            show_success("Uploaded document added to Brand Brain.")
                            st.rerun()
                        except Exception as exc:
                            session.rollback()
                            _handle(exc, "brand_brain.upload")

        st.subheader("Workspace knowledge")
        if not documents:
            st.info("No knowledge documents have been added.")
        for document in documents:
            with st.container(border=True):
                columns = st.columns([4, 2, 1])
                columns[0].markdown(f"**{document.title}**")
                columns[0].caption(
                    f"{document.source_type} · {len(document.content_text):,} characters"
                )
                columns[1].write(document.status.title())
                if (
                    document.status == "active"
                    and workspace.can("content:write")
                    and columns[2].button(
                        "Archive", key=f"knowledge_archive_{document.id}"
                    )
                ):
                    try:
                        archive_knowledge_document(
                            session,
                            context=workspace,
                            actor=user,
                            document_id=document.id,
                        )
                        show_success("Knowledge document archived.")
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        _handle(exc, "brand_brain.archive")
                with st.expander("Preview"):
                    st.text(document.content_text[:5_000])

    with search_tab:
        query = st.text_input(
            "Knowledge query", max_chars=2_000, key="brand_brain_query"
        )
        if st.button("Retrieve relevant knowledge"):
            try:
                matches = retrieve_knowledge(
                    session, context=workspace, query=query, limit=10
                )
                if not matches:
                    st.info("No relevant knowledge matches were found.")
                for match in matches:
                    with st.container(border=True):
                        st.markdown(f"**{match.title}**")
                        st.caption(
                            f"{match.source_type} · score {match.score:.4f} · document #{match.document_id}"
                        )
                        st.text(match.excerpt)
            except Exception as exc:
                session.rollback()
                _handle(exc, "brand_brain.retrieve")

    with generate_tab:
        if not workspace.can("content:write"):
            st.info("Your workspace role cannot generate drafts.")
            return
        with st.form("brand_brain_generate_form"):
            platform = st.selectbox("Platform", list(PLATFORMS))
            topic = st.text_area("Topic or request", max_chars=2_000)
            goal = st.text_input("Goal", max_chars=1_000)
            tone = st.text_input(
                "Tone", value="Professional and trustworthy", max_chars=500
            )
            language = st.text_input("Language", value="English", max_chars=100)
            cta = st.text_input("Call to action", max_chars=512)
            generate = st.form_submit_button(
                "Generate review draft", type="primary"
            )
        if generate:
            try:
                result = generate_brand_draft(
                    session,
                    context=workspace,
                    actor=user,
                    platform=platform,
                    topic=topic,
                    goal=goal,
                    tone=tone,
                    language=language,
                    cta=cta,
                )
                st.session_state["brand_brain_generated_post_id"] = result.post.id
                show_success(
                    f"Draft #{result.post.id} created. It was not approved, scheduled, or published."
                )
                st.subheader("Generated draft")
                st.write(result.post.content)
                if result.matches:
                    st.caption(
                        "Knowledge used: "
                        + ", ".join(match.title for match in result.matches)
                    )
            except Exception as exc:
                session.rollback()
                _handle(exc, "brand_brain.generate")

        generated_id = st.session_state.get("brand_brain_generated_post_id")
        if generated_id:
            st.info(
                f"Draft #{generated_id} is available in Approvals. Brand Brain never auto-publishes generated content."
            )
