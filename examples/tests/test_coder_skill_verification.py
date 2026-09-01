"""The coder skill must verify changes before offering platform publication.

This is a skill-contract test rather than an implementation test: the coder
operates in an arbitrary target repository, so the contract must make it run
that repository's documented check and report the observable result in-thread.
The publication instruction is deliberately treated as the final boundary;
verification language after it would be too late to guide the coder.
"""

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CODER_SKILL = REPO / "examples" / "coder" / "skills" / "coder" / "SKILL.md"
PUBLISH_TOOL = "mcp__curie__publish_changes"


def _skill_body() -> str:
    """Read the instructions, excluding the YAML tool allow-list."""
    text = CODER_SKILL.read_text(encoding="utf-8")
    _front_matter, separator, body = text.partition("\n---\n")
    assert separator, "coder skill has no front-matter separator"
    return body


def test_coder_verifies_and_reports_changes_before_publication() -> None:
    body = _skill_body()
    lowered = body.casefold()

    # Keep the assertion tied to the actual publication paragraph, rather than
    # the same tool name in front matter or an incidental example.
    publication = re.search(
        r"when the user asks[^.\n]*publish.*?" + re.escape(PUBLISH_TOOL),
        body,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert publication, "coder skill must retain an explicit publication instruction"
    before_publish = body[: publication.start()]

    assert "/workspace" in before_publish, "verification must run inside the sandbox"
    assert re.search(
        r"(?:repository(?:'s)?\s+own|repo[- ]owned|own\s+repository)"
        r".{0,140}(?:document(?:ed|ation)?).{0,140}"
        r"(?:test\s+or\s+check|test/check|check\s+or\s+test).{0,80}command",
        before_publish,
        flags=re.IGNORECASE | re.DOTALL,
    ), "run the target repository's documented test/check command"
    assert re.search(
        r"(?:after|once|following).{0,100}(?:change|edit|modif)",
        before_publish,
        flags=re.IGNORECASE | re.DOTALL,
    ), "verification must happen after the requested changes"

    assert "thread" in before_publish.casefold()
    assert "exit status" in before_publish.casefold()
    assert re.search(r"\bcommand\b", before_publish, flags=re.IGNORECASE)
    assert re.search(r"\bresult\b", before_publish, flags=re.IGNORECASE)

    # A failed check must be an observable stop condition, not merely a report.
    failure_blocks_publish = re.search(
        r"(?:fail(?:s|ed|ure|ing)?|non[- ]zero).{0,140}"
        r"(?:block|prevent|do not|must not|never).{0,100}publish"
        r"|(?:do not|must not|never|block|prevent).{0,100}publish"
        r".{0,140}(?:fail(?:s|ed|ure|ing)?|non[- ]zero)",
        before_publish,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert failure_blocks_publish, "a failed verification must prevent publication"
