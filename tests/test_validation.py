"""Security regression tests for input, URL, schema, and upload validation."""

import pytest
from pydantic import ValidationError

from core.errors import FileUploadError, ValidationAppError
from core.schemas import BrandProfileUpdate, CampaignCreate, PostCreate
from core.validation import (
    normalize_text,
    sanitize_filename,
    validate_http_url,
    validate_upload,
)


def test_normalize_text_rejects_control_characters():
    with pytest.raises(ValidationAppError):
        normalize_text("safe\x00unsafe", field="Message", max_length=100)


def test_normalize_text_enforces_length_limit():
    with pytest.raises(ValidationAppError):
        normalize_text("x" * 11, field="Topic", max_length=10)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1/internal",
        "http://10.0.0.1/internal",
        "http://169.254.169.254/latest/meta-data",
        "https://user:password@example.com/path",
    ],
)
def test_url_validation_blocks_unsafe_destinations(url):
    with pytest.raises(ValidationAppError):
        validate_http_url(url)


def test_url_validation_accepts_public_https_url():
    assert validate_http_url("https://artixcore.com/about") == "https://artixcore.com/about"


def test_filename_validation_blocks_path_traversal():
    with pytest.raises(FileUploadError):
        sanitize_filename("../secret.txt")
    with pytest.raises(FileUploadError):
        sanitize_filename("..\\secret.txt")


def test_upload_validation_accepts_matching_pdf_signature():
    result = validate_upload(
        filename="report.pdf",
        content=b"%PDF-1.7\ncontent",
        content_type="application/pdf",
    )
    assert result.filename == "report.pdf"
    assert result.extension == ".pdf"


def test_upload_validation_rejects_forged_extension():
    with pytest.raises(FileUploadError):
        validate_upload(
            filename="malware.pdf",
            content=b"MZ-not-a-pdf",
            content_type="application/pdf",
        )


def test_upload_validation_rejects_mime_mismatch():
    with pytest.raises(FileUploadError):
        validate_upload(
            filename="image.png",
            content=b"\x89PNG\r\n\x1a\nrest",
            content_type="application/pdf",
        )


def test_post_schema_rejects_unsupported_platform_and_oversized_topic():
    with pytest.raises(ValidationError):
        PostCreate(platform="unknown", topic="Valid topic")
    with pytest.raises((ValidationError, ValidationAppError)):
        PostCreate(platform="linkedin", topic="x" * 501)


def test_brand_schema_rejects_private_website_url():
    payload = {
        "company_name": "Artixcore",
        "page_name": "Artixcore",
        "website_url": "http://127.0.0.1/admin",
        "description": "Software company",
        "tone": "Professional",
        "target_audience": "Businesses",
        "services": "Software development",
        "preferred_cta": "Contact us",
        "forbidden_style": "No misleading claims",
    }
    with pytest.raises((ValidationError, ValidationAppError)):
        BrandProfileUpdate(**payload)


def test_campaign_schema_rejects_reversed_date_range():
    with pytest.raises(ValidationError):
        CampaignCreate(
            name="Launch",
            start_date="2026-08-10T00:00:00Z",
            end_date="2026-08-01T00:00:00Z",
        )
