"""The thinking vocabulary and its parse (#1182, ADR-0098).

The load-bearing case is the FIRST one: unset must produce None, so the caller
omits the SDK option entirely. "No opinion" and "explicitly adaptive" are
different instructions to a model, and only the first is the pre-#1182 status
quo -- an install that configures nothing must behave exactly as it always has.
"""

from __future__ import annotations

import pytest
from curie_runner.thinking import ThinkingError, parse_thinking


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_unset_parses_to_none_so_the_option_is_omitted(raw: str | None) -> None:
    # None here is not a default; it is the signal to leave `thinking` out of
    # ClaudeAgentOptions altogether. Returning {"type": "adaptive"} instead would
    # silently start configuring every model on every install.
    assert parse_thinking(raw) is None


def test_disabled_and_adaptive_map_to_their_sdk_shapes() -> None:
    assert parse_thinking("disabled") == {"type": "disabled"}
    assert parse_thinking("adaptive") == {"type": "adaptive"}
    # Surrounding whitespace is an operator typo in a values file, not a value.
    assert parse_thinking("  disabled  ") == {"type": "disabled"}


def test_enabled_carries_the_budget_through() -> None:
    assert parse_thinking("enabled:2000") == {"type": "enabled", "budget_tokens": 2000}
    assert parse_thinking("enabled: 512 ") == {"type": "enabled", "budget_tokens": 512}


@pytest.mark.parametrize(
    "raw",
    [
        "disable",  # the plausible typo: no trailing 'd'
        "off",  # the plausible synonym
        "none",
        "true",
        "ENABLED:2000",  # the vocabulary is lowercase; near-miss must not pass
        "enabled",  # the prefix without a budget
        "enabled:",
        "enabled:lots",
        "2000",  # a bare number is not the grammar
    ],
)
def test_a_value_outside_the_vocabulary_is_rejected_loudly(raw: str) -> None:
    # Rejected at boot, never dropped. A silently ignored knob is the failure
    # this whole change exists to answer: the operator sets it, sees no error,
    # and concludes Curie cannot control reasoning.
    with pytest.raises(ThinkingError):
        parse_thinking(raw)


@pytest.mark.parametrize("budget", [0, -1, -2000])
def test_a_nonpositive_budget_is_rejected_rather_than_treated_as_off(budget: int) -> None:
    # `enabled:0` is not a second spelling of `disabled`. Accepting it would hand
    # the SDK a budget it cannot honor, and it is a plausible typo of a real
    # budget (a dropped digit), so it must fail rather than quietly mean "off".
    with pytest.raises(ThinkingError) as excinfo:
        parse_thinking(f"enabled:{budget}")
    assert "disabled" in str(excinfo.value), "the error must name the way to turn it off"


def test_the_error_names_the_vocabulary_not_just_the_verdict() -> None:
    # An operator who mistypes needs to be told what IS accepted; a bare
    # rejection sends them to the source.
    with pytest.raises(ThinkingError) as excinfo:
        parse_thinking("disable")
    message = str(excinfo.value)
    assert "disable" in message, "the rejected value must be quoted back"
    for word in ("disabled", "adaptive", "enabled:<budget_tokens>"):
        assert word in message, f"the error must offer {word!r}"
