"""Artixcore chatbot AI agent."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from chatbot.memory import build_input_prompt, load_conversation_history
from chatbot.personality import build_system_prompt
from chatbot.safety import run_safety_check
from chatbot.schemas import ChatReply
from core.chat_database import (
    get_chatbot_settings,
    get_message,
    reject_message,
    save_draft_reply,
    save_manual_reply,
)
from core.chat_events import log_chat_event
from core.database import get_brand_profile
from core.models import ChatConversation, ChatMessage
from core.errors import ChatbotError, ValidationAppError
from core.rate_limiter import check_rate_limit
from core.router import ProviderRouter

logger = logging.getLogger(__name__)

CHATBOT_PROVIDER_UNAVAILABLE_MSG = (
    "Please provide a valid OpenAI or Anthropic API key to use Artixcore ContentPilot Chatbot."
)


class ChatbotAgentError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ArtixcoreChatbotAgent:
    def __init__(self, session: Session):
        self.session = session
        self.router = ProviderRouter(session=session)

    def _require_provider(self) -> None:
        if not self.router.has_any_provider():
            raise ChatbotError(
                CHATBOT_PROVIDER_UNAVAILABLE_MSG,
                error_code="PROVIDER_UNAVAILABLE",
                retryable=False,
            )

    def _within_business_hours(self, settings) -> bool:
        if not settings.business_hours_enabled:
            return True
        if not settings.business_hours_start or not settings.business_hours_end:
            return True
        try:
            start = datetime.strptime(settings.business_hours_start, "%H:%M").time()
            end = datetime.strptime(settings.business_hours_end, "%H:%M").time()
            now = datetime.now(timezone.utc).time()
            if start <= end:
                return start <= now <= end
            return now >= start or now <= end
        except ValueError:
            return True

    def generate_reply(
        self,
        platform: str,
        conversation_id: int,
        user_message: str,
        user_message_id: int,
        provider_mode: str = "auto",
        selected_provider: str | None = None,
        auto_send: bool = True,
    ) -> ChatReply:
        if not user_message or not user_message.strip():
            raise ValidationAppError("User message cannot be empty.")

        check_rate_limit("chatbot_reply", key=platform)
        self._require_provider()

        settings = get_chatbot_settings(self.session)
        if not settings:
            raise ChatbotError(
                "Chatbot settings not found.",
                user_action="Configure chatbot settings in Chat Control.",
            )

        brand = get_brand_profile(self.session)
        if not brand:
            raise ChatbotError(
                "Brand profile not found. Please configure brand settings first.",
                user_action="Open Brand Settings and save your profile.",
            )

        history = load_conversation_history(self.session, conversation_id)
        system_prompt = build_system_prompt(settings, brand, platform)
        input_prompt = build_input_prompt(user_message, history)

        try:
            result = self.router.generate(
                prompt=input_prompt,
                system_prompt=system_prompt,
                mode=provider_mode,
                selected_provider=selected_provider,
                task_type="chatbot_reply",
            )
        except Exception as exc:
            msg = getattr(exc, "message", str(exc))
            raise ChatbotError(msg, original_exception=exc) from exc

        draft_text = (result.text or "").strip()
        safety = run_safety_check(user_message, draft_text, settings)

        if not safety.passed:
            log_chat_event(
                self.session,
                conversation_id,
                "safety_blocked",
                {"notes": safety.notes},
                message_id=user_message_id,
            )

        if settings.approval_required or not safety.passed:
            reply_status = "pending_approval"
        else:
            reply_status = "draft"

        if safety.notes:
            log_chat_event(
                self.session,
                conversation_id,
                "safety_blocked" if not safety.passed else "safety_warning",
                {"notes": safety.notes},
                message_id=user_message_id,
            )

        msg = save_draft_reply(
            self.session,
            conversation_id=conversation_id,
            platform=platform,
            user_message_id=user_message_id,
            ai_text=draft_text,
            system_prompt=system_prompt,
            input_prompt=input_prompt,
            raw_ai_response=json.dumps({"text": draft_text}, ensure_ascii=False),
            provider_used=result.provider,
            model_used=result.model or "",
            safety_status=safety.status,
            safety_notes="; ".join(safety.notes) if safety.notes else None,
            reply_status=reply_status,
        )

        sent = False
        if (
            auto_send
            and settings.auto_reply_enabled
            and not settings.approval_required
            and safety.passed
            and self._within_business_hours(settings)
        ):
            from chatbot.channel_router import send_reply

            conv = self.session.get(ChatConversation, conversation_id)
            if conv and conv.status != "human_handoff":
                send_result = send_reply(self.session, platform, conversation_id, msg.id, draft_text)
                sent = send_result.get("success", False)
                if not sent:
                    msg.reply_status = "failed"
                    msg.safety_notes = (
                        f"{msg.safety_notes or ''}\nSend failed: {send_result.get('error', '')}"
                    ).strip()
                    self.session.flush()
                    logger.warning(
                        "Chatbot reply generated but send failed: conversation=%s error=%s",
                        conversation_id,
                        send_result.get("error"),
                    )

        return ChatReply(
            success=True,
            conversation_id=conversation_id,
            message_id=msg.id,
            user_message_id=user_message_id,
            draft_text=draft_text,
            reply_status=msg.reply_status,
            safety_passed=safety.passed,
            safety_notes="; ".join(safety.notes) if safety.notes else None,
            provider_used=result.provider,
            model_used=result.model,
            sent=sent,
        )

    def regenerate_reply(
        self,
        message_id: int,
        provider_mode: str = "auto",
        selected_provider: str | None = None,
    ) -> ChatReply:
        draft = get_message(self.session, message_id)
        if draft.sender_type != "bot":
            raise ChatbotAgentError("Only bot draft messages can be regenerated.")

        user_msg = (
            self.session.query(ChatMessage)
            .filter(
                ChatMessage.conversation_id == draft.conversation_id,
                ChatMessage.sender_type == "user",
                ChatMessage.id < draft.id,
            )
            .order_by(ChatMessage.id.desc())
            .first()
        )
        if not user_msg:
            raise ChatbotAgentError("Original user message not found for regeneration.")

        reject_message(self.session, message_id, reason="Regenerated")
        return self.generate_reply(
            platform=draft.platform,
            conversation_id=draft.conversation_id,
            user_message=user_msg.message_text,
            user_message_id=user_msg.id,
            provider_mode=provider_mode,
            selected_provider=selected_provider,
            auto_send=False,
        )

    def send_manual_reply(
        self,
        conversation_id: int,
        platform: str,
        text: str,
        sender_name: str = "Admin",
    ) -> int:
        if not text or not text.strip():
            raise ChatbotAgentError("Reply text cannot be empty.")
        msg = save_manual_reply(self.session, conversation_id, platform, text.strip(), sender_name)
        return msg.id
