"""Durable KV state store (#23): real Postgres round-trip + compare-and-set.

Nothing mocked -- exercises the API against the compose Postgres (the
disposable-DB conftest provisions and migrates a throwaway database per run).
"""

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import asyncpg
from curie_api.config import get_settings
from curie_api.routers.state import _NAMESPACE_LOCK_CLASS, _namespace_lock_key
from curie_api.sandbox_token import mint
from sqlalchemy import make_url

# Scoped-sandbox-token auth matrix constants (#410).
_FAR_FUTURE = 4102444800  # 2100-01-01, valid at test time
_PAST = 1000000000  # 2001, expired at test time


def _agent(client: Any, headers: dict[str, str]) -> str:
    resp = client.post(
        "/agents",
        json={"name": "state-agent", "channel": {"kind": "slack", "address": "C000000S01"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    agent_id: str = resp.json()["id"]
    return agent_id


def test_state_router_accepts_a_scoped_token_for_the_path_agent(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # A scoped "state" token whose agent claim matches the path agent is a
    # first-class credential on the state router: no regression for the
    # platform key, and the sandboxed agent reaches its own namespace with a
    # least-privilege token instead of the raw shared key (#410).
    aid = _agent(client, auth_headers)
    api_key = get_settings().api_key
    token = mint(api_key, agent=aid, scope="state", exp=_FAR_FUTURE)
    headers = {"X-API-Key": token}
    url = f"/agents/{aid}/state/scoped/k"

    put = client.put(url, json={"value": {"n": 1}}, headers=headers)
    assert put.status_code == 200, put.text
    assert put.json()["value"] == {"n": 1}

    got = client.get(url, headers=headers)
    assert got.status_code == 200
    assert got.json()["value"] == {"n": 1}

    # The platform key still works on the same endpoint (no regression).
    assert client.get(url, headers=auth_headers).status_code == 200


def test_state_router_rejects_scoped_token_for_a_different_agent(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    other = str(uuid.uuid4())
    token = mint(get_settings().api_key, agent=other, scope="state", exp=_FAR_FUTURE)
    r = client.put(
        f"/agents/{aid}/state/scoped/k",
        json={"value": {"n": 1}},
        headers={"X-API-Key": token},
    )
    assert r.status_code == 401, r.text


def test_state_router_rejects_expired_scoped_token(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    token = mint(get_settings().api_key, agent=aid, scope="state", exp=_PAST)
    r = client.put(
        f"/agents/{aid}/state/scoped/k",
        json={"value": {"n": 1}},
        headers={"X-API-Key": token},
    )
    assert r.status_code == 401, r.text


def test_state_router_rejects_wrong_scope_token(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    token = mint(get_settings().api_key, agent=aid, scope="admin", exp=_FAR_FUTURE)
    r = client.put(
        f"/agents/{aid}/state/scoped/k",
        json={"value": {"n": 1}},
        headers={"X-API-Key": token},
    )
    assert r.status_code == 401, r.text


def test_state_router_rejects_wrong_signing_key_token(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    # Correct agent + scope, but signed with a key that is not the platform key.
    token = mint("not-the-platform-key", agent=aid, scope="state", exp=_FAR_FUTURE)
    r = client.put(
        f"/agents/{aid}/state/scoped/k",
        json={"value": {"n": 1}},
        headers={"X-API-Key": token},
    )
    assert r.status_code == 401, r.text


def test_state_router_rejects_garbage_token_without_echoing_it(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    garbage = "sbx.xxx.yyy"
    r = client.put(
        f"/agents/{aid}/state/scoped/k",
        json={"value": {"n": 1}},
        headers={"X-API-Key": garbage},
    )
    assert r.status_code == 401, r.text
    # A rejected credential is never echoed back in the error body.
    assert garbage not in r.text


def test_state_router_rejects_missing_api_key_header(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    r = client.put(f"/agents/{aid}/state/scoped/k", json={"value": {"n": 1}})
    assert r.status_code == 401, r.text


def test_app_scoped_token_is_refused_on_reserved_namespaces(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # #249 security backstop: the bundle-facing state token is the NARROW
    # ``state.app`` scope, and the server refuses it on the memory/transcript
    # namespaces owned by the memory (#264) and history (#20) ports -- so a skill
    # cannot corrupt them by composing CURIE_STATE_URL directly, bypassing the
    # runner tool's own client-side refusal.
    aid = _agent(client, auth_headers)
    app = mint(get_settings().api_key, agent=aid, scope="state.app", exp=_FAR_FUTURE)
    headers = {"X-API-Key": app}

    for ns in ("memory", "transcript"):
        put = client.put(
            f"/agents/{aid}/state/{ns}/k", json={"value": {"n": 1}}, headers=headers
        )
        assert put.status_code == 403, f"{ns}: {put.text}"
        assert "reserved" in put.text
        # Every verb over a reserved namespace is refused, not just writes.
        assert client.get(f"/agents/{aid}/state/{ns}/k", headers=headers).status_code == 403
        assert client.get(f"/agents/{aid}/state/{ns}", headers=headers).status_code == 403
        assert (
            client.post(
                f"/agents/{aid}/state/{ns}/log/append", json={"item": 1}, headers=headers
            ).status_code
            == 403
        )
        assert client.delete(f"/agents/{aid}/state/{ns}/k", headers=headers).status_code == 403


def test_namespace_enumeration_hides_reserved_from_the_app_token(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # #856: the enumeration route (GET .../state) has no namespace path param, so
    # forbid_reserved_namespace cannot gate it. The narrow state.app token must
    # still not learn the reserved namespaces exist -- their key counts and write
    # times are exactly what that scope fences off. The platform key (the UI
    # inspector) keeps full reach.
    aid = _agent(client, auth_headers)
    # Seed both reserved namespaces and a normal one via the unrestricted key.
    for ns, key in (("memory", "m1"), ("transcript", "t1"), ("workflow", "w1")):
        put = client.put(
            f"/agents/{aid}/state/{ns}/{key}", json={"value": {"n": 1}}, headers=auth_headers
        )
        assert put.status_code == 200, f"{ns}: {put.text}"

    app = mint(get_settings().api_key, agent=aid, scope="state.app", exp=_FAR_FUTURE)
    app_names = {
        row["namespace"]
        for row in client.get(f"/agents/{aid}/state", headers={"X-API-Key": app}).json()
    }
    assert app_names == {"workflow"}, app_names

    platform_names = {
        row["namespace"]
        for row in client.get(f"/agents/{aid}/state", headers=auth_headers).json()
    }
    assert platform_names == {"memory", "transcript", "workflow"}, platform_names


def test_app_scoped_token_works_on_a_non_reserved_namespace(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # The narrow token is refused ONLY on the reserved set; everywhere else it is
    # a first-class credential -- the bundle "gets the rest".
    aid = _agent(client, auth_headers)
    app = mint(get_settings().api_key, agent=aid, scope="state.app", exp=_FAR_FUTURE)
    headers = {"X-API-Key": app}
    url = f"/agents/{aid}/state/workflow/step"

    assert client.put(url, json={"value": {"n": 1}}, headers=headers).status_code == 200
    assert client.get(url, headers=headers).json()["value"] == {"n": 1}


def test_broad_state_token_and_platform_key_reach_reserved_namespaces(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # The loaders MUST reach memory/transcript to rehydrate: the broad ``state``
    # token (their credential) and the platform key are both unrestricted. If this
    # regressed, memory/history rehydration would break -- the reason the fix
    # gates on scope, not on the namespace alone.
    aid = _agent(client, auth_headers)
    broad = mint(get_settings().api_key, agent=aid, scope="state", exp=_FAR_FUTURE)

    for headers in ({"X-API-Key": broad}, auth_headers):
        for ns in ("memory", "transcript"):
            r = client.put(
                f"/agents/{aid}/state/{ns}/k", json={"value": {"n": 1}}, headers=headers
            )
            assert r.status_code == 200, f"{ns}: {r.text}"


def test_put_get_list_delete_round_trip(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    base = f"/agents/{aid}/state/approvals"

    # put (create) -> version 1
    r = client.put(
        f"{base}/thread-1", json={"value": {"status": "pending"}}, headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["value"] == {"status": "pending"}
    assert r.json()["version"] == 1

    # get returns what was written
    got = client.get(f"{base}/thread-1", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["value"] == {"status": "pending"}

    # put (update) -> version bumps to 2
    r2 = client.put(
        f"{base}/thread-1", json={"value": {"status": "approved"}}, headers=auth_headers
    )
    assert r2.json()["version"] == 2

    # list by namespace returns both keys
    client.put(
        f"{base}/thread-2", json={"value": {"status": "pending"}}, headers=auth_headers
    )
    listed = client.get(base, headers=auth_headers).json()
    assert {e["key"] for e in listed} == {"thread-1", "thread-2"}

    # delete -> 204, then gone
    d = client.delete(f"{base}/thread-1", headers=auth_headers)
    assert d.status_code == 204
    assert client.get(f"{base}/thread-1", headers=auth_headers).status_code == 404


def test_compare_and_set_rejects_a_stale_version(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    url = f"/agents/{aid}/state/dedupe/seen"

    v1 = client.put(url, json={"value": {"n": 1}}, headers=auth_headers).json()
    assert v1["version"] == 1

    # CAS with the current version succeeds and bumps to 2.
    ok = client.put(
        url, json={"value": {"n": 2}, "expected_version": 1}, headers=auth_headers
    )
    assert ok.status_code == 200
    assert ok.json()["version"] == 2

    # CAS with the now-stale version 1 is rejected, and the value is unchanged.
    stale = client.put(
        url, json={"value": {"n": 3}, "expected_version": 1}, headers=auth_headers
    )
    assert stale.status_code == 409, stale.text
    assert client.get(url, headers=auth_headers).json()["value"] == {"n": 2}


def test_put_unknown_agent_is_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.put(
        f"/agents/{missing}/state/ns/k", json={"value": {}}, headers=auth_headers
    )
    assert r.status_code == 404


def test_get_missing_entry_is_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    r = client.get(f"/agents/{aid}/state/ns/nope", headers=auth_headers)
    assert r.status_code == 404


def test_concurrent_cas_one_writer_wins_loser_sees_compare_failure(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # The AC's core scenario: two writers both read the same version, both try a
    # compare-and-set write; exactly one wins and the loser gets the 409.
    aid = _agent(client, auth_headers)
    url = f"/agents/{aid}/state/counter/n"

    seed = client.put(url, json={"value": {"n": 0}}, headers=auth_headers).json()
    read_version = seed["version"]  # both writers observe this version

    winner = client.put(
        url,
        json={"value": {"n": 1}, "expected_version": read_version},
        headers=auth_headers,
    )
    loser = client.put(
        url,
        json={"value": {"n": 2}, "expected_version": read_version},
        headers=auth_headers,
    )

    assert winner.status_code == 200, winner.text
    assert loser.status_code == 409, loser.text
    # The winner's write stands; the loser's did not clobber it.
    assert client.get(url, headers=auth_headers).json()["value"] == {"n": 1}


def test_append_grows_a_log_shaped_entry(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    url = f"/agents/{aid}/state/audit/log/append"

    # First append creates the entry as a single-element array (version 1).
    r1 = client.post(url, json={"item": {"event": "created"}}, headers=auth_headers)
    assert r1.status_code == 200, r1.text
    assert r1.json()["value"] == [{"event": "created"}]
    assert r1.json()["version"] == 1

    # Second append extends the array and bumps the version.
    r2 = client.post(url, json={"item": {"event": "approved"}}, headers=auth_headers)
    assert r2.json()["value"] == [{"event": "created"}, {"event": "approved"}]
    assert r2.json()["version"] == 2


def test_append_onto_a_non_array_value_is_409(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    base = f"/agents/{aid}/state/audit"
    client.put(f"{base}/obj", json={"value": {"not": "a list"}}, headers=auth_headers)

    r = client.post(f"{base}/obj/append", json={"item": 1}, headers=auth_headers)
    assert r.status_code == 409, r.text


def test_value_over_the_per_value_cap_is_rejected(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    from curie_api.config import get_settings

    aid = _agent(client, auth_headers)
    # Shrink the per-value cap for this test. get_settings() is lru_cached, so it
    # returns a singleton we mutate and reset via cache_clear() in the finally.
    get_settings().state_max_value_bytes = 50
    try:
        oversized = {"blob": "x" * 200}
        r = client.put(
            f"/agents/{aid}/state/big/v", json={"value": oversized}, headers=auth_headers
        )
        assert r.status_code == 413, r.text
        # A small value under the cap still writes.
        ok = client.put(
            f"/agents/{aid}/state/big/v", json={"value": {"n": 1}}, headers=auth_headers
        )
        assert ok.status_code == 200
    finally:
        get_settings.cache_clear()


def test_namespace_over_the_per_namespace_cap_is_rejected(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    from curie_api.config import get_settings

    aid = _agent(client, auth_headers)
    base = f"/agents/{aid}/state/capped"
    get_settings().state_max_namespace_bytes = 100
    try:
        # First key fits (~58 bytes serialized).
        a = client.put(f"{base}/a", json={"value": {"s": "x" * 50}}, headers=auth_headers)
        assert a.status_code == 200, a.text
        # Second key pushes the namespace total (~116 bytes) over the 100 cap.
        b = client.put(f"{base}/b", json={"value": {"s": "x" * 50}}, headers=auth_headers)
        assert b.status_code == 413, b.text
    finally:
        get_settings.cache_clear()


def test_namespace_count_over_the_per_agent_cap_is_rejected(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # #852: a per-agent cap on the NUMBER of namespaces refuses a NEW namespace
    # once the agent is at its limit, so a sandbox cannot loop creating unbounded
    # namespaces (each under the byte caps). Writes to an existing namespace are
    # unaffected. Same lru_cache mutate-and-reset pattern as the byte-cap tests.
    aid = _agent(client, auth_headers)

    def put(ns: str, key: str = "k") -> Any:
        return client.put(
            f"/agents/{aid}/state/{ns}/{key}", json={"value": 1}, headers=auth_headers
        )

    get_settings().state_max_namespaces = 2
    try:
        assert put("ns1").status_code == 200
        assert put("ns2").status_code == 200
        # A third, NEW namespace is refused with a clear 4xx, not a 500.
        over = put("ns3")
        assert over.status_code == 403, over.text
        assert "cap" in over.text.lower() and "namespace" in over.text.lower()
        # More keys in an EXISTING namespace still succeed (not a new namespace).
        assert put("ns1", "k2").status_code == 200
    finally:
        get_settings.cache_clear()


def _asyncpg_dsn() -> str:
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


async def _install_state_insert_gate() -> None:
    """Hold every INSERT into the state store at an advisory lock (#933).

    The state variant of test_memory.py's BEFORE UPDATE gate: the write that can
    create a new namespace is an INSERT, not an UPDATE. Lock id 933933 is
    distinct from test_memory's 391391 so the two gates can never alias, and both
    live in the ONE-argument advisory space, which Postgres keeps entirely
    separate from the two-int4 space the production namespace lock uses.
    """
    connection = await asyncpg.connect(_asyncpg_dsn())
    try:
        await connection.execute(
            """
            CREATE OR REPLACE FUNCTION curie.test_state_insert_gate()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                PERFORM pg_advisory_xact_lock(933933);
                RETURN NEW;
            END;
            $$
            """
        )
        await connection.execute(
            """
            CREATE TRIGGER test_state_insert_gate
            BEFORE INSERT ON curie.workflow_state_entries
            FOR EACH ROW
            EXECUTE FUNCTION curie.test_state_insert_gate()
            """
        )
    finally:
        await connection.close()


async def _remove_state_insert_gate() -> None:
    connection = await asyncpg.connect(_asyncpg_dsn())
    try:
        await connection.execute(
            """
            DROP TRIGGER IF EXISTS test_state_insert_gate
            ON curie.workflow_state_entries
            """
        )
        await connection.execute("DROP FUNCTION IF EXISTS curie.test_state_insert_gate()")
    finally:
        await connection.close()


async def _wait_for_blocked_state_requests(minimum: int, requests: list[Future[Any]]) -> None:
    """Block until `minimum` sessions are waiting on a lock, or fail loudly.

    Deliberately NOT filtered by `query LIKE '%workflow_state_entries%'` the way
    test_memory's twin is: once the cap is atomic, the second writer waits on
    `SELECT pg_advisory_xact_lock($1, $2)` inside the cap check and never reaches
    an INSERT, so its query text names no table and that filter would never see
    it. Leaving the predicate on `wait_event_type` alone is safe because the
    disposable-DB conftest gives the run its own database (so `current_database()`
    already scopes the count to this test's traffic) and the gate holder *holds*
    its advisory lock rather than waiting on one.
    """
    connection = await asyncpg.connect(_asyncpg_dsn())
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            count = await connection.fetchval(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND wait_event_type = 'Lock'
                """
            )
            if int(count) >= minimum:
                return
            # A request that finished instead of blocking means the interleaving
            # under test never happened -- fail loudly rather than hang to the
            # deadline and report a timeout.
            completed = [request.result() for request in requests if request.done()]
            assert not completed, f"request completed before blocking: {completed}"
            await asyncio.sleep(0.01)
        raise AssertionError(f"only some of {minimum} state requests blocked")
    finally:
        await connection.close()


def _hold_advisory_lock(
    args: tuple[int, ...],
    acquired: threading.Event,
    release: threading.Event,
    errors: list[Exception],
) -> None:
    """Hold a Postgres session-level advisory lock from an outside session (#933).

    Shared by both #933 gates: the one-argument INSERT gate (`(933933,)`) and
    the two-argument PRODUCTION namespace lock (`_NAMESPACE_LOCK_CLASS`,
    `_namespace_lock_key(...)`). Postgres keeps the one-argument and
    two-argument advisory-lock spaces entirely separate, so 933933 as a
    single arg can never collide with the two-arg production key.

    Session-level (`pg_advisory_lock`), not transaction-level, because it has
    to outlive the statement that takes it and be released on a signal from
    the test.
    """
    placeholders = ", ".join(f"${i}" for i in range(1, len(args) + 1))

    async def hold() -> None:
        connection = await asyncpg.connect(_asyncpg_dsn())
        try:
            await connection.execute(f"SELECT pg_advisory_lock({placeholders})", *args)
            acquired.set()
            release.wait()
            await connection.execute(f"SELECT pg_advisory_unlock({placeholders})", *args)
        finally:
            # Closing the connection releases the session lock too, so no
            # failure path can leave the key held and wedge every later test.
            await connection.close()

    try:
        asyncio.run(hold())
    except Exception as error:
        errors.append(error)
        acquired.set()


def _run_ordered_state_requests(
    first: Callable[[], Any], second: Callable[[], Any]
) -> tuple[Any, Any]:
    """Run two real requests concurrently, both parked at the INSERT gate.

    Ordering is established by the database, not by a timer: `first` is submitted
    and confirmed blocked before `second` is submitted, and both are confirmed
    blocked before the gate is released. "Confirmed blocked" means observed --
    polling `pg_stat_activity` until the expected number of waiters is seen --
    not timed; the poll loop's sleep is pacing between observations, not a
    handoff window, so the interleaving is identical on a loaded box regardless
    of that sleep's duration.
    """
    asyncio.run(_install_state_insert_gate())
    acquired = threading.Event()
    release = threading.Event()
    lock_errors: list[Exception] = []
    lock_thread = threading.Thread(
        target=_hold_advisory_lock,
        args=((933933,), acquired, release, lock_errors),
        daemon=True,
    )
    lock_thread.start()
    try:
        assert acquired.wait(timeout=10), "database gate was not acquired"
        assert not lock_errors, lock_errors
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_request = executor.submit(first)
            try:
                asyncio.run(_wait_for_blocked_state_requests(1, [first_request]))
                second_request = executor.submit(second)
                asyncio.run(_wait_for_blocked_state_requests(2, [first_request, second_request]))
            finally:
                release.set()
            first_response = first_request.result(timeout=10)
            second_response = second_request.result(timeout=10)
    finally:
        # An INSERT gate left installed would block essentially every later test
        # in the session, so it comes down even if the orchestration failed.
        release.set()
        lock_thread.join(timeout=10)
        asyncio.run(_remove_state_insert_gate())

    assert not lock_thread.is_alive(), "database gate did not release"
    assert not lock_errors, lock_errors
    return first_response, second_response


def _state_writer(
    client: Any, headers: dict[str, str], aid: str, namespace: str, key: str
) -> Callable[[], Any]:
    def request() -> Any:
        return client.put(
            f"/agents/{aid}/state/{namespace}/{key}", json={"value": 1}, headers=headers
        )

    return request


def test_existing_namespace_write_does_not_take_the_namespace_lock(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """#933 AC4: an existing-namespace write never touches the advisory lock.

    The mutation this catches is deleting the hot-path early `return` in
    `_enforce_caps` (routers/state.py) -- i.e. making the advisory lock
    unconditional. Every other test in this file still passes under that
    mutation, because they all create ABSENT namespaces and so take the lock
    either way. dadf93e2's stated contract is that "writes to an existing
    namespace are unaffected": no lock, no added latency. Without this test that
    sentence is pinned by nothing.

    Both directions are proved under ONE held lock, which is what makes a pass
    meaningful rather than vacuous:

    * the brand-NEW namespace write is confirmed blocked on the key (via
      `pg_stat_activity`, not a sleep), so the key really is the production one
      and the lock really is being contended; then
    * the EXISTING-namespace write must return 200 inside a bounded timeout.

    Failure is a `TimeoutError` from `.result()`, never a hang: the holder is
    released in a `finally` on every path.
    """
    aid = _agent(client, auth_headers)
    # Seed the namespace so the second write into it is an EXISTING-namespace
    # write. Seeded before the lock is taken; seeding afterwards would itself
    # block on the very key under test.
    seed = client.put(f"/agents/{aid}/state/existing/k1", json={"value": 1}, headers=auth_headers)
    assert seed.status_code == 200, seed.text

    acquired = threading.Event()
    release = threading.Event()
    lock_errors: list[Exception] = []
    lock_thread = threading.Thread(
        target=_hold_advisory_lock,
        args=(
            (_NAMESPACE_LOCK_CLASS, _namespace_lock_key(uuid.UUID(aid))),
            acquired,
            release,
            lock_errors,
        ),
        daemon=True,
    )
    lock_thread.start()
    try:
        assert acquired.wait(timeout=10), "production namespace lock was not acquired"
        assert not lock_errors, lock_errors
        with ThreadPoolExecutor(max_workers=2) as executor:
            # Control arm: a NEW namespace must serialize on the held key.
            blocked = executor.submit(_state_writer(client, auth_headers, aid, "brand-new", "k1"))
            try:
                asyncio.run(_wait_for_blocked_state_requests(1, [blocked]))
                # The arm under test. Post-fix it returns before the lock is ever
                # requested; with the hot-path `return` deleted it joins the
                # queue behind the holder and this `.result` raises TimeoutError.
                unblocked = executor.submit(
                    _state_writer(client, auth_headers, aid, "existing", "k2")
                )
                existing_response = unblocked.result(timeout=5)
            finally:
                # Released INSIDE the executor context: exiting the `with` joins
                # its workers with no timeout, so a still-blocked request would
                # hang there instead of failing.
                release.set()
            blocked_response = blocked.result(timeout=10)
    finally:
        release.set()
        lock_thread.join(timeout=10)

    assert not lock_thread.is_alive(), "namespace lock holder did not release"
    assert not lock_errors, lock_errors
    assert existing_response.status_code == 200, existing_response.text
    # The control arm proves the block was real contention, not a dead key: it
    # only completes once the holder lets go.
    assert blocked_response.status_code == 200, blocked_response.text

    listing = client.get(f"/agents/{aid}/state", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    by_ns = {row["namespace"]: row for row in listing.json()}
    assert by_ns["existing"]["key_count"] == 2, listing.text
    assert by_ns["brand-new"]["key_count"] == 1, listing.text


def test_concurrent_new_namespaces_cannot_exceed_the_cap(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """#933: a concurrent burst must not walk past the per-agent namespace cap.

    Pre-fix this is RED. The cap is a check-then-write TOCTOU: writer 2's
    unlocked `count(distinct namespace)` sees only the seed, because writer 1's
    `race-a` row is still uncommitted behind the gate, so it reads 1 < 2 and
    passes its check too. Both insert -- two 200s, zero 403s, three namespaces
    for a cap of 2.
    """
    aid = _agent(client, auth_headers)
    # Seed BEFORE the gate is installed; a seed written afterwards would itself
    # block on the gate.
    seed = client.put(f"/agents/{aid}/state/seed/k", json={"value": 1}, headers=auth_headers)
    assert seed.status_code == 200, seed.text

    get_settings().state_max_namespaces = 2
    try:
        # The distinct-namespace count reaches 2 either way, so the same wait
        # predicate holds pre-fix and post-fix. Post-fix writer 1 blocks at the
        # gate while holding the namespace advisory lock and writer 2 blocks on
        # that lock inside the cap check; pre-fix writer 2 sails through its
        # check and blocks at the gate on its own INSERT. Two waiters either way.
        first, second = _run_ordered_state_requests(
            _state_writer(client, auth_headers, aid, "race-a", "k"),
            _state_writer(client, auth_headers, aid, "race-b", "k"),
        )

        # Which writer wins is not under test, so assert the multiset.
        statuses = sorted([first.status_code, second.status_code])
        assert statuses == [200, 403], (first.text, second.text)
        refused = first if first.status_code == 403 else second
        assert "cap" in refused.text.lower() and "namespace" in refused.text.lower()

        listing = client.get(f"/agents/{aid}/state", headers=auth_headers)
        assert listing.status_code == 200, listing.text
        assert len(listing.json()) == 2, listing.text
    finally:
        get_settings.cache_clear()


def test_concurrent_writes_to_the_same_new_namespace_both_succeed(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """The false-positive guard for #933's own fix, not regression coverage.

    Stated honestly: this test also passes PRE-fix, because today both writers
    sail through the unlocked check and insert. Its value is in the other
    direction -- it goes RED against a fix that takes the namespace lock WITHOUT
    re-checking existence under it. Such a fix would make writer 2 run the count,
    see 2 (`seed` + `shared-new`) >= 2, and 403 a write that must be allowed. The
    cap and seed numbers are chosen so it discriminates; do not raise the cap.

    The two writers use DIFFERENT keys so the `uq_state_agent_ns_key` unique
    constraint is not involved: this is about the cap, not about insert
    conflicts.
    """
    aid = _agent(client, auth_headers)
    seed = client.put(f"/agents/{aid}/state/seed/k", json={"value": 1}, headers=auth_headers)
    assert seed.status_code == 200, seed.text

    get_settings().state_max_namespaces = 2
    try:
        first, second = _run_ordered_state_requests(
            _state_writer(client, auth_headers, aid, "shared-new", "k1"),
            _state_writer(client, auth_headers, aid, "shared-new", "k2"),
        )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

        listing = client.get(f"/agents/{aid}/state", headers=auth_headers)
        assert listing.status_code == 200, listing.text
        body = listing.json()
        assert len(body) == 2, body
        by_ns = {row["namespace"]: row for row in body}
        assert by_ns["shared-new"]["key_count"] == 2, body
    finally:
        get_settings.cache_clear()


def test_list_namespaces_summarizes_the_store_recent_first(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # The operator read/inspect surface (#250) enumerates an agent's namespaces
    # with key counts and the last write time, most-recently-written first.
    aid = _agent(client, auth_headers)
    assert client.get(f"/agents/{aid}/state", headers=auth_headers).json() == []

    client.put(f"/agents/{aid}/state/alpha/a", json={"value": 1}, headers=auth_headers)
    client.put(f"/agents/{aid}/state/alpha/b", json={"value": 2}, headers=auth_headers)
    # beta is written after alpha, so it sorts first (most recent).
    client.put(f"/agents/{aid}/state/beta/c", json={"value": 3}, headers=auth_headers)

    resp = client.get(f"/agents/{aid}/state", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    namespaces = [row["namespace"] for row in body]
    assert namespaces == ["beta", "alpha"]
    by_ns = {row["namespace"]: row for row in body}
    assert by_ns["alpha"]["key_count"] == 2
    assert by_ns["beta"]["key_count"] == 1
    assert by_ns["alpha"]["last_updated"] and by_ns["beta"]["last_updated"]


def test_binding_scoped_state_is_isolated_from_the_shared_and_sibling_scopes(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """#1525 follow-up: the `/state/bindings/{kind}/{address}/...` path names an
    independent partition of the same agent's general state, distinct from the
    plain (shared) path and from any other binding's own partition. This is
    what makes a memory=False agent's bindings behave as isolated instances
    that happen to share one bundle/budget/kill-state, and a memory=True
    agent's worth of state (the plain path) as one instance reachable from
    several doors -- the worker decides which URL to hand a runner (never
    exercised here, an API-level test), but the API must honor both shapes
    correctly regardless of which one is asked for.
    """
    aid = _agent(client, auth_headers)
    second = client.post(
        f"/agents/{aid}/channels",
        json={"kind": "slack", "address": "C000000S02"},
        headers=auth_headers,
    )
    assert second.status_code == 201, second.text

    shared_url = f"/agents/{aid}/state/ns/k"
    first_url = f"/agents/{aid}/state/bindings/slack/C000000S01/ns/k"
    second_url = f"/agents/{aid}/state/bindings/slack/C000000S02/ns/k"

    assert client.put(shared_url, json={"value": "shared"}, headers=auth_headers).status_code == 200
    assert client.put(first_url, json={"value": "first"}, headers=auth_headers).status_code == 200
    assert client.put(second_url, json={"value": "second"}, headers=auth_headers).status_code == 200

    assert client.get(shared_url, headers=auth_headers).json()["value"] == "shared"
    assert client.get(first_url, headers=auth_headers).json()["value"] == "first"
    assert client.get(second_url, headers=auth_headers).json()["value"] == "second"

    # Each scope's namespace listing sees only its own row.
    listed = client.get(f"/agents/{aid}/state/bindings/slack/C000000S01/ns", headers=auth_headers)
    assert [e["value"] for e in listed.json()] == ["first"]


def test_binding_scoped_route_404s_for_a_pair_that_is_not_this_agents(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    resp = client.get(
        f"/agents/{aid}/state/bindings/slack/C000000S03/ns/k", headers=auth_headers
    )
    assert resp.status_code == 404
    assert "binding" in resp.text.lower()


def test_binding_scoped_route_accepts_the_same_scoped_token_shape_as_the_plain_route(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # The binding path adds no new credential shape (#1525 follow-up): the
    # SAME app-scoped sandbox token that already works on the plain path
    # authenticates here too, since the token still only ever names an agent,
    # never a binding -- the rejected #1525 alternative was widening it to.
    aid = _agent(client, auth_headers)
    api_key = get_settings().api_key
    token = mint(api_key, agent=aid, scope="state.app", exp=_FAR_FUTURE)
    headers = {"X-API-Key": token}
    url = f"/agents/{aid}/state/bindings/slack/C000000S01/ns/k"

    put = client.put(url, json={"value": {"n": 1}}, headers=headers)
    assert put.status_code == 200, put.text
    assert client.get(url, headers=headers).json()["value"] == {"n": 1}
