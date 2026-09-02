"""Publication must be preceded by a reported verification of the changes.

The stable train shipped this contract against the coder example's
``SKILL.md``. The feature train made coding tools a built-in session
capability and deleted that file, so the same contract is asserted here
against the built-in ``publish_changes`` description, which is now the only
place the coder reads the publication protocol from.

This is a description-contract test rather than an implementation test: the
coder operates in an arbitrary target repository, so the contract must make it
run that repository's documented check and report the observable result
in-thread. The publication instruction is the final boundary; verification
language after it would be too late to guide the coder.
"""

import re

from curie_runner.approval import _PUBLISH_DESCRIPTION


def test_publication_description_requires_a_reported_verification_first() -> None:
    description = _PUBLISH_DESCRIPTION

    publication = re.search(
        r"when the changes are ready, use\s+this tool to request human approval",
        description,
        flags=re.IGNORECASE,
    )
    assert publication, "the publication instruction must remain explicit"

    verification = re.search(
        r"before requesting publication.*?(?=When the changes are ready)",
        description,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert verification, "there must be a distinct pre-publication verification instruction"
    verification_text = verification.group(0)
    assert verification.end() <= publication.start(), (
        "verification instructions must appear before publication"
    )

    assert re.search(
        r"repository(?:'s)?\s+own\s+documented\s+test\s+or\s+check\s+command",
        verification_text,
        flags=re.IGNORECASE,
    ), "the coder must run the target repository's own documented test or check command"
    assert re.search(
        r"run\s+it\s+from\s+/workspace",
        verification_text,
        flags=re.IGNORECASE,
    ), "the repository command must explicitly run from /workspace"
    assert re.search(
        r"exact command.{0,60}exit status.{0,60}result",
        verification_text,
        flags=re.IGNORECASE | re.DOTALL,
    ), "the command, its exit status, and its result must all be reported"
    assert "thread" in verification_text.casefold()

    assert re.search(
        r"cannot\s+identify\s+or\s+run\s+an?\s+appropriate\s+command"
        r".{0,120}(?:do\s+not|must\s+not|never)\s+publish",
        verification_text,
        flags=re.IGNORECASE | re.DOTALL,
    ), "inability to identify or run a command must prevent publication"
    assert re.search(
        r"(?:fail(?:s|ed|ure|ing)?|non[- ]zero).{0,140}"
        r"(?:do not|must not|never)\s+publish",
        verification_text,
        flags=re.IGNORECASE | re.DOTALL,
    ), "a failed verification must prevent publication"
    assert re.search(
        r"verification\s+generates\s+artifacts?"
        r".{0,140}(?:do\s+not|must\s+not|never)\s+publish\s+unrequested\s+artifacts?",
        verification_text,
        flags=re.IGNORECASE | re.DOTALL,
    ), "verification artifacts must not publish unrequested artifacts"
    assert re.search(
        r"only\s+artifacts?\s+this\s+verification\s+created",
        verification_text,
        flags=re.IGNORECASE,
    ), "cleanup must not remove requested or unrelated work"
