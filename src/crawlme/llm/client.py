"""Lazy LiteLLM access with concurrency limits, retries and token accounting."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from crawlme.config import Settings
from crawlme.llm.budget import TokenBudget
from crawlme.llm.errors import LLMError
from crawlme.llm.reasoning import effort_for, step_down

logger = logging.getLogger(__name__)

# Total deadline for an LLM request, including generation.
_LLM_TIMEOUT = 90.0
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
    # Expose likely truncation so callers can distinguish it from malformed JSON.
    truncated: bool = False


def _cached_input(usage: Any) -> int:
    """Read cached-input usage across provider response shapes; return 0 if unreported."""
    direct = getattr(usage, "prompt_cache_hit_tokens", None)
    if direct is not None:
        return int(direct)
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        nested = getattr(details, "cached_tokens", None)
        if nested is None and isinstance(details, dict):
            nested = details.get("cached_tokens")
        if nested is not None:
            return int(nested)
    if isinstance(usage, dict):
        return int(usage.get("prompt_cache_hit_tokens") or 0)
    return 0


def _reasoning_output(resp: Any, usage: Any) -> int:
    """Read reasoning-token usage, which is part of output usage."""
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        n = getattr(details, "reasoning_tokens", None)
        if n is None and isinstance(details, dict):
            n = details.get("reasoning_tokens")
        if n is not None:
            return int(n)
    # No usage field: fall back to what the model sent us and we drop.
    try:
        text = resp.choices[0].message.reasoning_content or ""
    except (AttributeError, IndexError):
        return 0
    return len(text) // 4


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def litellm_loaded() -> bool:
    """True once the litellm package has been imported (an LLM call
    happened).  Used by the CLI to decide whether to give litellm's
    background logging worker time to drain before loop teardown."""
    return _litellm is not None


async def close_litellm_clients() -> None:
    """Best-effort cleanup of LiteLLM cached clients before the event loop closes."""
    if not litellm_loaded():
        return
    try:
        from litellm.llms.custom_httpx.async_client_cleanup import close_litellm_async_clients

        await close_litellm_async_clients()  # type: ignore[no-untyped-call]
    except Exception as e:
        logger.debug("llm.shutdown cleanup best-effort failed: %s", e)
    await asyncio.sleep(0.2)


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
    # Treat authentication failures as permanent even when mapped to a server error.
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
        max_output_tokens: int = 8192,
        reasoning_effort: str = "",
        stage: str = "",
    ) -> None:
        # Attribute all calls from this client to its LLM stage.
        self._stage = stage
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._sem = asyncio.Semaphore(concurrency)
        self._budget = budget
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        budget: TokenBudget | None = None,
        reasoning_effort: str = "",
        stage: str = "",
    ) -> LLMClient:
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
            max_output_tokens=settings.llm_max_output_tokens,
            reasoning_effort=reasoning_effort,
            stage=stage,
        )

    @classmethod
    def from_settings_if_configured(
        cls,
        settings: Settings,
        *,
        budget: TokenBudget | None = None,
        reasoning_effort: str = "",
        stage: str = "",
    ) -> LLMClient | None:
        """Default-on with graceful auto-off, mirroring the analysis
        provider.  Without a key and without a custom endpoint there is
        no way to authenticate, so return None and let the caller skip
        the LLM stages instead of failing at runtime."""
        if not settings.llm_api_key and not settings.llm_base_url:
            logger.info("no LLM key configured, running without the LLM stages")
            return None
        return cls.from_settings(settings, budget=budget, reasoning_effort=reasoning_effort, stage=stage)

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
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """Request a completion with optional JSON mode and a per-call output ceiling.

        max_tokens defaults to the client setting. Usage is recorded for each response,
        including a lower-effort retry after a reasoning-only reply."""
        ceiling = max_tokens if max_tokens is not None else self._max_output_tokens
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
                    started = time.monotonic()
                    resp = await self._complete(messages, ceiling, json_mode)
                    elapsed = time.monotonic() - started
                    content = (resp.choices[0].message.content or "").strip()
                    usage = resp.usage
                    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
                    output_tokens = getattr(usage, "completion_tokens", 0) or 0
                    cached_tokens = _cached_input(usage)
                    thinking_tokens = _reasoning_output(resp, usage)
                    if self._budget is not None:
                        self._budget.record(
                            input_tokens, output_tokens, cached_tokens, thinking_tokens, stage=self._stage
                        )
                    # Retry an empty reasoning-only response once at lower effort.
                    lower = self._quieter()
                    if not content and thinking_tokens > 0 and lower:
                        logger.warning(
                            "llm.chat.thinking_only out=%d ceiling=%d; asking again at reasoning=%s",
                            output_tokens,
                            ceiling,
                            lower,
                        )
                        if self._budget is not None:
                            self._budget.check()
                        resp = await self._complete(messages, ceiling, json_mode, effort=lower)
                        content = (resp.choices[0].message.content or "").strip()
                        usage = resp.usage
                        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
                        output_tokens = getattr(usage, "completion_tokens", 0) or 0
                        cached_tokens = _cached_input(usage)
                        thinking_tokens = _reasoning_output(resp, usage)
                        if self._budget is not None:
                            self._budget.record(
                                input_tokens, output_tokens, cached_tokens, thinking_tokens, stage=self._stage
                            )
                    # An empty response with output usage is treated as truncated.
                    truncated = output_tokens >= ceiling or (not content and output_tokens > 0)
                    if truncated:
                        # Thinking down is no longer advice to give: the
                        # step-down above has already tried it, or the
                        # model has nowhere lower to go.
                        logger.warning(
                            "llm.chat.output_ceiling out=%d (thinking %d) ceiling=%d; %s (raise LLM_MAX_OUTPUT_TOKENS)",
                            output_tokens,
                            thinking_tokens,
                            ceiling,
                            "nothing left for the answer" if not content else "the reply is cut short",
                        )
                    # Elapsed wall time also includes event-loop scheduling delays.
                    logger.debug(
                        "llm.chat.wall %.1fs out=%d thinking=%d of %d",
                        elapsed,
                        output_tokens,
                        thinking_tokens,
                        ceiling,
                    )
                    return LLMResponse(
                        content=content,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        # Use the configured model for replay identity; provider aliases may differ.
                        model=str(self._model or getattr(resp, "model", "")),
                        truncated=truncated,
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

    def _quieter(self) -> str:
        """The effort to retry at, or empty when retrying cannot help.

        Empty covers both ends: a model that does not take the parameter
        at all, and one already at the floor.
        """
        lower = step_down(self._reasoning_effort)
        if not lower:
            return ""
        quieter = effort_for(self._model, lower)
        return quieter if quieter != effort_for(self._model, self._reasoning_effort) else ""

    async def _complete(
        self, messages: list[dict[str, str]], max_tokens: int, json_mode: bool, effort: str = ""
    ) -> Any:
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
        sending = effort or effort_for(self._model, self._reasoning_effort)
        if sending:
            kwargs["reasoning_effort"] = sending
        return await litellm.acompletion(**kwargs)
