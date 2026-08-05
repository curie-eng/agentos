"""The thinking-depth vocabulary and its parse (#1182, ADR-0098).

The boot contract carries ``CURIE_THINKING`` as an opaque ``str`` on purpose:
``BootEnv`` must not mirror claude-agent-sdk's option shape, or swapping the
harness becomes a protocol change. So the vocabulary and its rejection live
here, on the consumer side -- exactly where ``ApiBackend`` lives for
``api_backend`` (#514, ``sdk_auth``).

The grammar is three words, chosen to be the operator's vocabulary rather than
the SDK's::

    disabled            no extended thinking at all
    adaptive            the model decides when and how much
    enabled:<tokens>    a fixed thinking budget, e.g. enabled:2000

Unset (or empty) is NOT a fourth value. It means the knob was never turned, and
the runner then sends the SDK no thinking configuration whatsoever, leaving the
model's own default in force. That distinction is the whole reason an
unconfigured install behaves after #1182 exactly as it did before: "no opinion"
and "explicitly adaptive" are different instructions, and only the first one is
the status quo.

A malformed value is rejected loudly at boot rather than dropped. A silently
ignored knob is the worst outcome here: an operator sets ``disable`` (no `d`),
sees no error, and concludes Curie cannot control reasoning -- which is the
report this whole change answers.
"""

from __future__ import annotations

from typing import Any, Final

DISABLED: Final = "disabled"
ADAPTIVE: Final = "adaptive"
_ENABLED_PREFIX: Final = "enabled:"

# Named for the error message, which lists what IS accepted rather than only
# saying what was not: an operator who mistypes needs the vocabulary, not a
# verdict.
_VOCABULARY: Final = f"{DISABLED!r}, {ADAPTIVE!r}, or 'enabled:<budget_tokens>'"


class ThinkingError(ValueError):
    """A `CURIE_THINKING` value outside the declared vocabulary."""


def parse_thinking(raw: str | None) -> dict[str, Any] | None:
    """Turn a raw ``CURIE_THINKING`` value into an SDK thinking config.

    Args:
      raw: the boot value, or None/empty when the knob was never set.

    Returns:
      The mapping to hand ``ClaudeAgentOptions.thinking``, or None when the knob
      is unset -- in which case the caller must omit the option entirely rather
      than pass a default, so the model's own behavior is untouched.

    Raises:
      ThinkingError: the value is outside the vocabulary, or names a budget that
        is not a positive integer.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if value == DISABLED:
        return {"type": "disabled"}
    if value == ADAPTIVE:
        return {"type": "adaptive"}
    if value.startswith(_ENABLED_PREFIX):
        budget_raw = value[len(_ENABLED_PREFIX) :].strip()
        try:
            budget = int(budget_raw)
        except ValueError:
            raise ThinkingError(
                f"thinking budget {budget_raw!r} is not an integer; "
                f"expected 'enabled:<budget_tokens>', e.g. 'enabled:2000'"
            ) from None
        # Zero or negative is not "off" -- `disabled` is off. Accepting it would
        # hand the SDK a budget it cannot honor and make two spellings of the
        # same intent, one of which is a typo of a real budget.
        if budget <= 0:
            raise ThinkingError(
                f"thinking budget must be a positive integer, got {budget}; "
                f"use {DISABLED!r} to turn thinking off"
            )
        return {"type": "enabled", "budget_tokens": budget}
    raise ThinkingError(f"unknown thinking value {value!r}; expected {_VOCABULARY}")
