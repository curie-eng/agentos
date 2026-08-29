"""Derive a turn's prompt text from a Slack event, not just its top-level ``text`` (D1).

A Slack message delivered as Block Kit, as a legacy attachment, or as a bare
file share carries an empty (or whitespace-only) top-level ``text``. Reading
``event["text"]`` alone therefore mints a turn with no prompt in it: the message
is not dropped, it is *emptied*, which is the silent-loss defect class issue
#2006 closes. This module is the one place that answers "what did the human
actually say", so the ingest path in ``handlers.py`` stays a straight line.

**Design influence, no code copied.** The shape of this fix was informed by the
Vercel ``eve`` commit ``4464e4d`` (Apache-2.0) and Cloudflare's ``agents`` PR
#2129, both of which hit the same emptied-message failure in their own Slack
adapters. Neither the code nor any derivative of it is present here -- the
walker below was written against Slack's own Block Kit reference, and the
borrowing is conceptual (walk the payload, denylist the chrome, bound the
traversal). That is why the repository ``NOTICE`` is unchanged: nothing in this
file is a redistribution of third-party work.

**Passthrough is byte-identical.** When top-level ``text`` is non-empty after
``.strip()`` it is returned *unchanged*, including surrounding whitespace and
the ``<@U…>`` mention markup the worker relies on for addressing. Every enqueue
that works today is therefore bit-for-bit unaffected; derivation is strictly a
new path for payloads that would otherwise have produced nothing.

**Coverage is partial, and stated as such.** The walker is generic: it finds
recognized text-bearing nodes at any depth, *including inside block types it
does not know*, because Slack ships new block types faster than any adapter can
special-case them. It does not individually render the rich-text element types
absent from the table below -- ``date``, ``file``, ``citation``, message
mention, canvas reference, workflow mention, and work object. Those contribute
whatever recognized text they nest (plus a plain-string ``text`` value where
they happen to carry one, which is how ``date`` surfaces) and nothing more.
This is accepted partial coverage, not completeness.

**UI chrome is not message content.** Button labels, confirmation-dialog copy,
select placeholders, option lists and input hints are interface strings the
sender never wrote as prose. Feeding them to the model is both noise and a
prompt-injection surface, so the walker refuses to descend into those keys at
all. That refusal is also what makes the attachment ``fallback`` rule correct:
chrome can no longer be the thing that "yielded", so an attachment whose only
content is a button still emits its fallback string.

**The bounds bound traversal, not just the output.** A hostile or merely
enormous payload is bounded three ways -- recursion depth, visited-node count,
and accumulated characters -- and each stops the *walk*. Truncating only the
final joined string would still pay the CPU and memory cost of visiting a
200k-element flat list, so every budget is checked before descending.
"""

from typing import Any

#: Maximum recursion depth. Slack's own nesting (block -> elements ->
#: rich_text_section -> elements) is three or four levels; 12 leaves generous
#: headroom while keeping the walk far below Python's recursion limit.
_MAX_DEPTH = 12

#: Maximum characters charged against the walk. Reaching it stops traversal,
#: and the returned string is truncated to this length.
_MAX_CHARS = 40000

#: Maximum container/leaf nodes visited. Depth does not bound breadth: a flat
#: list of a million elements is depth 2 and would otherwise be walked whole.
_MAX_NODES = 5000

#: Keys whose subtrees are UI chrome, never message content. The walker does
#: not descend into them at any depth (an ``actions`` block's ``elements``
#: list is denied too, see ``_denied_keys``).
_CHROME_KEYS = frozenset(
    {"accessory", "confirm", "options", "option_groups", "placeholder", "hint"}
)

#: Composition-object and rich-text node types whose ``text`` is the content.
_TEXT_OBJECT_TYPES = frozenset({"plain_text", "mrkdwn", "text"})

#: Attachment keys emitted, in order, by the explicit attachment branch before
#: its ``fields``/``footer``/nested ``blocks`` are handled.
_ATTACHMENT_LEAD_KEYS = ("pretext", "title", "text")


class _Collector:
    """Ordered segment accumulator that owns the traversal budgets.

    Characters are charged for every candidate segment, including one that is
    then collapsed as a repeat of its immediate predecessor. The budget
    measures work done walking the payload, not the size of the result, which
    is what makes it a real bound on a bulky repetitive payload.
    """

    def __init__(self) -> None:
        self.segments: list[str] = []
        self.chars = 0
        self.nodes = 0

    @property
    def exhausted(self) -> bool:
        """True once either the character or the node budget is spent."""
        return self.chars >= _MAX_CHARS or self.nodes >= _MAX_NODES

    def charge_node(self) -> None:
        """Count one visited container node against the node budget."""
        self.nodes += 1

    def emit(self, value: object) -> None:
        """Append ``value`` as a segment when it is a non-blank string.

        Blank and non-string values are ignored. A segment identical to the
        one immediately before it is collapsed -- deliberately a trivial
        adjacent check and not a global dedupe index, which would drop
        legitimate repetition elsewhere in a long message.
        """
        if not isinstance(value, str) or not value.strip():
            return
        # Charged before the adjacency check: the walk did the work either way.
        self.chars += len(value) + 1
        if self.segments and self.segments[-1] == value:
            return
        self.segments.append(value)

    def render(self) -> str:
        """Join the collected segments and enforce the character cap."""
        return "\n".join(self.segments)[:_MAX_CHARS]


def _denied_keys(node_type: object) -> frozenset[str]:
    """Keys the walker must not descend into for a node of this type."""
    if node_type == "actions":
        # An actions block's elements are buttons; their labels are chrome.
        return _CHROME_KEYS | {"elements"}
    return _CHROME_KEYS


def _emit_recognized(node: dict[Any, Any], node_type: str, out: _Collector) -> bool:
    """Emit ``node`` per D1's type table; return True when it is consumed.

    A False return means the node is not a recognized leaf and the caller
    should keep recursing into it.
    """
    if node_type in _TEXT_OBJECT_TYPES:
        text = node.get("text")
        if isinstance(text, str):
            out.emit(text)
            return True
        return False
    if node_type == "link":
        text = node.get("text")
        out.emit(text if isinstance(text, str) and text.strip() else node.get("url"))
        return True
    if node_type == "emoji":
        name = node.get("name")
        if isinstance(name, str) and name.strip():
            out.emit(f":{name}:")
        return True
    if node_type == "user":
        user_id = node.get("user_id")
        if isinstance(user_id, str) and user_id.strip():
            out.emit(f"<@{user_id}>")
        return True
    if node_type == "channel":
        channel_id = node.get("channel_id")
        if isinstance(channel_id, str) and channel_id.strip():
            out.emit(f"<#{channel_id}>")
        return True
    if node_type == "usergroup":
        group_id = node.get("usergroup_id")
        if isinstance(group_id, str) and group_id.strip():
            out.emit(f"<!subteam^{group_id}>")
        return True
    if node_type == "broadcast":
        broadcast_range = node.get("range")
        if isinstance(broadcast_range, str) and broadcast_range.strip():
            out.emit(f"<!{broadcast_range}>")
        return True
    if node_type == "image":
        out.emit(node.get("alt_text"))
        return True
    return False


def _walk(node: object, depth: int, out: _Collector) -> None:
    """Recursively collect text-bearing content from ``node``.

    Scalars other than a recognized node's own text value are never emitted:
    ``block_id``, ``action_id``, urls of non-link nodes and the like are
    structure, not prose. Anything that is not a dict or a list is a dead end.
    """
    if out.exhausted or depth > _MAX_DEPTH:
        return
    if isinstance(node, list):
        out.charge_node()
        for item in node:
            if out.exhausted:
                return
            _walk(item, depth + 1, out)
        return
    if not isinstance(node, dict):
        return
    out.charge_node()

    node_type = node.get("type")
    if isinstance(node_type, str) and _emit_recognized(node, node_type, out):
        return

    skip = set(_denied_keys(node_type))
    if isinstance(node_type, str):
        # Accepted partial coverage: an unrecognized typed node whose own
        # ``text`` is a plain string still yields it (Slack's ``date`` element
        # is exactly this shape). A dict ``text`` is a composition object and
        # is left to the recursion below.
        own_text = node.get("text")
        if isinstance(own_text, str):
            out.emit(own_text)
            skip.add("text")

    for key, value in node.items():
        if key in skip:
            continue
        if out.exhausted:
            return
        _walk(value, depth + 1, out)


def _walk_attachment(attachment: object, out: _Collector) -> None:
    """Consume one legacy message attachment.

    The attachment dict is handled here **or** by the generic walker, never
    both: handing it to both paths would emit ``text``, ``title`` and every
    field twice. ``fallback`` is Slack's own plain-text summary of the rest of
    the attachment, so it is emitted only when nothing else in this attachment
    yielded anything.
    """
    if not isinstance(attachment, dict):
        return
    out.charge_node()
    before = len(out.segments)

    for key in _ATTACHMENT_LEAD_KEYS:
        out.emit(attachment.get(key))
    fields = attachment.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if out.exhausted:
                break
            if isinstance(field, dict):
                out.emit(field.get("title"))
                out.emit(field.get("value"))
    out.emit(attachment.get("footer"))
    _walk(attachment.get("blocks"), 1, out)

    if len(out.segments) == before:
        out.emit(attachment.get("fallback"))


def _emit_file(file_entry: object, out: _Collector) -> None:
    """Emit a shared file's human-readable label.

    ``file_share`` is admitted as actionable (D3), so a file dropped into a DM
    with no accompanying message must still derive *something* -- otherwise
    admitting it would open a brand-new silent-empty path, the exact defect
    this module exists to close.
    """
    if not isinstance(file_entry, dict):
        return
    out.charge_node()
    title = file_entry.get("title")
    out.emit(title if isinstance(title, str) and title.strip() else file_entry.get("name"))


def derive_text(event: dict[str, Any]) -> str:
    """Return the prompt text for a Slack message event.

    A non-empty top-level ``text`` (after ``.strip()``) is returned unchanged
    and nothing else is consulted. Otherwise the text is derived from
    ``blocks``, then ``attachments``, then ``files``, with non-empty segments
    joined by newlines in Slack's own serialization order.

    Never raises on a malformed payload: a missing, ``None``, or wrongly typed
    value at any position yields "" or the partial text collected so far.
    Callers get a plain string of at most ``_MAX_CHARS`` characters -- with the
    deliberate exception of the byte-identical passthrough above, which is
    returned exactly as Slack sent it.
    """
    raw_text = event.get("text")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text

    out = _Collector()
    _walk(event.get("blocks"), 1, out)

    attachments = event.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if out.exhausted:
                break
            _walk_attachment(attachment, out)

    files = event.get("files")
    if isinstance(files, list):
        for file_entry in files:
            if out.exhausted:
                break
            _emit_file(file_entry, out)

    return out.render()
