"""Pydantic schemas for validation and API boundaries."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from core.validation import normalize_text, validate_hashtag, validate_http_url


class Platform(str, Enum):
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    LINKEDIN = "linkedin"
    TWITTER = "twitter"
    WEBSITE_BLOG = "website_blog"


class PostStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"


class ProviderMode(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"
    FALLBACK = "fallback"
    QUALITY = "quality"


class BrandProfileBase(BaseModel):
    company_name: str = Field(min_length=1, max_length=255)
    page_name: str = Field(min_length=1, max_length=255)
    website_url: str = Field(min_length=1, max_length=2048)
    description: str = Field(min_length=1, max_length=10_000)
    tone: str = Field(min_length=1, max_length=1_000)
    target_audience: str = Field(min_length=1, max_length=5_000)
    services: str = Field(min_length=1, max_length=10_000)
    preferred_cta: str = Field(min_length=1, max_length=2_000)
    forbidden_style: str = Field(min_length=1, max_length=5_000)

    @field_validator(
        "company_name",
        "page_name",
        "description",
        "tone",
        "target_audience",
        "services",
        "preferred_cta",
        "forbidden_style",
        mode="before",
    )
    @classmethod
    def normalize_brand_text(cls, value, info):
        limits = {
            "company_name": 255,
            "page_name": 255,
            "description": 10_000,
            "tone": 1_000,
            "target_audience": 5_000,
            "services": 10_000,
            "preferred_cta": 2_000,
            "forbidden_style": 5_000,
        }
        return normalize_text(
            value,
            field=info.field_name.replace("_", " ").title(),
            min_length=1,
            max_length=limits[info.field_name],
            allow_newlines=info.field_name not in {"company_name", "page_name"},
        )

    @field_validator("website_url", mode="before")
    @classmethod
    def validate_website(cls, value):
        return validate_http_url(value, field="Website URL")


class BrandProfileUpdate(BrandProfileBase):
    pass


class BrandProfileRead(BrandProfileBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class GeneratedPost(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    image_prompt: Optional[str] = Field(default=None, max_length=10_000)
    quality_notes: Optional[str] = Field(default=None, max_length=10_000)
    provider_used: str = Field(min_length=1, max_length=100)
    model_used: Optional[str] = Field(default=None, max_length=255)
    post_id: Optional[int] = Field(default=None, gt=0)
    platform: str
    topic: str = Field(min_length=1, max_length=500)
    saved: bool = True

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, value: str) -> str:
        return Platform(value).value

    @field_validator("hashtags", mode="before")
    @classmethod
    def validate_hashtags(cls, values):
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError("Hashtags must be provided as a list.")
        unique: list[str] = []
        for value in values[:30]:
            hashtag = validate_hashtag(value)
            if hashtag.casefold() not in {item.casefold() for item in unique}:
                unique.append(hashtag)
        return unique


class PostCreate(BaseModel):
    platform: str
    topic: str = Field(min_length=1, max_length=500)
    goal: str = Field(default="", max_length=2_000)
    tone: str = Field(default="", max_length=1_000)
    language: str = Field(default="English", min_length=1, max_length=100)
    cta: str = Field(default="", max_length=2_000)
    content: str = Field(default="", max_length=100_000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    image_prompt: Optional[str] = Field(default=None, max_length=10_000)
    status: str = "pending_approval"
    provider_used: Optional[str] = Field(default=None, max_length=100)
    model_used: Optional[str] = Field(default=None, max_length=255)
    quality_notes: Optional[str] = Field(default=None, max_length=10_000)

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, value: str) -> str:
        try:
            return Platform(str(value).strip().lower()).value
        except ValueError as exc:
            raise ValueError("Unsupported publishing platform.") from exc

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        try:
            return PostStatus(str(value).strip().lower()).value
        except ValueError as exc:
            raise ValueError("Unsupported post status.") from exc

    @field_validator("topic", mode="before")
    @classmethod
    def validate_topic(cls, value):
        return normalize_text(value, field="Topic", min_length=1, max_length=500)

    @field_validator("goal", "tone", "language", "cta", "content", mode="before")
    @classmethod
    def validate_text_fields(cls, value, info):
        limits = {"goal": 2_000, "tone": 1_000, "language": 100, "cta": 2_000, "content": 100_000}
        return normalize_text(
            value,
            field=info.field_name.title(),
            min_length=1 if info.field_name == "language" else 0,
            max_length=limits[info.field_name],
            allow_newlines=info.field_name != "language",
        )

    @field_validator("hashtags", mode="before")
    @classmethod
    def validate_hashtags(cls, values):
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError("Hashtags must be provided as a list.")
        unique: list[str] = []
        seen: set[str] = set()
        for value in values[:30]:
            hashtag = validate_hashtag(value)
            key = hashtag.casefold()
            if key not in seen:
                seen.add(key)
                unique.append(hashtag)
        return unique


class PostRead(BaseModel):
    id: int
    platform: str
    topic: str
    goal: str
    tone: str
    language: str
    cta: str
    content: str
    hashtags: str
    image_prompt: Optional[str] = None
    status: str
    provider_used: Optional[str] = None
    model_used: Optional[str] = None
    quality_notes: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PostUpdate(BaseModel):
    content: Optional[str] = Field(default=None, min_length=1, max_length=100_000)
    hashtags: Optional[list[str]] = Field(default=None, max_length=30)
    status: Optional[str] = None
    quality_notes: Optional[str] = Field(default=None, max_length=10_000)

    @field_validator("content", mode="before")
    @classmethod
    def validate_content(cls, value):
        if value is None:
            return None
        return normalize_text(value, field="Content", min_length=1, max_length=100_000)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is None:
            return None
        return PostStatus(str(value).strip().lower()).value

    @field_validator("hashtags", mode="before")
    @classmethod
    def validate_hashtags(cls, values):
        if values is None:
            return None
        if not isinstance(values, list):
            raise ValueError("Hashtags must be provided as a list.")
        return [validate_hashtag(value) for value in values[:30]]


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    goal: str = Field(default="", max_length=2_000)
    description: str = Field(default="", max_length=10_000)
    platforms: list[str] = Field(default_factory=list, max_length=5)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    posts_per_week: int = Field(default=3, ge=1, le=50)
    status: str = Field(default="draft", min_length=1, max_length=64)

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value):
        return normalize_text(value, field="Campaign name", min_length=1, max_length=255)

    @field_validator("goal", "description", mode="before")
    @classmethod
    def validate_campaign_text(cls, value, info):
        return normalize_text(
            value,
            field=info.field_name.title(),
            min_length=0,
            max_length=2_000 if info.field_name == "goal" else 10_000,
        )

    @field_validator("platforms", mode="before")
    @classmethod
    def validate_platforms(cls, values):
        if values is None:
            return []
        if not isinstance(values, list):
            raise ValueError("Platforms must be provided as a list.")
        result: list[str] = []
        for value in values:
            platform = Platform(str(value).strip().lower()).value
            if platform not in result:
                result.append(platform)
        return result

    @field_validator("status", mode="before")
    @classmethod
    def validate_campaign_status(cls, value):
        return normalize_text(
            value,
            field="Campaign status",
            min_length=1,
            max_length=64,
            allow_newlines=False,
        ).lower()

    @model_validator(mode="after")
    def validate_date_range(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("Campaign end date cannot be earlier than its start date.")
        return self


class CampaignRead(BaseModel):
    id: int
    name: str
    goal: str
    description: str
    platforms: str
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    posts_per_week: int
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProviderLogRead(BaseModel):
    id: int
    provider: str
    model: Optional[str] = None
    task_type: str
    success: bool
    latency_ms: Optional[int] = None
    error_message: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CampaignIdeas(BaseModel):
    ideas: list[str] = Field(default_factory=list, max_length=100)
    topics: list[str] = Field(default_factory=list, max_length=100)
    provider_used: str = Field(min_length=1, max_length=100)
    model_used: Optional[str] = Field(default=None, max_length=255)
