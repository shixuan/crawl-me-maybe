"""Translate off/low/medium/high effort settings using provider capabilities.

An empty setting leaves the provider default unchanged."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# What configuration may say. Anything else is passed through, so a
# value this project has not heard of still reaches the provider.
OFF = "off"
LEVELS = (OFF, "low", "medium", "high")

# The provider words for "as little as possible". The first one a model
# accepts wins; a model that takes neither cannot be turned off.
_FLOORS = ("none", "minimal")

_PARAM = "reasoning_effort"


def effort_for(model: str, wanted: str) -> str:
    """The value to send for *model*, or empty to send nothing."""
    if not wanted:
        return ""
    if not _takes_effort(model):
        # Said out loud: the setting had no effect, and a model whose
        # catalogue entry is missing looks exactly like one that cannot
        # think, so a new model can silently keep thinking and billing.
        logger.info(
            "%s is not known to take a thinking level, so %r had no effect",
            model,
            wanted,
        )
        return ""
    if wanted != OFF:
        return wanted
    for floor in _FLOORS:
        if _accepts(model, floor):
            return floor
    # Omit an unsupported off value instead of sending an invalid parameter.
    logger.info("%s has no way to turn thinking off, leaving it on", model)
    return ""


def step_down(wanted: str) -> str:
    """Return the next lower effort. Unknown values fall to off; off has no lower level."""
    if wanted == OFF:
        return ""
    if wanted in LEVELS:
        return LEVELS[LEVELS.index(wanted) - 1]
    return OFF


def _takes_effort(model: str) -> bool:
    """Whether this model takes the parameter at all."""
    try:
        import litellm

        supported = litellm.get_supported_openai_params(model=model) or []
    except Exception:
        # Unknown to litellm. Sending it is the guess that fails loudly
        # rather than the one that silently thinks.
        return True
    return _PARAM in supported


def _accepts(model: str, value: str) -> bool:
    """Return whether a value is accepted; assume acceptance when capability data is absent."""
    try:
        import litellm

        info = dict(litellm.get_model_info(model) or {})
    except Exception:
        return True
    return info.get(f"supports_{value}_reasoning_effort") is not False
