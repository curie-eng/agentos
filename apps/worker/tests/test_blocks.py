"""Structured-reply rendering + the curie-reply parsing convention (pure)."""

from __future__ import annotations

import json

from channel_protocol import Action, ChoiceIntent, ConfirmIntent, OutboundMessage
from curie_worker.behaviorpacks import NavPack
from curie_worker.blocks import Reply, _reply_from_message, chunk, parse_reply, render, to_blocks

# An enabled nav pack, same shape as tests/test_behaviorpacks.py.
_NAV = NavPack(enabled=True, hub_label="Help", hub_command="help")


def _action_ids(blocks: list[dict]) -> list[str]:
    return [
        e["action_id"]
        for b in blocks
        if b["type"] == "actions"
        for e in b["elements"]
    ]


def _action_labels(blocks: list[dict]) -> list[str]:
    return [
        e["text"]["text"]
        for b in blocks
        if b["type"] == "actions"
        for e in b["elements"]
    ]

_BLOCK = """Here you go:

```curie-reply
{"header": "Top leaks", "text": "**3** open", "fields": [["Open", "3"]],
 "buttons": [["Details", "details"], ["← Back", "home"]], "footer": "now"}
```"""


def _types(blocks: list[dict]) -> list[str]:
    return [b["type"] for b in blocks]


def test_to_blocks_orders_and_renders_parts() -> None:
    reply = Reply(
        text="body",
        header="Title",
        fields=[("A", "1")],
        buttons=[("Go", "go")],
        footer="foot",
    )
    assert _types(to_blocks(reply)) == ["header", "section", "section", "actions", "context"]


def test_to_blocks_sorts_nav_buttons_leftmost() -> None:
    reply = Reply(text="x", buttons=[("Details", "details"), ("← Back", "home")])
    actions = next(b for b in to_blocks(reply) if b["type"] == "actions")
    labels = [e["text"]["text"] for e in actions["elements"]]
    assert labels == ["← Back", "Details"]  # nav sorted to the front


def test_confirm_intent_prompt_reaches_the_reply() -> None:
    """#454: ConfirmIntent's `prompt` -- the actual question -- must not be
    dropped when projecting the channel-neutral contract into this Slack
    renderer's Reply. Before this fix, an approver saw bare Approve/Cancel
    buttons with no question attached."""
    message = OutboundMessage(
        version="1.0",
        text="Ready when you are.",
        interaction=ConfirmIntent(
            kind="confirm",
            id="deploy",
            prompt="Deploy revenue-leak to prod?",
            confirm=Action(label="Deploy", value="deploy"),
            cancel=Action(label="Cancel", value="cancel"),
        ),
    )
    reply = _reply_from_message(message)
    assert reply.action_prompt == "Deploy revenue-leak to prod?"


def test_choice_intent_prompt_reaches_the_reply() -> None:
    message = OutboundMessage(
        version="1.0",
        text="Pick one.",
        interaction=ChoiceIntent(
            kind="choice",
            id="repo",
            prompt="Which repository?",
            options=[Action(label="Curie", value="curie-eng/curie")],
        ),
    )
    reply = _reply_from_message(message)
    assert reply.action_prompt == "Which repository?"


def test_choice_intent_with_no_prompt_reaches_the_reply_as_none() -> None:
    # ChoiceIntent.prompt is optional (ConfirmIntent.prompt is not); a choice
    # with no prompt renders no extra section, not an empty one.
    message = OutboundMessage(
        version="1.0",
        text="Pick one.",
        interaction=ChoiceIntent(
            kind="choice",
            id="repo",
            options=[Action(label="Curie", value="curie-eng/curie")],
        ),
    )
    reply = _reply_from_message(message)
    assert reply.action_prompt is None


def test_action_prompt_renders_as_its_own_section_before_the_actions_block() -> None:
    reply = Reply(
        text="body",
        buttons=[("Deploy", "deploy"), ("Cancel", "cancel")],
        action_prompt="Deploy revenue-leak to prod?",
    )
    blocks = to_blocks(reply)
    assert _types(blocks) == ["section", "section", "actions"]
    assert blocks[1]["text"]["text"] == "Deploy revenue-leak to prod?"


def test_no_action_prompt_adds_no_extra_section() -> None:
    reply = Reply(text="body", buttons=[("Go", "go")])
    assert _types(to_blocks(reply)) == ["section", "actions"]


def test_chunk_splits_long_text_under_limit() -> None:
    pieces = chunk("x" * 7000, limit=2900)
    assert all(len(p) <= 2900 for p in pieces)
    assert "".join(pieces) == "x" * 7000


def test_parse_reply_extracts_a_complete_block() -> None:
    reply = parse_reply(_BLOCK)
    assert reply is not None
    assert reply.header == "Top leaks"
    assert reply.fields == [("Open", "3")]
    assert ("← Back", "home") in reply.buttons


def test_parse_reply_maps_versioned_choice_contract_to_slack_actions() -> None:
    reply = parse_reply(
        "```curie-reply\n"
        '{"version":"1.0","text":"Which view?","interaction":'
        '{"kind":"choice","id":"view","options":['
        '{"label":"Open issues","value":"show open issues"}]}}\n'
        "```"
    )
    assert reply is not None
    assert reply.text == "Which view?"
    assert reply.buttons == [("Open issues", "show open issues")]


def test_parse_reply_maps_versioned_confirmation_to_two_actions() -> None:
    reply = parse_reply(
        "```curie-reply\n"
        '{"version":"1.0","text":"Deploy?","interaction":'
        '{"kind":"confirm","id":"deploy","prompt":"Deploy?",'
        '"confirm":{"label":"Deploy","value":"deploy"},'
        '"cancel":{"label":"Cancel","value":"cancel"}}}\n'
        "```"
    )
    assert reply is not None
    assert reply.buttons == [("Deploy", "deploy"), ("Cancel", "cancel")]


def test_versioned_reply_rejects_unknown_channel_native_fields() -> None:
    assert (
        parse_reply(
            '```curie-reply\n{"version":"1.0","text":"x","blocks":[]}\n```'
        )
        is None
    )


def test_parse_reply_none_without_a_block() -> None:
    assert parse_reply("just a normal answer") is None


def test_parse_reply_defensive_on_bad_json() -> None:
    assert parse_reply("```curie-reply\n{not json}\n```") is None


def test_render_complete_block_returns_blocks() -> None:
    text, blocks = render(_BLOCK)
    assert blocks is not None
    assert _types(blocks)[0] == "header"
    assert "raw" not in text.lower()  # fallback text is the reply body, not JSON


def test_render_hides_half_streamed_block() -> None:
    # Fence opened mid-stream, not yet closed: never show the raw JSON.
    partial = "Working...\n```curie-reply\n{\"header\": \"T"
    text, blocks = render(partial)
    assert blocks is None
    assert "curie-reply" not in text
    assert text.startswith("Working")


def test_render_plain_text_is_mrkdwn() -> None:
    text, blocks = render("**hi** [x](http://y)")
    assert blocks is None
    assert text == "*hi* <http://y|x>"


# -- nav pack wiring into the render path -------------------------------------
# Contract: to_blocks(reply, nav=None) / render(text, nav=None) gain an optional
# nav: NavPack | None. When nav is enabled, the hub button is appended to the
# actions block before it is emitted; when nav is None/disabled, output is
# byte-identical to today (no hub button).


def test_to_blocks_appends_hub_button_when_nav_enabled() -> None:
    reply = Reply(text="x", buttons=[("Details", "details")])
    blocks = to_blocks(reply, nav=_NAV)
    assert "help" in _action_ids(blocks)  # the hub command reached the actions block
    assert "← Help" in _action_labels(blocks)  # left-arrow: no back button present


def test_to_blocks_nav_none_is_identical_to_no_nav() -> None:
    reply = Reply(text="x", buttons=[("Details", "details")])
    assert to_blocks(reply, nav=None) == to_blocks(reply)
    assert "help" not in _action_ids(to_blocks(reply, nav=None))


def test_to_blocks_disabled_nav_adds_no_hub_button() -> None:
    disabled = NavPack(enabled=False, hub_label="Help", hub_command="help")
    reply = Reply(text="x", buttons=[("Details", "details")])
    assert to_blocks(reply, nav=disabled) == to_blocks(reply)
    assert "help" not in _action_ids(to_blocks(reply, nav=disabled))


def test_render_applies_nav_hub_button() -> None:
    # _BLOCK already carries a "← Back" nav button, so the hub is above -> "↑".
    _text, blocks = render(_BLOCK, nav=_NAV)
    assert blocks is not None
    assert "help" in _action_ids(blocks)
    assert "↑ Help" in _action_labels(blocks)


def test_render_without_nav_has_no_hub_button() -> None:
    _text, blocks = render(_BLOCK)
    assert blocks is not None
    assert "help" not in _action_ids(blocks)


# --- #31: status context + link buttons --------------------------------------


def test_status_renders_as_first_context_block() -> None:
    reply = Reply(text="body", status="Running the numbers")
    blocks = to_blocks(reply)
    first = blocks[0]
    assert first["type"] == "context"
    assert first["elements"] == [{"type": "mrkdwn", "text": "Running the numbers"}]


def test_status_is_rendered_as_authored_not_mrkdwn_converted() -> None:
    # Like footer, status is a context line rendered as-authored (no Markdown ->
    # mrkdwn conversion); a Markdown link stays verbatim rather than <url|label>.
    reply = Reply(text="body", status="**bold** [x](http://y)")
    first = to_blocks(reply)[0]
    assert first["type"] == "context"
    assert first["elements"][0]["text"] == "**bold** [x](http://y)"


def test_parse_reply_extracts_status() -> None:
    reply = parse_reply('```curie-reply\n{"status": "Working", "text": "b"}\n```')
    assert reply is not None
    assert reply.status == "Working"


def test_parse_reply_status_is_none_without_the_key() -> None:
    reply = parse_reply('```curie-reply\n{"text": "b"}\n```')
    assert reply is not None
    assert reply.status is None


def test_links_render_as_url_buttons() -> None:
    reply = Reply(text="x", links=[("Docs", "https://x/y")])
    actions = [b for b in to_blocks(reply) if b["type"] == "actions"]
    assert len(actions) == 1
    elements = actions[0]["elements"]
    assert len(elements) == 1
    el = elements[0]
    assert el["url"] == "https://x/y"
    assert el["text"]["text"] == "Docs"
    assert "action_id" not in el  # a URL button is interactive without the dispatcher


def test_links_with_invalid_url_are_dropped() -> None:
    # Only the absolute http(s) link survives; empty / relative / non-URL are dropped
    # so one malformed url never makes Slack reject the whole reply.
    reply = Reply(
        text="x",
        links=[("Good", "https://ok.com"), ("Empty", ""), ("Rel", "/relative"), ("Bad", "None")],
    )
    actions = [b for b in to_blocks(reply) if b["type"] == "actions"]
    assert len(actions) == 1
    elements = actions[0]["elements"]
    assert len(elements) == 1
    assert elements[0]["url"] == "https://ok.com"
    labels = [e["text"]["text"] for e in elements]
    assert "Empty" not in labels
    assert "Rel" not in labels
    assert "Bad" not in labels


def test_links_all_invalid_emits_no_actions_block() -> None:
    # Every link url is invalid and there are no buttons: no actions block at all.
    reply = Reply(text="x", links=[("Empty", ""), ("Rel", "/relative"), ("Bad", "None")])
    assert not any(b["type"] == "actions" for b in to_blocks(reply))


def test_parse_reply_extracts_links() -> None:
    reply = parse_reply('```curie-reply\n{"links": [["Docs", "https://x/y"]], "text": "b"}\n```')
    assert reply is not None
    assert reply.links == [("Docs", "https://x/y")]


def test_parse_reply_drops_malformed_links() -> None:
    reply = parse_reply(
        '```curie-reply\n'
        '{"links": [["only-one"], ["a", "b", "c"], ["Docs", "https://x/y"]], "text": "b"}\n'
        '```'
    )
    assert reply is not None
    assert reply.links == [("Docs", "https://x/y")]  # 1-elem and 3-elem entries dropped


def test_to_blocks_full_order_with_status_and_links() -> None:
    reply = Reply(
        text="body",
        status="Working",
        header="Title",
        fields=[("A", "1")],
        buttons=[("Go", "go")],
        links=[("Docs", "https://x/y")],
        footer="foot",
    )
    blocks = to_blocks(reply)
    assert _types(blocks) == [
        "context",  # status first
        "header",
        "section",  # fields
        "section",  # body
        "actions",  # buttons
        "actions",  # links
        "context",  # footer last
    ]
    actions = [b for b in blocks if b["type"] == "actions"]
    # First actions block is buttons (action_id elements), second is links (url).
    assert all("action_id" in e for e in actions[0]["elements"])
    assert all("url" in e for e in actions[1]["elements"])
    # status context is first, footer context is last.
    assert blocks[0]["elements"][0]["text"] == "Working"
    assert blocks[-1]["elements"][0]["text"] == "foot"


def test_backward_compatible_without_status_or_links() -> None:
    # Defaults (status None, links empty) add ZERO blocks: identical to today.
    reply = Reply(header="H", text="body", footer="f")
    blocks = to_blocks(reply)
    assert _types(blocks) == ["header", "section", "context"]
    # No status context leading, and no links actions block anywhere.
    assert not any(b["type"] == "actions" for b in blocks)
    assert blocks[0]["type"] == "header"  # header first, not a status context


# --- #228: clamp Block Kit output to Slack's hard limits ----------------------
# Slack rejects the whole message (and the turn's reply is lost) if any block
# exceeds its documented cap. to_blocks must clamp so the reply is always
# deliverable, whatever the model authored. Tests assert the length/count
# invariant only, leaving the exact truncation style to the implementer.


def test_header_text_clamped_to_150() -> None:
    reply = Reply(text="body", header="H" * 200)
    header = next(b for b in to_blocks(reply) if b["type"] == "header")
    assert len(header["text"]["text"]) <= 150


def test_button_label_clamped_to_75() -> None:
    reply = Reply(text="body", buttons=[("x" * 200, "y" * 400)])
    actions = next(b for b in to_blocks(reply) if b["type"] == "actions")
    element = actions["elements"][0]
    assert len(element["text"]["text"]) <= 75


def test_action_id_clamped_to_255() -> None:
    reply = Reply(text="body", buttons=[("x" * 200, "y" * 400)])
    actions = next(b for b in to_blocks(reply) if b["type"] == "actions")
    element = actions["elements"][0]
    assert len(element["action_id"]) <= 255


def test_section_fields_capped_at_10() -> None:
    reply = Reply(text="body", fields=[(f"k{i}", f"v{i}") for i in range(15)])
    section = next(
        b for b in to_blocks(reply) if b["type"] == "section" and "fields" in b
    )
    assert len(section["fields"]) == 10


def test_total_blocks_capped_at_50() -> None:
    # ~150000 non-newline chars chunk into ~52 sections (target ~2900); with a
    # header on top that is well over Slack's 50-block ceiling.
    reply = Reply(text="x" * 150_000, header="Title")
    assert len(to_blocks(reply)) <= 50


def test_render_bounds_fallback_text_for_oversized_body() -> None:
    # A complete structured reply whose BODY is far over Slack's chat.update
    # text cap (~40000). render must still return blocks (it is a complete
    # reply) AND bound the accessibility/fallback text, or the text-only retry
    # in AsyncSlackSink.update re-raises and re-opens the unbounded paid loop.
    text = "```curie-reply\n" + json.dumps({"header": "H", "text": "x" * 60000}) + "\n```"
    rendered_text, blocks = render(text)
    assert blocks is not None
    assert len(rendered_text) <= 40000


def test_approval_card_shape_and_click_contract() -> None:
    # The card carries what needs approval, who asked, and two buttons whose
    # action ids are the dispatcher's click contract with the record id as value.
    from curie_dispatcher.approval_actions import APPROVE_ACTION_ID, REJECT_ACTION_ID
    from curie_worker.blocks import approval_card

    fallback, card = approval_card(
        approval_id="appr-1",
        summary="Give ACME a 20% discount",
        requested_by="U_AE",
    )
    assert "Give ACME a 20% discount" in fallback
    assert card[0]["type"] == "header"
    assert "Give ACME a 20% discount" in card[1]["text"]["text"]
    assert "<@U_AE>" in card[2]["elements"][0]["text"]

    actions = card[-1]
    assert actions["type"] == "actions"
    approve, reject = actions["elements"]
    assert approve["action_id"] == APPROVE_ACTION_ID
    assert approve["value"] == "appr-1"
    assert approve["style"] == "primary"
    assert reject["action_id"] == REJECT_ACTION_ID
    assert reject["value"] == "appr-1"
    assert reject["style"] == "danger"


def test_approval_card_renders_allow_free_text_as_the_note_buttons() -> None:
    """ADR-0020's semantic field, rendered by this adapter (#1053).

    `allow_free_text` says only "this decision may carry free text"; Slack
    expresses that as a dialog opened by the click, so the card swaps in the
    note-collecting action ids. Everything else about the card is unchanged --
    the note is an enrichment, not a different card.
    """

    from curie_dispatcher.approval_actions import (
        APPROVE_ACTION_ID,
        APPROVE_NOTE_ACTION_ID,
        REJECT_ACTION_ID,
        REJECT_NOTE_ACTION_ID,
    )
    from curie_worker.blocks import approval_card

    _, plain = approval_card(
        approval_id="appr-1", summary="Give ACME a 20% discount", requested_by="U_AE"
    )
    _, with_note = approval_card(
        approval_id="appr-1",
        summary="Give ACME a 20% discount",
        requested_by="U_AE",
        allow_free_text=True,
    )

    plain_approve, plain_reject = plain[-1]["elements"]
    note_approve, note_reject = with_note[-1]["elements"]
    assert plain_approve["action_id"] == APPROVE_ACTION_ID
    assert plain_reject["action_id"] == REJECT_ACTION_ID
    assert note_approve["action_id"] == APPROVE_NOTE_ACTION_ID
    assert note_reject["action_id"] == REJECT_NOTE_ACTION_ID

    # Only the action ids differ: same record id, same styles, same body blocks.
    assert note_approve["value"] == note_reject["value"] == "appr-1"
    assert note_approve["style"] == "primary" and note_reject["style"] == "danger"
    assert plain[:-1] == with_note[:-1]

    # The CLI's Slack stub detects a posted card by matching the shared prefix
    # of both Approve ids (cli/src/chat.rs APPROVE_ACTION_ID_PREFIX). Pin that
    # relationship here: a rename that broke it would blind `local message` to
    # awaiting-approval turns, and nothing else would fail.
    assert APPROVE_NOTE_ACTION_ID.startswith(APPROVE_ACTION_ID)


def test_approval_card_clamps_oversized_summary() -> None:
    from curie_worker.blocks import approval_card

    fallback, card = approval_card(
        approval_id="appr-2", summary="x" * 10000, requested_by="U1"
    )
    # Section mrkdwn stays under Slack's 3000-char cap; fallback under 40k.
    assert len(card[1]["text"]["text"]) <= 2900
    assert len(fallback) <= 39000


def test_expired_approval_card_drops_buttons_and_marks_expired() -> None:
    # #419: the expired form keeps the summary but has NO actions block, and
    # states it can no longer be resolved -- so the card cannot be clicked.
    from curie_worker.blocks import expired_approval_card

    fallback, card = expired_approval_card(summary="Give ACME a 20% discount")
    assert "Give ACME a 20% discount" in fallback
    assert card[0]["type"] == "header"
    assert "Give ACME a 20% discount" in card[1]["text"]["text"]
    assert all(block.get("type") != "actions" for block in card)
    assert "expired" in str(card[-1]).lower()


def test_expired_approval_card_clamps_oversized_summary() -> None:
    from curie_worker.blocks import expired_approval_card

    fallback, card = expired_approval_card(summary="x" * 10000)
    assert len(card[1]["text"]["text"]) <= 2900
    assert len(fallback) <= 39000
