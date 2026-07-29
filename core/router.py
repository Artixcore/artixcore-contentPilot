"""Provider routing with fallback and logging."""

import json
import logging

from sqlalchemy.orm import Session

from core.models import ProviderLog
from core.utils import sanitize_payload
from providers import PROVIDER_UNAVAILABLE_MSG, get_all_providers
from providers.base import BaseAIProvider, GenerationResult, ProviderError, ProviderUnavailableError

logger = logging.getLogger(__name__)


class ProviderRouter:
    MODES = ("manual", "auto", "fallback", "quality")

    AUTO_ORDER = ("openai", "anthropic")
    QUALITY_ORDER = ("anthropic", "openai")

    def __init__(self, session: Session | None = None):
        self.session = session
        self.providers = get_all_providers()

    def get_provider(self, name: str) -> BaseAIProvider | None:
        return self.providers.get(name)

    def is_provider_available(self, name: str) -> bool:
        provider = self.get_provider(name)
        return provider is not None and provider.is_available()

    def has_any_provider(self) -> bool:
        return any(p.is_available() for p in self.providers.values())

    def resolve_provider(
        self,
        mode: str = "auto",
        selected_provider: str | None = None,
    ) -> BaseAIProvider:
        mode = (mode or "auto").lower()
        if mode not in self.MODES:
            mode = "auto"

        if mode == "quality":
            provider = self._first_available(self.QUALITY_ORDER)
            if provider:
                logger.info("Quality mode: resolved to %s", provider.name)
                return provider
            raise ProviderUnavailableError(PROVIDER_UNAVAILABLE_MSG)

        if mode == "auto":
            provider = self._first_available(self.AUTO_ORDER)
            if provider:
                logger.info("Auto mode: resolved to %s", provider.name)
                return provider
            raise ProviderUnavailableError(PROVIDER_UNAVAILABLE_MSG)

        if mode == "manual":
            if selected_provider:
                provider = self.get_provider(selected_provider)
                if provider and provider.is_available():
                    logger.info("Manual mode: using %s", selected_provider)
                    return provider
            raise ProviderUnavailableError(PROVIDER_UNAVAILABLE_MSG)

        if mode == "fallback":
            candidates: list[str] = []
            if selected_provider:
                candidates.append(selected_provider)
            for name in self.AUTO_ORDER:
                if name not in candidates:
                    candidates.append(name)
            provider = self._first_available(candidates)
            if provider:
                logger.info("Fallback mode: resolved to %s", provider.name)
                return provider
            raise ProviderUnavailableError(PROVIDER_UNAVAILABLE_MSG)

        raise ProviderUnavailableError(PROVIDER_UNAVAILABLE_MSG)

    def _first_available(self, names: tuple[str, ...] | list[str]) -> BaseAIProvider | None:
        for name in names:
            provider = self.get_provider(name)
            if provider and provider.is_available():
                return provider
        return None

    def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        mode: str = "auto",
        selected_provider: str | None = None,
        task_type: str = "generate",
        **kwargs,
    ) -> GenerationResult:
        if not self.has_any_provider():
            raise ProviderUnavailableError(PROVIDER_UNAVAILABLE_MSG)

        mode = (mode or "auto").lower()
        chain = self._build_fallback_chain(mode, selected_provider)
        last_error: str | None = None

        request_sanitized = sanitize_payload(
            {"prompt": prompt[:500], "system_prompt": (system_prompt or "")[:500], **kwargs}
        )

        for provider_name in chain:
            provider = self.get_provider(provider_name)
            if not provider or not provider.is_available():
                continue
            try:
                result = provider.generate(prompt, system_prompt, **kwargs)
                if not result.text or not result.text.strip():
                    last_error = "Model returned an empty response."
                    self._log_provider(
                        provider.name,
                        result.model or provider.model_name,
                        task_type,
                        False,
                        result.latency_ms,
                        last_error,
                        request_sanitized,
                        None,
                    )
                    continue
                response_sanitized = sanitize_payload(
                    {"text_preview": result.text[:500], "model": result.model}
                )
                self._log_provider(
                    provider.name,
                    result.model or provider.model_name,
                    task_type,
                    True,
                    result.latency_ms,
                    None,
                    request_sanitized,
                    response_sanitized,
                )
                logger.info("Generation succeeded with provider=%s model=%s", provider.name, result.model)
                return result
            except ProviderError as exc:
                last_error = exc.message
                self._log_provider(
                    provider.name,
                    provider.model_name,
                    task_type,
                    False,
                    None,
                    exc.message,
                    request_sanitized,
                    None,
                )
                logger.warning("Provider %s failed: %s", provider.name, exc.message)
            except Exception as exc:
                last_error = f"Unexpected error: {type(exc).__name__}"
                self._log_provider(
                    provider.name,
                    provider.model_name,
                    task_type,
                    False,
                    None,
                    last_error,
                    request_sanitized,
                    None,
                )
                logger.warning("Provider %s unexpected error: %s", provider.name, type(exc).__name__)

        raise ProviderUnavailableError(last_error or PROVIDER_UNAVAILABLE_MSG)

    def _build_fallback_chain(
        self,
        mode: str,
        selected_provider: str | None,
    ) -> list[str]:
        if mode == "quality":
            return list(self.QUALITY_ORDER)

        if mode == "auto":
            return list(self.AUTO_ORDER)

        if mode == "manual":
            chain: list[str] = []
            if selected_provider:
                chain.append(selected_provider)
            return chain

        if mode == "fallback":
            chain = []
            if selected_provider:
                chain.append(selected_provider)
            for name in self.AUTO_ORDER:
                if name not in chain:
                    chain.append(name)
            return chain

        return list(self.AUTO_ORDER)

    def _log_provider(
        self,
        provider: str,
        model: str | None,
        task_type: str,
        success: bool,
        latency_ms: int | None,
        error_message: str | None = None,
        request_payload_sanitized: str | None = None,
        response_payload_sanitized: str | None = None,
    ) -> None:
        """Stage a provider log without committing or rolling back the caller transaction."""
        if not self.session:
            return
        try:
            with self.session.begin_nested():
                log = ProviderLog(
                    provider=provider,
                    model=model,
                    task_type=task_type,
                    success=success,
                    latency_ms=latency_ms,
                    error_message=error_message,
                    request_payload_sanitized=request_payload_sanitized,
                    response_payload_sanitized=response_payload_sanitized,
                )
                self.session.add(log)
                self.session.flush()
        except Exception as exc:
            logger.warning("Failed to log provider usage: %s", type(exc).__name__)

    def get_availability_status(self) -> dict[str, bool]:
        return {
            name: provider.is_available()
            for name, provider in self.providers.items()
        }
