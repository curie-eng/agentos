"""THROWAWAY SPIKE for #2509 (v0.8.8): completion_events retention and recovery.

Not product code and not a regression suite. Every candidate policy lives in
this file; `state.py` is untouched. Observations are printed (run with `-s`)
and copied into the dated spike report. Delete this file once #2509 lands.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from curie_mail_adapter.adapter import EMPTY_REPLY_TEXT, EVENT_MARKER, MailAdapter
from curie_mail_adapter.agentmail import AgentMailClient
from curie_mail_adapter.state import LEASE_SECONDS, MailState

# --------------------------------------------------------------------------
# Candidate policy under test (spike-local; the product has no analogue yet).
# --------------------------------------------------------------------------

TERMINAL_PREDICATE_OR = "(delivered=1 OR deleted=1)"
TERMINAL_PREDICATE_AND = "(delivered=1 AND deleted=1)"  # negative control: the literal issue text
NO_PREDICATE = "1=1"  # negative control: proves the tests discriminate


def compact_terminal_completions(
    connection: sqlite3.Connection,
    *,
    cap: int,
    incoming: int = 0,
    predicate: str = TERMINAL_PREDICATE_OR,
) -> int:
    """Oldest-first bounded eviction of terminal completion rows; returns rows evicted."""
    count = int(
        connection.execute(f"SELECT count(*) FROM completion_events WHERE {predicate}").fetchone()[
            0
        ]
    )
    excess = count + incoming - cap
    if excess <= 0:
        return 0
    cursor = connection.execute(
        "DELETE FROM completion_events WHERE event_id IN ("
        f"SELECT event_id FROM completion_events WHERE {predicate} "
        "ORDER BY updated_at, event_id LIMIT ?)",
        (excess,),
    )
    return int(cursor.rowcount)


def rows(state: MailState) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in state.connection.execute(
        "SELECT event_id, delivered, deleted, lease_owner, lease_until, updated_at "
        "FROM completion_events ORDER BY updated_at, event_id"
    ):
        out[r[0]] = {
            "delivered": r[1],
            "deleted": r[2],
            "lease_owner": r[3],
            "lease_until": r[4],
            "updated_at": r[5],
        }
    return out


def classify(row: dict[str, Any], now: float) -> str:
    if row["deleted"] == 1 and row["delivered"] == 1:
        return "delivered+deleted"
    if row["deleted"] == 1:
        return "deleted"
    if row["delivered"] == 1:
        return "delivered"
    if row["lease_owner"] and float(row["lease_until"] or 0) > now:
        return "leased"
    return "unresolved"


def pages(state: MailState) -> dict[str, int]:
    c = state.connection
    ps = int(c.execute("PRAGMA page_size").fetchone()[0])
    pc = int(c.execute("PRAGMA page_count").fetchone()[0])
    fl = int(c.execute("PRAGMA freelist_count").fetchone()[0])
    return {
        "page_size": ps,
        "page_count": pc,
        "freelist": fl,
        "used_bytes": ps * (pc - fl),
        "file_bytes": state.path.stat().st_size,
        "wal_bytes": Path(f"{state.path}-wal").stat().st_size
        if Path(f"{state.path}-wal").exists()
        else 0,
    }


def open_state(
    path: Path, *, max_bytes: int = 4 * 1024 * 1024, max_pending: int = 1000
) -> MailState:
    return MailState(str(path), max_pending=max_pending, max_bytes=max_bytes)


def set_updated_at(state: MailState, event_id: str, when: float) -> None:
    with state.transaction() as c:
        c.execute("UPDATE completion_events SET updated_at=? WHERE event_id=?", (when, event_id))


# --------------------------------------------------------------------------
# T1: real transitions and the four (five) terminal/non-terminal classes.
# --------------------------------------------------------------------------


def test_t1_transitions_produce_distinct_row_signatures(tmp_path: Path) -> None:
    state = open_state(tmp_path / "s.sqlite3")
    now = time.time()
    assert state.claim_event("ev-unres", "thr", "msg", "ownerA") == "claimed"
    state.release_event("ev-unres", "ownerA")  # claimed then released: unresolved
    assert state.claim_event("ev-leased", "thr", "msg", "ownerA") == "claimed"
    assert state.claim_event("ev-leased", "thr", "msg", "ownerB") == "busy"
    assert state.claim_event("ev-deliv", "thr", "msg", "ownerA") == "claimed"
    state.finish_event("ev-deliv")
    state.release_event("ev-deliv", "ownerA")
    assert state.claim_event("ev-deliv", "thr", "msg", "ownerB") == "done"
    assert state.claim_event("ev-del", "thr", "msg", "ownerA") == "claimed"
    state.delete_event("ev-del", "thr", "msg")
    assert state.claim_event("ev-del", "thr", "msg", "ownerB") == "deleted"
    # Both flags: delete after finish has no guard. The literal AND reading of
    # the issue text describes only this accidental class.
    assert state.claim_event("ev-both", "thr", "msg", "ownerA") == "claimed"
    state.finish_event("ev-both")
    state.delete_event("ev-both", "thr", "msg")
    assert state.claim_event("ev-both", "thr", "msg", "ownerB") == "deleted"
    # Expired lease is reclaimable by another owner (no restart needed).
    assert state.claim_event("ev-expired", "thr", "msg", "ownerA") == "claimed"
    set_lease = time.time() - 1
    with state.transaction() as c:
        c.execute(
            "UPDATE completion_events SET lease_until=? WHERE event_id='ev-expired'", (set_lease,)
        )
    assert state.claim_event("ev-expired", "thr", "msg", "ownerB") == "claimed"

    table = rows(state)
    classes = {k: classify(v, time.time()) for k, v in table.items()}
    print("\nT1 classes:", classes)
    assert classes == {
        "ev-unres": "unresolved",
        "ev-leased": "leased",
        "ev-deliv": "delivered",
        "ev-del": "deleted",
        "ev-both": "delivered+deleted",
        "ev-expired": "leased",
    }
    assert now < table["ev-leased"]["lease_until"] <= time.time() + LEASE_SECONDS
    # Delivered/deleted rows carry no lease: finish/delete both NULL it.
    assert table["ev-deliv"]["lease_owner"] is None and table["ev-del"]["lease_owner"] is None
    state.close()

    # Restart reclaims live leases on delivered=0 rows only.
    reopened = open_state(tmp_path / "s.sqlite3")
    after = rows(reopened)
    assert after["ev-leased"]["lease_owner"] is None
    assert after["ev-expired"]["lease_owner"] is None
    assert reopened.claim_event("ev-leased", "thr", "msg", "ownerC") == "claimed"
    reopened.close()


# --------------------------------------------------------------------------
# T2: candidate bounded oldest-first policy; unresolved and leased survive.
# --------------------------------------------------------------------------


def _seed_mixed(state: MailState) -> None:
    base = 1_000_000.0
    for i in range(8):
        state.claim_event(f"d{i}", "thr", "msg", "o")
        state.finish_event(f"d{i}")
        set_updated_at(state, f"d{i}", base + i)
    for i in range(3):
        state.claim_event(f"x{i}", "thr", "msg", "o")
        state.delete_event(f"x{i}", "thr", "msg")
        set_updated_at(state, f"x{i}", base - 10 + i)  # tombstones are the oldest
    for i in range(2):
        state.claim_event(f"u{i}", "thr", "msg", "o")
        state.release_event(f"u{i}", "o")
        set_updated_at(state, f"u{i}", base - 100)  # older than everything terminal
    for i in range(2):
        state.claim_event(f"l{i}", "thr", "msg", "leaser")
        set_updated_at(state, f"l{i}", base - 100)


def test_t2_policy_evicts_oldest_terminal_only_and_is_deterministic(tmp_path: Path) -> None:
    state = open_state(tmp_path / "s.sqlite3")
    _seed_mixed(state)
    before = rows(state)
    assert len(before) == 15
    with state.transaction() as c:
        evicted = compact_terminal_completions(c, cap=5)
    after = rows(state)
    print("\nT2 evicted:", evicted, "survivors:", list(after))
    assert evicted == 6
    # Survivors: all unresolved + leased (older than any terminal row) and the 5 newest terminal.
    assert {k for k in after if k[0] in "ul"} == {"u0", "u1", "l0", "l1"}
    assert [k for k in after if k[0] in "dx"] == ["d3", "d4", "d5", "d6", "d7"]
    # Evicted set is exactly the 6 oldest terminal rows: all 3 tombstones + d0..d2.
    assert set(before) - set(after) == {"x0", "x1", "x2", "d0", "d1", "d2"}
    # Idempotent: a second pass evicts nothing.
    with state.transaction() as c:
        assert compact_terminal_completions(c, cap=5) == 0
    # `incoming` reserves a slot before the insert, same as deliveries.
    with state.transaction() as c:
        assert compact_terminal_completions(c, cap=5, incoming=1) == 1
    assert "d3" not in rows(state)
    state.close()


def test_t2_tie_on_updated_at_breaks_on_event_id(tmp_path: Path) -> None:
    state = open_state(tmp_path / "s.sqlite3")
    for name in ("b", "a", "c"):
        state.claim_event(name, "thr", "msg", "o")
        state.finish_event(name)
        set_updated_at(state, name, 5.0)
    with state.transaction() as c:
        assert compact_terminal_completions(c, cap=1) == 2
    assert list(rows(state)) == ["c"]
    state.close()


def test_t2_negative_controls_show_the_predicate_matters(tmp_path: Path) -> None:
    # AND reading (the literal issue text): evicts nothing but the accidental both-flag class.
    state = open_state(tmp_path / "and.sqlite3")
    _seed_mixed(state)
    with state.transaction() as c:
        assert compact_terminal_completions(c, cap=0, predicate=TERMINAL_PREDICATE_AND) == 0
    assert len(rows(state)) == 15
    state.close()
    # No predicate: would evict unresolved and leased rows first (oldest), which
    # the CLAUDE.md invariant forbids. Proves T2's survival assertion is not vacuous.
    state = open_state(tmp_path / "none.sqlite3")
    _seed_mixed(state)
    with state.transaction() as c:
        compact_terminal_completions(c, cap=5, predicate=NO_PREDICATE)
    survivors = rows(state)
    assert not any(k[0] in "ul" for k in survivors), survivors
    state.close()


# --------------------------------------------------------------------------
# T3: late duplicate completion / tombstone replay after eviction, through
# the real MailAdapter.send_reply decision with a stub provider.
# --------------------------------------------------------------------------


class _StubClient(AgentMailClient):
    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.thread_status = 200
        self.thread_messages: list[dict[str, Any]] = []
        self.thread_body: Any = None  # for the 404 case: dict => provider-confirmed
        self.get_thread_calls = 0
        self.replies: list[tuple[str, str]] = []

    def get_thread(self, thread_id: str) -> tuple[int, Any]:
        self.get_thread_calls += 1
        if self.thread_status == 200:
            return 200, {"thread_id": thread_id, "messages": list(self.thread_messages)}
        return self.thread_status, self.thread_body

    def reply(self, message_id: str, text: str) -> tuple[int, Any]:
        self.replies.append((message_id, text))
        return 200, {"message_id": "sent-1"}


@pytest.fixture
def stub_adapter(make_config: Any) -> tuple[MailAdapter, _StubClient]:
    config = make_config()
    client = _StubClient(config)
    adapter = MailAdapter(config, client=client)
    return adapter, client


def _admit_turn(adapter: MailAdapter, conversation_id: str, reply_ref: str) -> None:
    adapter.state.admit({"message_id": reply_ref, "thread_id": conversation_id})
    adapter.state.store_turn(
        reply_ref, {"conversation_id": conversation_id, "reply_ref": reply_ref, "text": "hi"}
    )
    adapter.state.record_text(conversation_id, reply_ref, "answer", append=False, max_bytes=1 << 20)


def _evict_all_terminal(adapter: MailAdapter) -> int:
    with adapter.state.transaction() as c:
        return compact_terminal_completions(c, cap=0)


def test_t3a_retained_delivered_row_short_circuits_without_provider(stub_adapter: Any) -> None:
    adapter, client = stub_adapter
    _admit_turn(adapter, "thr", "msg")
    assert adapter.send_reply("ev1", "thr", "msg") == 200
    assert len(client.replies) == 1 and EVENT_MARKER in client.replies[0][1]
    calls = client.get_thread_calls
    assert adapter.send_reply("ev1", "thr", "msg") == 200  # late duplicate
    assert client.get_thread_calls == calls and len(client.replies) == 1
    print("\nT3a retained: duplicate -> 200, provider untouched")


def test_t3b_evicted_delivered_row_recovers_from_provider_witness(stub_adapter: Any) -> None:
    adapter, client = stub_adapter
    _admit_turn(adapter, "thr", "msg")
    assert adapter.send_reply("ev1", "thr", "msg") == 200
    sent_text = client.replies[0][1]
    assert _evict_all_terminal(adapter) == 1
    client.thread_messages = [{"text": sent_text}]  # provider still shows the marker
    calls = client.get_thread_calls
    assert adapter.send_reply("ev1", "thr", "msg") == 200
    assert client.get_thread_calls == calls + 1 and len(client.replies) == 1
    assert classify(rows(adapter.state)["ev1"], time.time()) == "delivered"  # bounded regrowth by 1
    print("\nT3b evicted+witness: duplicate -> 200 via one get_thread, no resend, row re-created")


def test_t3c_evicted_delivered_row_with_lost_witness_sends_an_empty_duplicate(
    stub_adapter: Any,
) -> None:
    """NEGATIVE RESULT: the replay horizon is the provider's retention of the marker mail."""
    adapter, client = stub_adapter
    _admit_turn(adapter, "thr", "msg")
    assert adapter.send_reply("ev1", "thr", "msg") == 200
    assert _evict_all_terminal(adapter) == 1
    client.thread_messages = []  # marker mail gone (retention/user deletion)
    status = adapter.send_reply("ev1", "thr", "msg")
    print("\nT3c evicted+no witness: status", status, "replies", [t for _, t in client.replies])
    assert status == 200
    assert len(client.replies) == 2
    assert client.replies[1][1].startswith(EMPTY_REPLY_TEXT)  # reply_text ignores active=0
    # Control: same lost witness WITH the row retained does not resend.
    _admit_turn(adapter, "thr2", "msg2")
    assert adapter.send_reply("ev2", "thr2", "msg2") == 200
    client.thread_messages = []
    assert adapter.send_reply("ev2", "thr2", "msg2") == 200
    assert len(client.replies) == 3  # ev2 sent once only


def test_t3d_evicted_delivered_row_and_evicted_reply_state_is_502_no_send(
    stub_adapter: Any,
) -> None:
    adapter, client = stub_adapter
    _admit_turn(adapter, "thr", "msg")
    assert adapter.send_reply("ev1", "thr", "msg") == 200
    _evict_all_terminal(adapter)
    client.thread_messages = []
    with adapter.state.transaction() as c:  # hypothetical future reply_state compaction
        c.execute("DELETE FROM reply_state WHERE conversation_id='thr'")
    assert adapter.send_reply("ev1", "thr", "msg") == 502
    assert len(client.replies) == 1
    print("\nT3d evicted row + evicted reply_state + no witness: 502, no send")


def test_t3e_retained_tombstone_is_410_without_provider(stub_adapter: Any) -> None:
    adapter, client = stub_adapter
    _admit_turn(adapter, "thr", "msg")
    client.thread_status, client.thread_body = 404, {"error": "not found"}
    assert adapter.send_reply("ev1", "thr", "msg") == 410
    calls = client.get_thread_calls
    assert adapter.send_reply("ev1", "thr", "msg") == 410
    assert client.get_thread_calls == calls and client.replies == []


def test_t3f_evicted_tombstone_replays_through_provider_404(stub_adapter: Any) -> None:
    adapter, client = stub_adapter
    _admit_turn(adapter, "thr", "msg")
    client.thread_status, client.thread_body = 404, {"error": "not found"}
    assert adapter.send_reply("ev1", "thr", "msg") == 410
    assert _evict_all_terminal(adapter) == 1
    calls = client.get_thread_calls
    assert adapter.send_reply("ev1", "thr", "msg") == 410  # re-tombstoned from the provider verdict
    assert client.get_thread_calls == calls + 1 and client.replies == []
    assert classify(rows(adapter.state)["ev1"], time.time()) == "deleted"
    # Provider ambiguity (gateway 404 without a body, or 500) after eviction: 502, never 410/200.
    _evict_all_terminal(adapter)
    client.thread_status, client.thread_body = 404, "<html>edge</html>"
    assert adapter.send_reply("ev1", "thr", "msg") == 502
    client.thread_status, client.thread_body = 500, None
    assert adapter.send_reply("ev1", "thr", "msg") == 502
    assert client.replies == []
    # Evicted tombstone AND provider thread later readable again with no marker:
    # the tombstoned reply is SENT (empty), because reply_state still exists.
    client.thread_status, client.thread_messages = 200, []
    assert adapter.send_reply("ev1", "thr", "msg") == 200
    assert len(client.replies) == 1
    print("\nT3f evicted tombstone: 404 -> 410; ambiguity -> 502; resurrected -> empty send")


# --------------------------------------------------------------------------
# T4: page accounting, reuse, and the startup byte-size refusal.
# --------------------------------------------------------------------------

SMALL_CAP = 256 * 1024  # 64 pages of 4 KiB


def _fill_until_full(state: MailState, prefix: str) -> int:
    n = 0
    while True:
        if state.admit({"message_id": f"{prefix}-probe-{n}", "thread_id": "t"}) == "full":
            return n
        # Withdraw the probe so only completion rows fill the file.
        with state.transaction() as c:
            c.execute("DELETE FROM deliveries WHERE message_id=?", (f"{prefix}-probe-{n}",))
        state.claim_event(
            f"{prefix}-{n:07d}", "conversation-" + "x" * 40, "reply-" + "y" * 40, "owner"
        )
        state.finish_event(f"{prefix}-{n:07d}")
        n += 1
        assert n < 200_000


def test_t4a_delete_frees_pages_that_are_reused_but_the_file_never_shrinks(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    state = open_state(path, max_bytes=SMALL_CAP)
    n = _fill_until_full(state, "a")
    state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    full = pages(state)
    print("\nT4a full after", n, "delivered rows:", full)
    assert full["used_bytes"] >= SMALL_CAP or full["page_count"] * full["page_size"] >= SMALL_CAP
    assert state.admit({"message_id": "late", "thread_id": "t"}) == "full"

    with state.transaction() as c:
        evicted = compact_terminal_completions(c, cap=8)
    state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    freed = pages(state)
    print("T4a after evicting", evicted, ":", freed)
    assert freed["freelist"] > 0
    assert freed["file_bytes"] == full["file_bytes"]  # no shrink without VACUUM
    assert freed["used_bytes"] < full["used_bytes"]
    # Ingress recovers: admission is open again.
    assert state.admit({"message_id": "late", "thread_id": "t"}) == "admitted"
    # Freed pages are reused: refilling grows page_count by zero (or one for the
    # deliveries row above) until the freelist is drained.
    before_refill = pages(state)
    for i in range(evicted // 2):
        state.claim_event(f"b-{i:07d}", "conversation-" + "x" * 40, "reply-" + "y" * 40, "owner")
        state.finish_event(f"b-{i:07d}")
    state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    refilled = pages(state)
    print("T4a after refilling half:", refilled)
    assert refilled["page_count"] == before_refill["page_count"]
    assert refilled["freelist"] < before_refill["freelist"]
    state.close()
    # Restart at the same cap: st_size never exceeds max_page_count * page_size,
    # so the boot refusal does not fire from organic growth at an unchanged cap.
    print("T4a st_size", path.stat().st_size, "cap", SMALL_CAP)
    assert path.stat().st_size <= SMALL_CAP
    open_state(path, max_bytes=SMALL_CAP).close()


def test_t4b_boot_refusal_fires_only_when_the_cap_moves_and_delete_alone_does_not_recover(
    tmp_path: Path,
) -> None:
    path = tmp_path / "s.sqlite3"
    state = open_state(path, max_bytes=SMALL_CAP)
    _fill_until_full(state, "a")
    state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    state.close()
    lowered = SMALL_CAP // 2
    with pytest.raises(RuntimeError, match="CURIE_MAIL_MAX_STATE_BYTES"):
        open_state(path, max_bytes=lowered)
    # Operator remedy 1 (no SQL): raise the cap for one boot, compact, VACUUM.
    over = open_state(path, max_bytes=SMALL_CAP * 4)
    with over.transaction() as c:
        compact_terminal_completions(c, cap=8)
    over.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    over.close()
    # Negative control: deleting rows alone leaves st_size unchanged; the lowered cap still refuses.
    assert path.stat().st_size >= SMALL_CAP - 4096
    with pytest.raises(RuntimeError):
        open_state(path, max_bytes=lowered)
    # VACUUM on the (offline) copy is what returns the pages to the filesystem.
    over = open_state(path, max_bytes=SMALL_CAP * 4)
    over.connection.execute("VACUUM")
    over.close()
    shrunk = path.stat().st_size
    print("\nT4b after compact+VACUUM st_size", shrunk)
    assert shrunk < lowered
    recovered = open_state(path, max_bytes=lowered)
    assert recovered.admit({"message_id": "after", "thread_id": "t"}) == "admitted"
    assert len(rows(recovered)) == 8
    recovered.close()


def test_t4c_offline_copy_recovery_procedure_leaves_the_live_file_untouched(tmp_path: Path) -> None:
    """The supported shape: copy, recover the copy, verify, swap. Live file unread here."""
    live = tmp_path / "live.sqlite3"
    state = open_state(live, max_bytes=SMALL_CAP)
    _fill_until_full(state, "a")
    # leave one unresolved and one leased row that must survive
    state.claim_event("keep-unresolved", "c", "r", "o")
    state.release_event("keep-unresolved", "o")
    state.claim_event("keep-leased", "c", "r", "o")
    state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    state.close()
    live_bytes = live.read_bytes()
    copy = tmp_path / "copy.sqlite3"
    shutil.copy2(live, copy)  # WAL was checkpointed and truncated; copy the main file only
    # Recovery on the copy uses the real state API + VACUUM; a raised cap is the only override.
    rec = open_state(copy, max_bytes=SMALL_CAP * 4)
    with rec.transaction() as c:
        compact_terminal_completions(c, cap=8)
    rec.connection.execute("VACUUM")
    rec.close()
    assert live.read_bytes() == live_bytes  # live untouched
    check = open_state(copy, max_bytes=SMALL_CAP // 2)
    survivors = rows(check)
    assert "keep-unresolved" in survivors and "keep-leased" in survivors
    assert len(survivors) == 10
    assert check.admit({"message_id": "m", "thread_id": "t"}) == "admitted"
    check.close()
    print("\nT4c copy recovered:", copy.stat().st_size, "bytes; live still", live.stat().st_size)


def test_t4d_auto_vacuum_full_shrinks_without_vacuum_but_only_on_a_fresh_file(
    tmp_path: Path,
) -> None:
    """Candidate alternative: auto_vacuum=FULL set before the schema is created."""
    fresh = tmp_path / "fresh.sqlite3"
    raw = sqlite3.connect(fresh)
    raw.execute("PRAGMA auto_vacuum=FULL")
    raw.execute("VACUUM")
    raw.close()
    state = open_state(fresh, max_bytes=SMALL_CAP)
    assert int(state.connection.execute("PRAGMA auto_vacuum").fetchone()[0]) == 1
    n = _fill_until_full(state, "a")
    state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    full = pages(state)
    with state.transaction() as c:
        compact_terminal_completions(c, cap=8)
    state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    after = pages(state)
    print("\nT4d auto_vacuum=FULL:", n, "rows; full", full, "after", after)
    assert after["page_count"] < full["page_count"] and after["freelist"] == 0
    state.close()
    assert fresh.stat().st_size < SMALL_CAP // 2
    # Existing files: the pragma is a no-op after schema creation without a VACUUM.
    existing = tmp_path / "existing.sqlite3"
    open_state(existing).close()
    raw = sqlite3.connect(existing)
    raw.execute("PRAGMA auto_vacuum=FULL")
    assert int(raw.execute("PRAGMA auto_vacuum").fetchone()[0]) == 0
    raw.close()


# --------------------------------------------------------------------------
# T5: which tables actually grow per completed turn (issue premise check).
# --------------------------------------------------------------------------


def test_t5_per_turn_growth_by_table(tmp_path: Path) -> None:
    state = open_state(tmp_path / "s.sqlite3", max_bytes=64 * 1024 * 1024)
    body = "x" * 2000  # a modest 2 KB mail body
    n = 2000

    def used() -> int:
        state.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return pages(state)["used_bytes"]

    base = used()
    for i in range(n):
        mid = f"msg-{i:06d}"
        state.admit({"message_id": mid, "thread_id": f"thr-{i:06d}", "subject": "s", "from": "a@b"})
        state.store_turn(mid, {"conversation_id": f"thr-{i:06d}", "reply_ref": mid, "text": body})
        state.accept_ingress(mid)
    after_deliveries = used()
    for i in range(n):
        state.record_text(
            f"thr-{i:06d}", f"msg-{i:06d}", "answer " * 20, append=False, max_bytes=1 << 20
        )
        state.finish_reply(f"thr-{i:06d}", f"msg-{i:06d}")
    after_reply_state = used()
    for i in range(n):
        state.claim_event(f"evt_{i:032x}", f"thr-{i:06d}", f"msg-{i:06d}", "owner")
        state.finish_event(f"evt_{i:032x}")
    after_completions = used()
    per = {
        "deliveries(accepted, keeps turn_json)": (after_deliveries - base) / n,
        "reply_state(inactive)": (after_reply_state - after_deliveries) / n,
        "completion_events(delivered)": (after_completions - after_reply_state) / n,
    }
    retained_turn = state.connection.execute(
        "SELECT length(turn_json) FROM deliveries WHERE state='accepted' LIMIT 1"
    ).fetchone()[0]
    print(
        "\nT5 bytes per turn by table:",
        per,
        "| accepted row still holds turn_json bytes:",
        retained_turn,
    )
    assert retained_turn > 2000  # the mail body survives acceptance
    state.close()
