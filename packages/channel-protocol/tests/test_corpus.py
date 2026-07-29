"""Cross-language field gate (#490, #1079): the Python OutboundMessage model asserts
against the SAME committed corpus the Rust adapter (cli/src/channel.rs) checks, so
a field-bound change on one side without the other fails a corpus test. The corpus
pins field bounds (#490) AND field semantics (#1079): every valid case carries an
explicit ``allow_free_text`` that both sides compare against, so a side that
hardcodes the flag instead of carrying the value through fails here.
"""

import json
from pathlib import Path

import pytest
from channel_protocol.models import OutboundMessage
from pydantic import ValidationError

_CORPUS = json.loads(
    (Path(__file__).parents[1] / "schema" / "channel-protocol.corpus.json").read_text(
        encoding="utf-8"
    )
)


def test_valid_messages_pass_the_model() -> None:
    for message in _CORPUS["valid"]:
        parsed = OutboundMessage.model_validate(message)
        interaction = message.get("interaction")
        if interaction is not None:
            # Compared, not just validated: a side that hardcoded the flag would
            # otherwise pass every corpus case regardless of what it carries. The
            # key is REQUIRED, not presence-guarded, so deleting it from a corpus
            # case reds this test instead of silently disarming the comparison.
            # Omitted-key defaults (choice True, confirm False, per ChoiceIntent/
            # ConfirmIntent in channel_protocol.models) are deliberately out of
            # scope for this corpus.
            assert "allow_free_text" in interaction, (
                f"corpus valid message carries no allow_free_text: {message}"
            )
            assert parsed.interaction is not None
            assert parsed.interaction.allow_free_text == interaction["allow_free_text"], (
                f"allow_free_text did not survive validation: {message}"
            )


def test_out_of_bounds_messages_are_rejected() -> None:
    for entry in _CORPUS["invalid"]:
        with pytest.raises(ValidationError):
            OutboundMessage.model_validate(entry["message"])
