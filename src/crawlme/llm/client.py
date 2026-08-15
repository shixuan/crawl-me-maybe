"""LLM client wrapper.

The single entry point for every LLM call in the system.  Wraps
litellm so providers are interchangeable (OpenAI, Anthropic, anything
OpenAI-compatible) and layers the house rules on top: a concurrency
cap, retries with backoff on transient failures, and token accounting
on every response so the token budget can be tracked.

litellm ships as a core dependency but is imported lazily on the first
LLM call, so runs that never touch the LLM never pay the import cost.

Retry policy: rate limits, timeouts, connection errors, and 5xx
responses are transient and get 2 retries with exponential backoff.
Everything else (auth errors, bad requests, content policy) raises
immediately.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from crawlme.config import Settings
from crawlme.llm.budget import TokenBudget
from crawlme.llm.errors import LLMError

logger = logging.getLogger(__name__)

_LLM_TIMEOUT = 60.0
_LLM_MAX_RETRIES = 2
_LLM_RETRY_BASE = 1.0
_DEFAULT_MODEL = "openai/gpt-4o-mini"

_litellm: Any | None = None


@dataclass(frozen=True)
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    model: str


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def litellm_loaded() -> bool:
    """True once the litellm package has been imported (an LLM call
    happened).  Used by the CLI to decide whether to give litellm's
    background logging worker time to drain before loop teardown."""
    return _litellm is not None


def _litellm_module() -> Any:
    """Import litellm once, failing fast with install instructions."""
    global _litellm
    if _litellm is None:
        try:
            import litellm
        except ImportError as e:
            raise LLMError(
                "LLM features require the 'litellm' package, which ships as a core "
                "dependency: reinstall with `pip install -e .`"
            ) from e
        _litellm = litellm
    return _litellm


def _is_transient(exc: BaseException) -> bool:
    # Auth problems are permanent no matter how litellm maps them.
    # Missing credentials surfaces as InternalServerError, so classify
    # by message as well as by type.
    if "credential" in str(exc).lower():
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    litellm = _litellm
    if litellm is None:
        return False
    return isinstance(
        exc,
        (
            litellm.RateLimitError,
            litellm.Timeout,
            litellm.APIConnectionError,
            litellm.ServiceUnavailableError,
            litellm.InternalServerError,
        ),
    )


class LLMClient:
    """Async chat client: concurrency cap, retries, token accounting.

    *model* is a litellm model id ("openai/gpt-4o-mini", ...).
    *base_url* points at another OpenAI-compatible endpoint when set;
    *api_key* is omitted from the request when empty (local endpoints).
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str = "",
        base_url: str = "",
        concurrency: int = 2,
        budget: TokenBudget | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._sem = asyncio.Semaphore(concurrency)
        self._budget = budget

    @classmethod
    def from_settings(cls, settings: Settings, *, budget: TokenBudget | None = None) -> LLMClient:
        """Build from Settings: llm_model, llm_api_key, llm_base_url,
        llm_concurrency.  An empty llm_model resolves to the provider
        default, so a key alone is enough to get a working client."""
        if not settings.llm_api_key and not settings.llm_base_url:
            logger.warning("llm.unconfigured no api key or base url set, calls will fail auth")
        return cls(
            settings.llm_model or _DEFAULT_MODEL,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            concurrency=settings.llm_concurrency,
            budget=budget,
        )

    @classmethod
    def from_settings_if_configured(cls, settings: Settings, *, budget: TokenBudget | None = None) -> LLMClient | None:
        """Default-on with graceful auto-off, mirroring the embedding
        provider.  Without a key and without a custom endpoint there is
        no way to authenticate, so return None and let the caller skip
        the LLM stages instead of failing at runtime."""
        if not settings.llm_api_key and not settings.llm_base_url:
            logger.info("llm.auto_off no api key or base url configured")
            return None
        return cls.from_settings(settings, budget=budget)

    @property
    def configured(self) -> bool:
        """True when credentials exist, either an API key or a custom
        endpoint that may not need one."""
        return bool(self._api_key or self._base_url)

    async def chat(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 512,
        json_mode: bool = False,
    ) -> LLMResponse:
        """One chat completion.

        *json_mode* requests structured JSON output where the provider
        supports it (OpenAI and compatible endpoints).
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        if self._budget is not None:
            self._budget.check()

        last_err: BaseException | None = None
        async with self._sem:
            for attempt in range(_LLM_MAX_RETRIES + 1):
                try:
                    resp = await self._complete(messages, max_tokens, json_mode)
                    content = (resp.choices[0].message.content or "").strip()
                    usage = resp.usage
                    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
                    output_tokens = getattr(usage, "completion_tokens", 0) or 0
                    if self._budget is not None:
                        self._budget.record(input_tokens, output_tokens)
                    return LLMResponse(
                        content=content,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        model=str(getattr(resp, "model", "") or self._model),
                    )
                except LLMError:
                    raise
                except Exception as exc:
                    if not _is_transient(exc):
                        raise LLMError(f"LLM call rejected by provider: {exc}") from exc
                    last_err = exc
                    if attempt < _LLM_MAX_RETRIES:
                        delay = _LLM_RETRY_BASE * (2**attempt)
                        logger.warning(
                            "llm.chat.retry attempt=%d delay=%.0fs error=%s",
                            attempt + 1,
                            delay,
                            exc,
                        )
                        await _sleep(delay)
        raise LLMError(f"LLM call failed after {_LLM_MAX_RETRIES + 1} attempts: {last_err}") from last_err

    async def _complete(self, messages: list[dict[str, str]], max_tokens: int, json_mode: bool) -> Any:
        litellm = _litellm_module()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "timeout": _LLM_TIMEOUT,
        }
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._base_url:
            kwargs["api_base"] = self._base_url
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return await litellm.acompletion(**kwargs)
