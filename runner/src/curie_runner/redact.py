"""Secret redaction at the runner's output boundaries.

Defense in depth. Curie forwards real credentials into the sandbox (the ACI
``CURIE_CREDENTIALS`` reference resolves to a live provider key) and the runner
self-emits OTel spans to a trace backend, so a secret that reaches a log line or a
span attribute has already left the trust boundary by the time anything downstream
sees it. This module scrubs the value on the way out, at the last point the runner
still owns it.

Scope is the two boundaries issue #518 names: the runner's stdout logs and the
gen_ai span attributes. That is not the whole of the runner's egress, and this
module does not claim otherwise. ``server.py`` streams verbatim model output as
NDJSON ACI frames, which is a larger surface left deliberately untouched here: the
frames are the product contract, and scrubbing them is a separate decision.
``check.py`` writes its own stdout without this pass, which is scoped out because
it has no credential path and never issues a model query, and because its JSON
output is a frozen contract that a scrub would mangle.

This complements, and does not replace, the export-time validator in issue #512,
which drops records that still carry an unscrubbed secret. That validator is the
alarm; this module is the scrub. Both exist because either one alone fails open:
the scrub can miss a class the regexes do not know, and the validator can only
discard a record after the fact.

Tripwire contract: ``runner/tests/test_redact.py`` binds ``REDACTION_RULES`` to a
frozen vector per rule and ``REDACTION_BOUNDARIES`` to a boundary per parametrized
case. Adding a regex requires adding its frozen vector; adding an output boundary
requires driving every vector through it. A rule applied at one boundary and absent
at the other is a leak that no other test would catch.

Rules are applied in registry order and first match wins, so they are written to be
disjoint on the frozen vectors. Every pattern is precompiled at import and the pass
is pure string work: no I/O and no environment reads, which keeps the runner's
``CURIE_FAKE_MODEL`` path a true offline no-op.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RedactionRule:
    """One secret class: the pattern that finds it and the text that replaces it."""

    name: str
    pattern: re.Pattern[str]
    placeholder: str


def _placeholder(name: str) -> str:
    return f"[REDACTED:{name}]"


# Order is load bearing. ``url_secret_param`` is anchored on a query-param prefix
# and runs before ``secret_assignment`` so a token carried in a URL is attributed
# to the URL rule rather than swallowed by the generic assignment rule; the
# assignment rule cannot reach the URL case because it does not match after a
# ``?`` or ``&``. ``jwt`` runs before ``bearer_token`` so a bare ``eyJ`` token is
# attributed to the JWT rule. Every remaining rule is keyed on a vendor-specific
# prefix, so they cannot overlap each other.
REDACTION_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        name="pem_private_key",
        pattern=re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        placeholder=_placeholder("pem_private_key"),
    ),
    RedactionRule(
        name="url_secret_param",
        pattern=re.compile(
            r"[?&](?:token|secret|password|passwd|pwd|api_key|apikey|access_token|key|sig"
            r"|signature)=[^&\s]+",
            re.IGNORECASE,
        ),
        placeholder=_placeholder("url_secret_param"),
    ),
    RedactionRule(
        name="jwt",
        pattern=re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
        placeholder=_placeholder("jwt"),
    ),
    RedactionRule(
        name="bearer_token",
        pattern=re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+"),
        placeholder=_placeholder("bearer_token"),
    ),
    RedactionRule(
        name="api_key",
        pattern=re.compile(r"\b(?:sk|xai)[-_][A-Za-z0-9_-]{16,}"),
        placeholder=_placeholder("api_key"),
    ),
    RedactionRule(
        name="aws_access_key_id",
        pattern=re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16,}"),
        placeholder=_placeholder("aws_access_key_id"),
    ),
    RedactionRule(
        name="github_pat",
        pattern=re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"),
        placeholder=_placeholder("github_pat"),
    ),
    RedactionRule(
        name="gitlab_token",
        pattern=re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),
        placeholder=_placeholder("gitlab_token"),
    ),
    RedactionRule(
        name="slack_token",
        pattern=re.compile(r"\bxox[baps]-[A-Za-z0-9-]{10,}"),
        placeholder=_placeholder("slack_token"),
    ),
    RedactionRule(
        name="google_api_key",
        pattern=re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),
        placeholder=_placeholder("google_api_key"),
    ),
    # Deliberately narrow: only secret-bearing key names, and only ``=``. A wider
    # key set or a ``:`` separator would eat ordinary runner log lines such as
    # "runner configured session=s-1 model=claude-opus-4-8 port=8080".
    RedactionRule(
        name="secret_assignment",
        pattern=re.compile(
            r"\b(?:secret|password|passwd|pwd|api_key|apikey|access_token|token)=\S+",
            re.IGNORECASE,
        ),
        placeholder=_placeholder("secret_assignment"),
    ),
    # Collapses the account portion only, so the remainder of the path stays
    # readable. Generic by construction: never read $HOME, never bake in a name.
    RedactionRule(
        name="home_path",
        pattern=re.compile(r"/(?:home|Users)/[^/\s]+"),
        placeholder=_placeholder("home_path"),
    ),
)

REDACTION_BOUNDARIES: tuple[str, ...] = ("stdout", "gen_ai_span")


def redact_text(text: str) -> str:
    """Apply every redaction rule, in registry order, to one string."""

    for rule in REDACTION_RULES:
        text = rule.pattern.sub(rule.placeholder, text)
    return text


def redact_span_attribute(value: object) -> object:
    """Redact a span attribute value, preserving its type.

    Strings are scrubbed and sequences are scrubbed ELEMENTWISE, preserving the
    container type (OTel requires a homogeneous sequence, so scrubbed str elements
    stay str). Every other value (int, float, bool) passes through untouched, so
    the ``gen_ai.usage.*`` token counts stay ints.

    Sequences are handled here rather than left to a future caller (#935): only str
    and int attributes are set today, but OTel permits sequence values, and relying
    on whoever adds the first one to remember to extend this function is exactly
    the defense-in-depth gap that issue closed. ``otel.py``'s export validator
    recurses the same way as the backstop.
    """

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (list, tuple)):
        scrubbed = [redact_span_attribute(item) for item in value]
        return tuple(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


class RedactingLogFilter(logging.Filter):
    """Scrubs secrets from a log record's fully formatted message.

    The runner logs args-style throughout (``logger.info("tool=%s", name)``), so a
    secret usually arrives in ``record.args`` and never appears in ``record.msg``.
    Filtering ``msg`` alone would leak it, so this formats the record first and
    replaces the pair with the scrubbed result.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_stdout_redaction() -> None:
    """Attach the redaction filter to every root logger handler, idempotently.

    Repeated calls are a no-op on handlers that already carry the filter, so the
    pass never stacks or double-redacts.
    """

    for handler in logging.getLogger().handlers:
        if not any(isinstance(existing, RedactingLogFilter) for existing in handler.filters):
            handler.addFilter(RedactingLogFilter())
