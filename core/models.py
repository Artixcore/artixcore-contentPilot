"""SQLAlchemy ORM models for ContentPilot."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.tenancy_base import TenantScopedMixin


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class BrandProfile(TenantScopedMixin, Base):
    __tablename__ = "brand_profiles"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_brand_profiles_workspace"),
        Index("ix_brand_profiles_workspace_updated", "workspace_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    page_name: Mapped[str] = mapped_column(String(255), nullable=False)
    website_url: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    tone: Mapped[str] = mapped_column(Text, nullable=False)
    target_audience: Mapped[str] = mapped_column(Text, nullable=False)
    services: Mapped[str] = mapped_column(Text, nullable=False)
    preferred_cta: Mapped[str] = mapped_column(Text, nullable=False)
    forbidden_style: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Post(TenantScopedMixin, Base):
    __tablename__ = "posts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','pending_approval','approved','scheduled','published','rejected','failed')",
            name="ck_posts_status",
        ),
        Index("ix_posts_workspace_status_created", "workspace_id", "status", "created_at"),
        Index("ix_posts_workspace_scheduled", "workspace_id", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    topic: Mapped[str] = mapped_column(String(512), nullable=False)
    goal: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    tone: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    language: Mapped[str] = mapped_column(String(50), nullable=False, default="English")
    cta: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    hashtags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    image_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft")
    provider_used: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quality_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    external_post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_post_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    published_by_platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    publish_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_ai_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_ai_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    generation_max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_input_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_output_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_estimate: Mapped[float | None] = mapped_column(Float, nullable=True)
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), nullable=True
    )
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    training_score: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Campaign(TenantScopedMixin, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','active','paused','completed','archived')",
            name="ck_campaigns_status",
        ),
        CheckConstraint("posts_per_week BETWEEN 1 AND 100", name="ck_campaigns_posts_per_week"),
        Index("ix_campaigns_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    platforms: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posts_per_week: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ProviderLog(TenantScopedMixin, Base):
    __tablename__ = "provider_logs"
    __table_args__ = (Index("ix_provider_logs_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False, default="generate")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_payload_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PublishingLog(TenantScopedMixin, Base):
    __tablename__ = "publishing_logs"
    __table_args__ = (Index("ix_publishing_logs_workspace_post", "workspace_id", "post_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    external_post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_post_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_payload_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TrainingExample(TenantScopedMixin, Base):
    __tablename__ = "training_examples"
    __table_args__ = (
        CheckConstraint("quality_score IS NULL OR quality_score BETWEEN 0 AND 100", name="ck_training_quality"),
        Index("ix_training_workspace_approval", "workspace_id", "approval_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    task_type: Mapped[str] = mapped_column(String(100), nullable=False, default="generate_post")
    input_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_edited_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    human_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engagement_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    used_for_training: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ContentEvent(TenantScopedMixin, Base):
    __tablename__ = "content_events"
    __table_args__ = (Index("ix_content_events_workspace_post", "workspace_id", "post_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    event_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SocialAccount(TenantScopedMixin, Base):
    __tablename__ = "social_accounts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "platform", "account_id", name="uq_social_accounts_workspace"),
        Index("ix_social_accounts_workspace_active", "workspace_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_token_encrypted_or_env_reference: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    refresh_token_encrypted_or_env_reference: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class PostAnalytics(TenantScopedMixin, Base):
    __tablename__ = "post_analytics"
    __table_args__ = (
        Index("ix_post_analytics_workspace_post", "workspace_id", "post_id"),
        CheckConstraint("impressions IS NULL OR impressions >= 0", name="ck_analytics_impressions"),
        CheckConstraint("reach IS NULL OR reach >= 0", name="ck_analytics_reach"),
        CheckConstraint("clicks IS NULL OR clicks >= 0", name="ck_analytics_clicks"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    external_post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    impressions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reach: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engagement_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ChatbotSettings(TenantScopedMixin, Base):
    __tablename__ = "chatbot_settings"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_chatbot_settings_workspace"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chatbot_name: Mapped[str] = mapped_column(String(255), nullable=False, default="Artixcore Assistant")
    personality_type: Mapped[str] = mapped_column(String(100), nullable=False, default="Professional Consultant")
    custom_personality_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    gender_style: Mapped[str] = mapped_column(String(50), nullable=False, default="Neutral")
    language: Mapped[str] = mapped_column(String(100), nullable=False, default="English")
    tone: Mapped[str] = mapped_column(String(100), nullable=False, default="Professional")
    reply_length: Mapped[str] = mapped_column(String(50), nullable=False, default="Medium")
    emoji_usage: Mapped[str] = mapped_column(String(50), nullable=False, default="Minimal")
    cta_style: Mapped[str] = mapped_column(String(100), nullable=False, default="Ask for project details")
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    human_handoff_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    blocked_keywords: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    business_hours_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    business_hours_start: Mapped[str | None] = mapped_column(String(10), nullable=True)
    business_hours_end: Mapped[str | None] = mapped_column(String(10), nullable=True)
    fallback_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ChatConversation(TenantScopedMixin, Base):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "platform", "platform_conversation_id", name="uq_chat_conversations_external"
        ),
        Index("ix_chat_conversations_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    platform_conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_platform_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_profile_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="open")
    assigned_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ChatMessage(TenantScopedMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_workspace_conversation", "workspace_id", "conversation_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    platform_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_type: Mapped[str] = mapped_column(String(50), nullable=False)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    message_type: Mapped[str] = mapped_column(String(50), nullable=False, default="text")
    ai_generated_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_status: Mapped[str] = mapped_column(String(50), nullable=False, default="incoming")
    provider_used: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_ai_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_ai_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    safety_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    safety_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ChatEvent(TenantScopedMixin, Base):
    __tablename__ = "chat_events"
    __table_args__ = (Index("ix_chat_events_workspace_conversation", "workspace_id", "conversation_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    event_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ChatTrainingExample(TenantScopedMixin, Base):
    __tablename__ = "chat_training_examples"
    __table_args__ = (Index("ix_chat_training_workspace_approval", "workspace_id", "approval_status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[int] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    user_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    human_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approval_status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    used_for_training: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class TelegramAdmin(TenantScopedMixin, Base):
    __tablename__ = "telegram_admins"
    __table_args__ = (
        UniqueConstraint("workspace_id", "telegram_user_id", name="uq_telegram_admin_workspace_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[str] = mapped_column(String(50), nullable=False)
    telegram_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class TelegramCommand(TenantScopedMixin, Base):
    __tablename__ = "telegram_commands"
    __table_args__ = (Index("ix_telegram_commands_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[str] = mapped_column(String(50), nullable=False)
    command: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


CHAT_CONVERSATION_STATUSES = (
    "open",
    "pending_approval",
    "replied",
    "human_handoff",
    "closed",
    "blocked",
)

CHAT_SENDER_TYPES = ("user", "bot", "human_admin", "system")

CHAT_REPLY_STATUSES = (
    "incoming",
    "draft",
    "pending_approval",
    "approved",
    "sent",
    "rejected",
    "failed",
)

CHAT_PLATFORMS = ("facebook", "linkedin", "twitter")

DEFAULT_CHATBOT_SETTINGS = {
    "chatbot_name": "Artixcore Assistant",
    "personality_type": "Professional Consultant",
    "gender_style": "Neutral",
    "language": "English",
    "tone": "Professional",
    "reply_length": "Medium",
    "emoji_usage": "Minimal",
    "cta_style": "Ask for project details",
    "auto_reply_enabled": False,
    "approval_required": True,
    "human_handoff_enabled": True,
    "blocked_keywords": "[]",
    "business_hours_enabled": False,
    "fallback_message": (
        "Thanks for reaching out to Artixcore. A team member will follow up with you shortly."
    ),
}

POST_STATUSES = (
    "draft",
    "pending_approval",
    "approved",
    "scheduled",
    "published",
    "rejected",
    "failed",
)

PLATFORMS = (
    "facebook",
    "instagram",
    "linkedin",
    "twitter",
    "website_blog",
)

PUBLISH_PLATFORMS = PLATFORMS

DEFAULT_BRAND = {
    "company_name": "Artixcore",
    "page_name": "Artixcore",
    "website_url": "https://artixcore.com",
    "description": (
        "Artixcore is a technology and software company that builds SaaS platforms, "
        "AI systems, websites, mobile applications, automation tools, and business "
        "software for modern companies."
    ),
    "tone": "Confident, professional, visionary, technical, trustworthy, founder-led.",
    "target_audience": (
        "Startup founders, SMEs, SaaS companies, local businesses, eCommerce brands, "
        "agencies, and enterprise clients."
    ),
    "services": (
        "SaaS development, web app development, mobile app development, AI automation, "
        "custom software, business dashboards, CRM, ERP, eCommerce systems, cloud "
        "deployment, API development."
    ),
    "preferred_cta": (
        "Book a consultation, visit Artixcore, start your software journey, "
        "build your next product with Artixcore."
    ),
    "forbidden_style": (
        "Avoid fake guarantees, exaggerated claims, spammy hashtags, childish AI hype, "
        "and overpromising."
    ),
}
