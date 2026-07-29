"""Focused replay and payload-substitution tests for webhook receipts."""

import pytest

from core.errors import ValidationAppError
from core.webhooks import record_webhook_receipt


def test_reused_event_id_with_different_payload_is_rejected(db_session):
    record_webhook_receipt(
        db_session,
        provider="linkedin",
        event_id="evt-payload-binding",
        event_type="message.created",
        body=b'{"message":"original"}',
        signature_valid=True,
    )

    with pytest.raises(ValidationAppError, match="different payload"):
        record_webhook_receipt(
            db_session,
            provider="linkedin",
            event_id="evt-payload-binding",
            event_type="message.created",
            body=b'{"message":"substituted"}',
            signature_valid=True,
        )
