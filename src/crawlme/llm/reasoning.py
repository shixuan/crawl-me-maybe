"""How hard to think, said once and translated per model.

Configuration names an effort in this project's own words. What a
provider accepts differs: DeepSeek turns thinking off with "none",
GPT-5 rejects that value and calls its floor "minimal", and a model
that does not think at all rejects the parameter itself. Naming the
provider's word in configuration made one setting right for one model
and an error for the rest.

The vocabulary is off / low / medium / high, and empty leaves the
provider's own default alone.
"""

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
    # It thinks and will not be talked out of it. Sending a floor the
    # model rejects would fail the call, and failing is worse than
    # thinking.
    logger.info("%s has no way to turn thinking off, leaving it on", model)
    return ""


def step_down(wanted: str) -> str:
    """The level below *wanted*, or empty when there is nowhere lower.

    A model that thinks away the whole output allowance answers nothing.
    Asking again at the same level buys the same silence and a bigger
    ceiling buys a longer one, so the only move left is to think less.
    A value this project has not heard of drops straight to the floor,
    since there is no ladder to walk down.
    """
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
    """Whether this model is known to reject *value*.

    Unknown counts as accepted. litellm records the answer for a handful
    of models and nothing for the rest, so treating silence as refusal
    would turn thinking on everywhere it has not been catalogued.
    """
    try:
        import litellm

        info = dict(litellm.get_model_info(model) or {})
    except Exception:
        return True
    return info.get(f"supports_{value}_reasoning_effort") is not False
