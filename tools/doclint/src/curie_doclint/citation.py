"""Citation discovery, the three-bucket classifier, and the raw line-ban rule.

Discovery is by markdown parse, not raw regex, so fenced and indented code
blocks are structurally excluded from path/symbol checking rather than
heuristically skipped. Only inline-code spans are candidate citations.

The raw line-ban rule is the one deliberate exception: it scans the raw file
text (including fenced blocks and prose) because the goal is that the rotten
coordinate form does not appear at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

# The single recognized extension list, shared by the path rule and the raw
# line-ban rule. Two lists would drift, which is the whole subject of #452.
SOURCE_EXTENSIONS: tuple[str, ...] = (
    "py",
    "rs",
    "ts",
    "tsx",
    "yaml",
    "yml",
    "json",
    "sh",
    "toml",
    "md",
)

# The unified raw line-citation ban: every recognized extension followed by
# either the ``:NN`` or the GitHub ``#LNN`` spelling of a line coordinate. The
# leading character class captures the path prefix so the reported match names
# the full coordinate (e.g. ``pkg/x.rs:12``).
_LINE_BAN = re.compile(
    r"[A-Za-z0-9_./#-]*\.(?:" + "|".join(SOURCE_EXTENSIONS) + r")(?::\d+|#L\d+)"
)

_IGNORE_LINE = "<!-- doclint:ignore-line -->"

# Reused across calls: MarkdownIt is safe to construct once and parse many
# times, and construction is not free enough to redo on every doc.
_MD = MarkdownIt("commonmark")


def parse_markdown(text: str) -> list[Token]:
    """Parse ``text`` with the shared parser. The one construction site for the
    whole package, so a second ``MarkdownIt("commonmark")`` never drifts from
    this one's config."""
    return _MD.parse(text)


@dataclass(frozen=True)
class CodeSpan:
    content: str
    line: int | None
    # The href of the enclosing markdown link, when the span is the visible text
    # of a ``[`text`](target)`` link; ``None`` for a bare inline-code span. Used
    # to gate that a decorated citation's target agrees with its cited path.
    href: str | None = None


@dataclass(frozen=True)
class LineBanHit:
    coordinate: str
    line: int


def _has_source_extension(text: str) -> bool:
    return any(text.endswith("." + ext) for ext in SOURCE_EXTENSIONS)


def _locate_span(block_text: str, content: str, cursor: int) -> tuple[int, int]:
    """Return ``(line_offset, next_cursor)`` for a span's content in a block.

    ``code_inline`` tokens carry no reliable own map, so the span's true source
    line is recovered by finding its backtick content within the inline block's
    raw source (the lines from ``token.map[0]`` to ``token.map[1]``). ``cursor``
    is the char offset to search from, advanced past each match so repeated
    identical spans map to successive physical lines instead of all collapsing
    onto the block's first line. ``line_offset`` is the count of newlines before
    the match, i.e. how many lines into the block the span sits.

    Falls back to the cursor position when the content is not found verbatim
    (e.g. a span whose source wraps a line break, which markdown collapses to a
    space); the reported line is then the block-relative cursor line.
    """
    index = block_text.find(content, cursor)
    if index < 0:
        index = cursor
    line_offset = block_text.count("\n", 0, index)
    return line_offset, index + max(len(content), 1)


def find_code_spans(text: str) -> list[CodeSpan]:
    """Return every inline-code span, excluding fenced/indented code blocks.

    ``code_inline`` tokens appear only as children of ``inline`` tokens, never
    inside a ``fence`` or ``code_block`` leaf, so collecting them naturally
    excludes fenced and indented content.

    Each span's ``line`` is its true physical source line, not the inline
    container's start line. A multi-line paragraph holds one ``inline`` token
    whose ``map`` spans the whole paragraph, so stamping every span with
    ``map[0]`` would report each on the paragraph's first line; that is what let
    a single escape-hatch comment silence findings paragraph-wide. Instead each
    span is located within the block's source text (see ``_locate_span``) so the
    reported line is where the backticks actually are.

    A ``code_inline`` inside a markdown link is validated like any other span,
    with one exception: a self-referential relative link whose visible text is
    identical to its own destination (e.g. `` [`diagrams/x.md`](diagrams/x.md) ``)
    is navigation, not a citation. The label and the target are the same string,
    so nothing is being cited, only linked; such spans are skipped. A decorated
    citation whose destination differs from its text (e.g.
    `` [`apps/api/x.py`](../../apps/api/x.py) ``) is still classified and checked,
    which closes the blind spot where a real repo-root citation escaped the gate
    merely by being written as a link. CommonMark disallows nested links, so at
    most one link is ever open at a time; that is tracked as a single optional
    href rather than a stack.
    """
    source_lines = text.split("\n")
    spans: list[CodeSpan] = []
    for token in _MD.parse(text):
        if token.type != "inline" or token.children is None:
            continue
        if token.map is not None:
            start_line = token.map[0]
            block_text = "\n".join(source_lines[token.map[0] : token.map[1]])
        else:
            start_line = None
            block_text = ""
        cursor = 0
        current_href: str | None = None
        for child in token.children:
            if child.type == "link_open":
                current_href = str(child.attrGet("href") or "")
            elif child.type == "link_close":
                current_href = None
            elif child.type == "code_inline":
                line_offset, cursor = _locate_span(block_text, child.content, cursor)
                if current_href is not None and child.content.strip() == current_href.strip():
                    continue
                line = start_line + 1 + line_offset if start_line is not None else None
                spans.append(CodeSpan(content=child.content, line=line, href=current_href))
    return spans


_FIELD_IDENT = re.compile(r"[a-z_][a-z0-9_]*")


@dataclass(frozen=True)
class FieldEnumeration:
    citation: str  # the citation code-span text (path::Symbol)
    fields: tuple[str, ...]
    line: int | None


def find_field_enumerations(text: str) -> list[FieldEnumeration]:
    """Find sealed field lists of the form `` `path::Class`: `a`, `b`, `c`) ``.

    A citation code-span immediately followed by a text ``:`` intro, then a run
    of backticked bare identifiers separated only by ``,``/``and``/whitespace,
    terminated by a ``)``. Only this sealed shape is returned, so a prose
    sentence that happens to mention a field or two is not mistaken for a
    complete enumeration (#615)."""
    source_lines = text.split("\n")
    results: list[FieldEnumeration] = []
    for token in _MD.parse(text):
        if token.type != "inline" or token.children is None:
            continue
        if token.map is not None:
            start_line = token.map[0]
            block_text = "\n".join(source_lines[token.map[0] : token.map[1]])
        else:
            start_line = None
            block_text = ""
        cursor = 0
        children = token.children
        # Pre-compute each code_inline's line by walking cursor in order.
        line_by_index: dict[int, int | None] = {}
        for idx, child in enumerate(children):
            if child.type == "code_inline":
                line_offset, cursor = _locate_span(block_text, child.content, cursor)
                line_by_index[idx] = (
                    start_line + 1 + line_offset if start_line is not None else None
                )
        for idx, child in enumerate(children):
            if child.type != "code_inline":
                continue
            if "::" not in child.content:
                continue
            enum = _parse_field_run(children, idx)
            if enum is not None:
                results.append(
                    FieldEnumeration(
                        citation=child.content,
                        fields=enum,
                        line=line_by_index.get(idx),
                    )
                )
    return results


def _parse_field_run(children: list[Token], start_idx: int) -> tuple[str, ...] | None:
    """Return the sealed field tuple following the citation at ``start_idx``, or
    None if the shape is not `` : `field`, `field`, ... ) ``."""
    intro = children[start_idx + 1] if start_idx + 1 < len(children) else None
    if intro is None or intro.type != "text":
        return None
    text_after = intro.content
    if ":" not in text_after:
        return None
    # Everything after the first ':' in the intro token must be blank (the
    # fields are their own code spans). Rejects ": something prose".
    if text_after.split(":", 1)[1].strip() != "":
        return None
    fields: list[str] = []
    j = start_idx + 2
    while j < len(children):
        c = children[j]
        if c.type == "code_inline":
            if not _FIELD_IDENT.fullmatch(c.content):
                return None
            fields.append(c.content)
            j += 1
            continue
        if c.type == "text":
            if ")" in c.content:
                before = c.content.split(")", 1)[0]
                if before.strip(" ,") in ("", "and"):
                    return tuple(fields) if fields else None
                return None
            if c.content.strip(" ,") in ("", "and"):
                j += 1
                continue
            return None
        return None
    return None


@dataclass(frozen=True)
class Classification:
    """The bucket a code span falls into, with the parts a citation carries."""

    kind: str  # "citation" | "shorthand_error" | "not_a_citation"
    path: str = ""
    symbol: str = ""


def _is_doc_relative(path: str) -> bool:
    """A ``..``/``.``-anchored path is doc-relative navigation, not a citation.

    The gate resolves every citation repo-root-relative (see ``path_exists``),
    so a path beginning ``../`` or ``./`` cannot be a repo-root citation by the
    spec's own rule. It is a doc-relative link, a link checker's concern.
    """
    return path.startswith("../") or path.startswith("./")


def _is_glob_or_placeholder(path: str) -> bool:
    """A path part carrying ``*``, ``<``, or ``>`` is a pattern/template.

    Examples: ``skills/**/SKILL.md``, ``skills/<name>/SKILL.md``. These name a
    shape, not a concrete file, so they are not checkable citations.
    """
    return any(ch in path for ch in ("*", "<", ">"))


def classify(content: str) -> Classification:
    """Sort a code span into exactly one of the three buckets.

    The path portion is the part before ``::`` when present, otherwise the
    whole span; that portion is normalized once (doc-relative/glob exclusion,
    then the path-with-extension citation check) so the ``::`` and no-``::``
    forms cannot drift out of sync with each other.
    """
    path_part, sep, symbol_part = content.partition("::")
    if _is_doc_relative(path_part) or _is_glob_or_placeholder(path_part):
        return Classification("not_a_citation")
    if "/" in path_part and _has_source_extension(path_part):
        return Classification("citation", path=path_part, symbol=symbol_part)
    if sep and _has_source_extension(path_part):
        # Symbol form with no path: invisible to the gate, so a hard error.
        return Classification("shorthand_error")
    return Classification("not_a_citation")


def scan_raw_line_ban(text: str) -> Iterator[LineBanHit]:
    """Yield every banned line coordinate in ``text`` (before suppression).

    Scans the raw file text, including fenced blocks and prose, because the
    goal is that the rotten coordinate form does not appear at all. The
    per-line escape hatch is applied by the caller through
    ``is_line_suppressed`` so that suppression and its accounting live in one
    place across every finding type (line-ban, path, symbol, shorthand).
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        for match in _LINE_BAN.finditer(line):
            yield LineBanHit(coordinate=match.group(0), line=index + 1)


def _is_standalone_ignore(line: str) -> bool:
    """True when ``line`` is a ``doclint:ignore-line`` comment and nothing else."""
    return _IGNORE_LINE in line and not line.replace(_IGNORE_LINE, "").strip()


def is_line_suppressed(lines: list[str], line_no: int) -> bool:
    """True when a finding on 1-based ``line_no`` is silenced by the hatch.

    Two forms, and each silences exactly one physical line:

    * inline form: a ``<!-- doclint:ignore-line -->`` at the end of a physical
      line that also carries content suppresses findings on that same line;
    * preceding-line form: a standalone comment (the marker alone on its own
      line) suppresses findings on the next line.

    The two are mutually exclusive by construction: a marker sharing a line with
    content is inline and cannot double as a preceding-line comment for the line
    below, so an inline suppression never bleeds onto the following line. There
    is no file-level, paragraph-level, or directory-level ignore, by design.
    ``lines`` is the file's ``splitlines()`` output.
    """
    if line_no < 1 or line_no > len(lines):
        return False
    same = lines[line_no - 1]
    if _IGNORE_LINE in same and not _is_standalone_ignore(same):
        return True
    return line_no >= 2 and _is_standalone_ignore(lines[line_no - 2])


def path_exists(repo_root: Path, rel_path: str) -> bool:
    """Repo-root-relative existence check. Never doc-relative."""
    return (repo_root / rel_path).is_file()


def _is_external_url(href: str) -> bool:
    """True when ``href`` is an absolute ``http(s)`` URL, not an in-tree path."""
    lowered = href.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def link_target_matches_citation(
    repo_root: Path, doc_rel: str, cite_path: str, href: str
) -> bool:
    """True when a decorated citation's link target points at the cited file.

    A ``[`path::Symbol`](target)`` link asserts two things about the same file:
    its citation text (repo-root-relative ``cite_path``) and its navigable
    ``target``. When the two name different files the reader is sent somewhere
    the citation does not describe -- the exact drift #575 left in ARCHITECTURE.md,
    where the text cited ``types.py`` but the link went to ``k8s.py``.

    A relative href is resolved with normal markdown semantics (relative to the
    doc's own directory), while a leading-slash href is repo-root-anchored (as a
    site serves ``/path``) and resolved from the repo root -- so a nested doc can
    cite a file with a root-relative link without false-failing. The comparison
    target is the citation resolved from the repo root.

    Two hrefs describe no in-repo file to compare against and are treated as
    consistent: a pure-anchor href (``#section``, no path) is a same-doc link,
    and an absolute external URL (``http://``/``https://``) points outside the
    tree entirely.
    """

    if _is_external_url(href):
        return True

    href_path = href.split("#", 1)[0].split("?", 1)[0].strip()
    if not href_path:
        return True
    if href_path.startswith("/"):
        href_abs = (repo_root / href_path.lstrip("/")).resolve()
    else:
        doc_dir = (repo_root / doc_rel).parent
        href_abs = (doc_dir / href_path).resolve()
    cite_abs = (repo_root / cite_path).resolve()
    return href_abs == cite_abs
