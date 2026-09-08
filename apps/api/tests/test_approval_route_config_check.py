"""The declared/bound approval-route join, refused at CONFIGURATION time (#2436).

Two planes nothing cross-checked before this: the routes a bundle DECLARES in
its manifest (`approvalPolicy.gates[].route`, versioned with the agent) and the
routes an operator BINDS (`agents.approval_routes`, per agent, mutable). A
bundle could declare a route no binding named, deploy green, and only discover
it at request time -- by escalating instead of routing, which strands the human
the gate exists to reach. ADR-0050's rationale already claims that residual is
bounded because "the bundle can only name a route the operator has itself
bound"; nothing realized the claim until this gate.

Every assertion here goes through the real consumer path -- an HTTP status code
and JSON body from the running app -- and never an internal struct field
(AGENTS.md, "Guards are outcome-tested"). The suite runs against the real
compose Postgres and RustFS the disposable-DB conftest provisions; nothing is
mocked. The two exceptions are the frozen-vector test and the blank-route poison
test, which call `bundles.declared_approval_routes` directly: they pin the
normalization the API side of a cross-process parity seam must implement, and
its refusal half is deliberately unreachable through HTTP because
`plugin_format.validate_bundle` already refuses such bytes at every storage
entry point (asserted, not assumed, in the blank-route test).

The manifest fixture shapes (`_tar_gz`, the valid-files map, the skill
frontmatter) follow `test_bundles.py`; the route-binding literal follows
`test_agents.py::_slack`. Helpers are local on purpose: the suite runs under
`--import-mode=importlib` with no `__init__.py`, so one test module cannot
import another.
"""

import io
import json
import tarfile
from pathlib import Path
from typing import Any

from curie_api import bundles

BUNDLE_NAME = "route-check-plugin"
VECTOR = Path(__file__).resolve().parents[3] / "tests/vectors/approval-route-normalization.json"


def _skill(name: str) -> str:
    return f"---\nname: {name}\ndescription: does {name} things\n---\n\n# {name}\n"


def _manifest(gates: list[dict[str, Any]] | None) -> str:
    """The plugin manifest, with an `approvalPolicy` only when `gates` is given.

    `gates is None` is the manifest that declares no `approvalPolicy` at all,
    which is a DIFFERENT case from `gates == []` (a policy declaring nothing).
    Both must read back as an empty declared set (AC4), and both have a test.
    """

    manifest: dict[str, Any] = {"name": BUNDLE_NAME, "version": "0.1.0"}
    if gates is not None:
        manifest["approvalPolicy"] = {"gates": gates}
    return json.dumps(manifest)


def _gate(route: str, tool: str = "Bash") -> dict[str, Any]:
    """One gate declaration naming `route`.

    A BUILT-IN tool name by default, so `validate_bundle`'s `mcp__` namespacing
    rules do not apply and the declared ROUTE is the only variable -- the same
    isolation `runner/tests/test_approval.py::_write_bundle` uses.
    """

    return {"gate": tool, "route": route}


def _files(gates: list[dict[str, Any]] | None = None) -> dict[str, str]:
    return {
        ".claude-plugin/plugin.json": _manifest(gates),
        "skills/alpha/SKILL.md": _skill("alpha"),
    }


def _tar_gz(files: dict[str, str], top: str = BUNDLE_NAME) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _archive(gates: list[dict[str, Any]] | None = None) -> bytes:
    return _tar_gz(_files(gates))


def _slack(address: str) -> dict[str, str]:
    """The Slack-kind binding literal, so a shape change lands in one place."""

    return {"kind": "slack", "address": address}


def _binding(address: str = "C0EXAMPLE1") -> dict[str, Any]:
    return {"resolution": _slack(address)}


def _routes(*names: str, address: str = "C0EXAMPLE1") -> dict[str, Any]:
    return {name: _binding(address) for name in names}


def _create_agent(
    client: Any,
    headers: dict[str, str],
    *,
    name: str = "route-agent",
    channel: str = "C000000A01",
    routes: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {"name": name, "channel": _slack(channel)}
    if routes is not None:
        payload["approval_routes"] = routes
    resp = client.post("/agents", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _create_version(client: Any, headers: dict[str, str], agent_id: str, label: str = "v1") -> str:
    resp = client.post(
        f"/agents/{agent_id}/versions",
        json={"version_label": label, "created_by": "bconn"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _upload(
    client: Any, headers: dict[str, str], agent_id: str, version_id: str, archive: bytes
) -> Any:
    return client.put(
        f"/agents/{agent_id}/versions/{version_id}/bundle",
        files={"file": ("bundle.tar.gz", archive)},
        headers=headers,
    )


def _bundled_version(
    client: Any,
    headers: dict[str, str],
    agent_id: str,
    gates: list[dict[str, Any]] | None = None,
    label: str = "v1",
) -> str:
    """A version carrying a stored bundle that declares `gates`."""

    version_id = _create_version(client, headers, agent_id, label)
    upload = _upload(client, headers, agent_id, version_id, _archive(gates))
    assert upload.status_code == 201, upload.text
    return version_id


def _deploy(
    client: Any,
    headers: dict[str, str],
    agent_id: str,
    version_id: str,
    environment: str = "dev",
) -> Any:
    return client.post(
        "/deployments",
        json={"agent_id": agent_id, "version_id": version_id, "environment": environment},
        headers=headers,
    )


def _deployments(client: Any, headers: dict[str, str], agent_id: str) -> list[dict[str, Any]]:
    resp = client.get("/deployments", params={"agent_id": agent_id}, headers=headers)
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def _versions(client: Any, headers: dict[str, str], agent_id: str) -> list[dict[str, Any]]:
    resp = client.get(f"/agents/{agent_id}/versions", headers=headers)
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def _agent(client: Any, headers: dict[str, str], agent_id: str) -> dict[str, Any]:
    resp = client.get(f"/agents/{agent_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _detail(response: Any) -> str:
    return str(response.json()["detail"])


def _write_bundle_root(root: Path, gates: list[dict[str, Any]] | None) -> Path:
    """A bundle directory on disk carrying `gates`, for the reader-level tests."""

    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(_manifest(gates), encoding="utf-8")
    return root


# --- AC1: a declared route with no binding is refused at configuration ---------


def test_deploy_is_refused_when_the_bundle_declares_an_unbound_route(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1, the fix pin: `POST /deployments` refuses a bundle declaring `ops`
    when the agent's `approval_routes` binds only `finance` (plan test table,
    API row 1)."""

    agent_id = _create_agent(client, auth_headers, routes=_routes("finance"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])

    resp = _deploy(client, auth_headers, agent_id, version_id)

    assert resp.status_code == 422, resp.text
    assert "'ops'" in _detail(resp), resp.text
    assert _deployments(client, auth_headers, agent_id) == [], "a refusal must leave no row"


def test_the_refusal_names_the_bound_routes_alongside_the_unbound_one(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1: the 422 detail names the BOUND routes as well as the unbound one, so
    the operator can see the typo without a second request (plan test table, API
    row 2)."""

    agent_id = _create_agent(client, auth_headers, routes=_routes("finance", "legal"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])

    resp = _deploy(client, auth_headers, agent_id, version_id)

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert "'ops'" in detail, detail
    assert "'finance'" in detail and "'legal'" in detail, detail


def test_a_route_declared_and_bound_deploys(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1's falsifiable pair: the same bundle with `ops` bound deploys (plan
    test table, API row 3). Without it a gate that refused every gated bundle
    would satisfy every negative above."""

    agent_id = _create_agent(client, auth_headers, routes=_routes("ops"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])

    resp = _deploy(client, auth_headers, agent_id, version_id)

    assert resp.status_code == 201, resp.text
    deployments = _deployments(client, auth_headers, agent_id)
    assert [(d["version_id"], d["status"]) for d in deployments] == [(version_id, "active")]


# --- F1: the late-attachment hole, and the pre-deployment upload it must spare -


def test_uploading_a_gated_bundle_onto_an_already_deployed_version_is_refused(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1 / F1: attaching a gated bundle to an already-active version is refused.

    Plan test table (API), the late-attachment row. `DeploymentCreate` carries no
    bundle and `revalidate_stored_bundle` no-ops on a null `bundle_ref`, so
    `POST /deployments` succeeds for a bundleless version -- and the worker's
    resolve query joins `deployments.status = 'active'` to
    `agent_versions.bundle_ref`, so a later upload puts the bundle live with no
    deployment gate ever running. The same join therefore has to fire on the
    upload, arriving from the other side. The null `bundle_ref` afterwards is the
    load-bearing half: `crud.attach_bundle` commits immediately, so a refusal
    that ran after it would already be live.
    """

    agent_id = _create_agent(client, auth_headers)
    version_id = _create_version(client, auth_headers, agent_id)
    assert _deploy(client, auth_headers, agent_id, version_id).status_code == 201

    resp = _upload(client, auth_headers, agent_id, version_id, _archive([_gate("ops")]))

    assert resp.status_code == 422, resp.text
    assert "'ops'" in _detail(resp), resp.text
    assert [v["bundle_ref"] for v in _versions(client, auth_headers, agent_id)] == [None]


def test_uploading_a_gated_bundle_before_any_deployment_is_unrestricted(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC2 / F1's falsifiable pair: the ordinary pre-deployment upload is untouched.

    Plan test table (API), the unrestricted-upload row. The CLI's `prepare_deploy`
    uploads the bundle BEFORE `curie <tier> approvals` binds the route, so an
    unconditional upload gate would refuse the documented onboarding order for
    every gated agent -- and would refuse a bundle that may never be deployed.
    The gate is conditional on the version already being live, and this is what
    holds it conditional.
    """

    agent_id = _create_agent(client, auth_headers)
    version_id = _create_version(client, auth_headers, agent_id)

    resp = _upload(client, auth_headers, agent_id, version_id, _archive([_gate("ops")]))

    assert resp.status_code == 201, resp.text
    assert [v["bundle_ref"] for v in _versions(client, auth_headers, agent_id)] == [
        resp.json()["bundle_ref"]
    ]


# --- AC2: a bound route no bundle declares is never an error ------------------


def test_a_bound_route_no_bundle_declares_is_accepted(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC2: binding ahead of a bundle bump is a supported workflow, not a warning.

    Plan test table (API), the bound-but-undeclared row. The join is one
    directional: every DECLARED route must be bound, and a bound route no bundle
    names is simply pre-binding.
    """

    agent_id = _create_agent(client, auth_headers, routes=_routes("finance", "ops"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])

    resp = _deploy(client, auth_headers, agent_id, version_id)

    assert resp.status_code == 201, resp.text


def test_agent_create_with_a_route_no_bundle_declares_succeeds(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC2: `POST /agents` with `approval_routes` and no version is legal.

    Plan test table (API), the create row, and behavior site 6's explicit no-op:
    no version and no bundle exists yet, so a create naming a route nothing
    declares is exactly the legal pre-binding AC2 protects.
    """

    resp = client.post(
        "/agents",
        json={
            "name": "prebound-agent",
            "channel": _slack("C000000A02"),
            "approval_routes": _routes("ops"),
        },
        headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    assert set(resp.json()["approval_routes"]) == {"ops"}


# --- AC4: the check does not fire for a bundle that declares no gates ----------


def test_a_bundle_with_no_approval_policy_deploys_with_no_routes_bound(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC4: no `approvalPolicy` in the manifest means an empty declared set.

    Plan test table (API), the no-policy row. The agent's `approval_routes` is
    null, so any reader that turned "cannot find a policy" into a refusal would
    break every ungated agent on the platform at once.
    """

    agent_id = _create_agent(client, auth_headers)
    assert _agent(client, auth_headers, agent_id)["approval_routes"] is None
    version_id = _bundled_version(client, auth_headers, agent_id, None)

    resp = _deploy(client, auth_headers, agent_id, version_id)

    assert resp.status_code == 201, resp.text


def test_a_bundle_with_an_empty_gates_list_deploys_with_no_routes_bound(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC4: `"approvalPolicy": {"gates": []}` also declares nothing.

    Plan test table (API), the empty-gates row: the boundary between "no policy"
    and "a policy declaring nothing". They reach the reader by different
    branches, so one passing does not imply the other.
    """

    agent_id = _create_agent(client, auth_headers)
    assert _agent(client, auth_headers, agent_id)["approval_routes"] is None
    version_id = _bundled_version(client, auth_headers, agent_id, [])

    resp = _deploy(client, auth_headers, agent_id, version_id)

    assert resp.status_code == 201, resp.text


# --- F4: the normalization the deploy reader shares with the runtime loader ----


def test_a_gate_with_a_blank_route_is_refused_like_the_runner_refuses_it(
    client: Any, auth_headers: dict[str, str], clean_db: None, tmp_path: Path
) -> None:
    """AC1 / F4: a gate whose stripped route is blank is poison, not a silent drop.

    Plan test table (API), the blank-route row. The first half asserts rather
    than assumes the premise: `plugin_format.validate_bundle` already refuses
    these bytes at the storage entry point (`approval_policy.incomplete`), so the
    reader can only meet such a manifest on bytes that predate the rule or
    arrived out of band. That is exactly when it must fail CLOSED. The reader
    returns the `None` poison value the caller turns into a refusal, on the SAME
    condition `curie_runner.approval.resolve_approval_policy` raises
    `ApprovalPolicyError` and refuses to boot: a declared gate name that arms no
    tool. Returning an empty set instead would accept, at configuration time, a
    bundle the runner will not run -- the fail-open shape ADR-0050 exists to
    prevent.
    """

    agent_id = _create_agent(client, auth_headers)
    version_id = _create_version(client, auth_headers, agent_id)
    upload = _upload(client, auth_headers, agent_id, version_id, _archive([_gate("   ")]))
    assert upload.status_code == 422, upload.text
    assert "approval_policy.incomplete" in {
        e["code"] for e in upload.json()["detail"]["errors"]
    }, upload.text

    root = _write_bundle_root(tmp_path / "blank-route", [_gate("   ")])
    assert bundles.declared_approval_routes(root) is None, (
        "a gate arming no tool must poison the declared set, not read as 'declares "
        "nothing' -- the runner raises ApprovalPolicyError on this exact condition"
    )


def test_two_gates_on_one_tool_declare_only_the_last_route(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """F4: the declared set is the LAST-WINS tool-to-route map, not a route union.

    Plan test table (API), the repeated-tool row. `resolve_approval_policy`
    builds `{tool: route}` and keeps only the last gate for a repeated tool, so
    `alpha` below can never be raised at runtime. A reader that unioned every
    gate's route value would refuse this deploy over a route that does not exist,
    which is an over-refusal of a bundle `validate_bundle` accepts and the runner
    boots.
    """

    agent_id = _create_agent(client, auth_headers, routes=_routes("omega"))
    version_id = _bundled_version(
        client, auth_headers, agent_id, [_gate("alpha"), _gate("omega")]
    )

    resp = _deploy(client, auth_headers, agent_id, version_id)

    assert resp.status_code == 201, resp.text


def test_declared_route_normalization_matches_the_frozen_vector(tmp_path: Path) -> None:
    """Parity seam / F4: the API reader executes the frozen normalization vector.

    Plan test table (API), the vector row. The deploy-time reader and the runtime
    loader must normalize identically, including the refusal case, or a bundle
    passes this configuration gate and then boots with a different set of gates.
    Code cannot be shared across the seam -- `packages/plugin-format` is frozen
    and `curie_api` must not import `curie_runner` -- so
    `tests/vectors/approval-route-normalization.json` is the rule and both sides
    EXECUTE it. The runner half is
    `runner/tests/test_approval.py::test_route_normalization_vector_matches_the_runtime_loader`,
    which asserts a RAISE where this asserts `None`, so the vector cannot be
    satisfied by weakening either side's fail-closed posture.
    """

    cases = json.loads(VECTOR.read_text(encoding="utf-8"))["cases"]
    assert cases, "the frozen vector must carry cases, or this test proves nothing"

    for case in cases:
        root = _write_bundle_root(tmp_path / str(case["id"]), case["gates"])
        declared = bundles.declared_approval_routes(root)
        if case["expected"] == "rejected":
            assert declared is None, (
                f"case {case['id']!r}: the reader returned {declared!r}, but the frozen "
                "vector says this bundle is refused; an empty set here accepts a bundle "
                "the runner refuses to boot"
            )
            continue
        assert declared == set(case["expected"]), (
            f"case {case['id']!r}: the reader declared {declared!r} but the frozen vector "
            f"says {case['expected']!r} -- normalization drift between the deploy-time "
            "reader and the runtime loader is the #453/#544 fail-open shape"
        )


# --- The operator-side write: `PATCH /agents/{id}` with `approval_routes` ------


def test_patch_dropping_a_route_declared_by_an_active_deployment_is_refused(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1: an unbind strands a live agent exactly as a bad deploy would.

    Plan test table (API), the patch-drop row. A write to `approval_routes` is a
    full replacement (`--route`, `--routes-from` and `--clear-routes` all replace
    the whole map), so dropping `ops` while a deployment whose bundle declares it
    is active is the same defect arriving from the operator's side.
    """

    agent_id = _create_agent(client, auth_headers, routes=_routes("ops", "finance"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])
    assert _deploy(client, auth_headers, agent_id, version_id).status_code == 201

    resp = client.patch(
        f"/agents/{agent_id}", json={"approval_routes": _routes("finance")}, headers=auth_headers
    )

    assert resp.status_code == 422, resp.text
    assert "'ops'" in _detail(resp), resp.text
    assert set(_agent(client, auth_headers, agent_id)["approval_routes"]) == {"ops", "finance"}


def test_patch_clearing_all_routes_is_refused_while_a_gated_deployment_is_active(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1: the explicit `{}` clear is a real request here, and it is refused.

    Plan test table (API), the clear row. `{}` is not "omitted" for this field --
    it is the documented way to clear the map (#247) -- so it has to be judged,
    not skipped, or `--clear-routes` walks straight past the gate.
    """

    agent_id = _create_agent(client, auth_headers, routes=_routes("ops"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])
    assert _deploy(client, auth_headers, agent_id, version_id).status_code == 201

    resp = client.patch(f"/agents/{agent_id}", json={"approval_routes": {}}, headers=auth_headers)

    assert resp.status_code == 422, resp.text
    assert "'ops'" in _detail(resp), resp.text
    assert set(_agent(client, auth_headers, agent_id)["approval_routes"]) == {"ops"}


def test_patch_that_keeps_every_declared_route_is_accepted(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1's falsifiable pair on the write side: a rewrite that keeps `ops` is 200.

    Plan test table (API), the patch-accepted row. The gate is about the ROUTE
    NAME's presence, not about the binding's contents, so moving the resolution
    channel must stay an ordinary operator action.
    """

    agent_id = _create_agent(client, auth_headers, routes=_routes("ops"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])
    assert _deploy(client, auth_headers, agent_id, version_id).status_code == 201

    resp = client.patch(
        f"/agents/{agent_id}",
        json={"approval_routes": {"ops": _binding("C0EXAMPLE2")}},
        headers=auth_headers,
    )

    assert resp.status_code == 200, resp.text
    routes = _agent(client, auth_headers, agent_id)["approval_routes"]
    assert routes["ops"]["resolution"]["address"] == "C0EXAMPLE2"


def test_ending_the_newest_deployment_still_refuses_while_an_older_active_row_declares_the_route(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """F2: the check unions EVERY active row, not the newest one per environment.

    Plan test table (API), the F2 row. `crud.get_active_deployment` deliberately
    returns only the newest active row per environment, git-flow appends a new
    active row per push without superseding older ones, and `end_deployment`
    stops exactly one row -- so several active rows routinely coexist in one
    environment, and the worker's resolve query orders over ALL of them. A check
    built on "newest per environment" would let the FIRST patch below through:
    the newest row is the ungated one, while the older gated row is still active
    and still bootable. The three steps are the discriminator, in order.
    """

    agent_id = _create_agent(client, auth_headers, routes=_routes("ops"))
    gated = _bundled_version(client, auth_headers, agent_id, [_gate("ops")], label="gated")
    ungated = _bundled_version(client, auth_headers, agent_id, None, label="ungated")

    older = _deploy(client, auth_headers, agent_id, gated)
    assert older.status_code == 201, older.text
    newer = _deploy(client, auth_headers, agent_id, ungated)
    assert newer.status_code == 201, newer.text

    clear: dict[str, Any] = {"approval_routes": {}}
    first = client.patch(f"/agents/{agent_id}", json=clear, headers=auth_headers)
    assert first.status_code == 422, (
        "the newest active row declares nothing, but the older one is still active "
        f"and still declares 'ops': {first.text}"
    )
    assert "'ops'" in _detail(first), first.text

    assert (
        client.delete(f"/deployments/{newer.json()['id']}", headers=auth_headers).status_code == 204
    )
    second = client.patch(f"/agents/{agent_id}", json=clear, headers=auth_headers)
    assert second.status_code == 422, second.text

    assert (
        client.delete(f"/deployments/{older.json()['id']}", headers=auth_headers).status_code == 204
    )
    third = client.patch(f"/agents/{agent_id}", json=clear, headers=auth_headers)
    assert third.status_code == 200, third.text
    assert _agent(client, auth_headers, agent_id)["approval_routes"] is None


def test_clearing_routes_succeeds_only_after_every_declaring_deployment_is_ended(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1 recovery: the refusal is not a one-way door, and it is not one DELETE.

    Plan test table (API), the recovery row, and question (b) in the plan's own
    words: to clear the map an operator must end EVERY active deployment whose
    bundle declares one of those routes. Two active rows in two environments both
    declare `ops` here, so ending one is deliberately not enough.
    """

    agent_id = _create_agent(client, auth_headers, routes=_routes("ops"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])
    dev = _deploy(client, auth_headers, agent_id, version_id, "dev")
    assert dev.status_code == 201, dev.text
    prod = _deploy(client, auth_headers, agent_id, version_id, "prod")
    assert prod.status_code == 201, prod.text

    clear: dict[str, Any] = {"approval_routes": {}}
    assert client.patch(f"/agents/{agent_id}", json=clear, headers=auth_headers).status_code == 422

    assert (
        client.delete(f"/deployments/{dev.json()['id']}", headers=auth_headers).status_code == 204
    )
    halfway = client.patch(f"/agents/{agent_id}", json=clear, headers=auth_headers)
    assert halfway.status_code == 422, (
        f"the prod row still declares 'ops' and is still active: {halfway.text}"
    )

    assert (
        client.delete(f"/deployments/{prod.json()['id']}", headers=auth_headers).status_code == 204
    )
    cleared = client.patch(f"/agents/{agent_id}", json=clear, headers=auth_headers)
    assert cleared.status_code == 200, cleared.text
    assert _agent(client, auth_headers, agent_id)["approval_routes"] is None


def test_a_prod_declared_route_blocks_a_dev_shaped_route_write(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1, question (c): the write is judged against BOTH environments at once.

    Plan test table (API), the two-environment row. The proposed map below
    satisfies everything the dev deployment declares, so a check scoped to one
    environment (or to the newest row of the environment it happened to look at)
    would accept it while the prod deployment's `compliance` route is silently
    unbound.
    """

    agent_id = _create_agent(client, auth_headers, routes=_routes("ops", "compliance"))
    dev_version = _bundled_version(client, auth_headers, agent_id, [_gate("ops")], label="dev")
    prod_version = _bundled_version(
        client, auth_headers, agent_id, [_gate("compliance")], label="prod"
    )
    assert _deploy(client, auth_headers, agent_id, dev_version, "dev").status_code == 201
    assert _deploy(client, auth_headers, agent_id, prod_version, "prod").status_code == 201

    resp = client.patch(
        f"/agents/{agent_id}", json={"approval_routes": _routes("ops")}, headers=auth_headers
    )

    assert resp.status_code == 422, resp.text
    assert "'compliance'" in _detail(resp), resp.text
    assert set(_agent(client, auth_headers, agent_id)["approval_routes"]) == {"ops", "compliance"}


def test_a_refused_mixed_field_patch_persists_no_field(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """F3: a refused PATCH persists nothing, including the fields it also carried.

    Plan test table (API), the mixed-field row. `update_agent` commits `model`,
    `thinking`, `memory` and `approval_required_tools` through independently
    committing helpers BEFORE it reaches the `approval_routes` block, so a
    refusal placed at that block would leave four fields persisted from a request
    the API answered 422. The preflight therefore runs at the top of the handler,
    and this is the test that says so through the read path.
    """

    agent_id = _create_agent(client, auth_headers, routes=_routes("ops"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])
    assert _deploy(client, auth_headers, agent_id, version_id).status_code == 201
    before = _agent(client, auth_headers, agent_id)

    resp = client.patch(
        f"/agents/{agent_id}",
        json={
            "model": "claude-sonnet-5",
            "thinking": "enabled:2000",
            "memory": True,
            "approval_required_tools": ["Bash"],
            "approval_routes": {},
        },
        headers=auth_headers,
    )

    assert resp.status_code == 422, resp.text
    after = _agent(client, auth_headers, agent_id)
    for field in ("model", "thinking", "memory", "approval_required_tools", "approval_routes"):
        assert after[field] == before[field], (
            f"{field} was committed by a request the API refused: "
            f"{before[field]!r} -> {after[field]!r}"
        )


# --- Verbatim, case-sensitive matching ----------------------------------------


def test_a_whitespace_padded_binding_key_does_not_satisfy_a_declared_route(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1 edge: the binding key is matched VERBATIM against the stripped route.

    Plan test table (API), the whitespace row. The worker's lookup
    (`kernel.py`, `(approval_routes or {}).get(route_name)`) and the API's
    resolve-time lookup (`crud.get_approval_route_binding`) are both exact dict
    lookups, so `" ops "` binds nothing at runtime and the config check must say
    so rather than trimming the operator's key into agreement. The message quotes
    with `!r` precisely so the invisible difference is visible in the 422.
    """

    agent_id = _create_agent(client, auth_headers, routes={" ops ": _binding()})
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])

    resp = _deploy(client, auth_headers, agent_id, version_id)

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert "'ops'" in detail, detail
    assert "' ops '" in detail, (
        "the bound key must be quoted so the operator can SEE the padding that "
        f"makes it a different route: {detail}"
    )


def test_route_matching_is_case_sensitive(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """AC1 edge: `Ops` does not bind `ops` (ADR-0046 Decision B).

    Plan test table (API), the case row. Both runtime consumers of this map do a
    case-sensitive exact lookup, so folding case here would report a binding that
    does not exist -- a silent failure at request time is exactly what this
    ticket exists to move forward to configuration time.
    """

    agent_id = _create_agent(client, auth_headers, routes=_routes("Ops"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])

    resp = _deploy(client, auth_headers, agent_id, version_id)

    assert resp.status_code == 422, resp.text
    assert "'ops'" in _detail(resp), resp.text


# --- Stage 5 regression pin: the preflight's own read is bounded too ----------


def test_patch_keeping_every_route_under_a_tightened_archive_cap_is_refused_as_too_large_not_500(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    """Code review P2 (`deploy.py:204-210`): AC1's preflight must translate a cap failure.

    The preflight extracts the stored bundle to learn what it DECLARES, so an
    operator who tightens the archive caps under an already-deployed bundle
    makes every `approval_routes` write raise an uncaught extraction error --
    including this one, which keeps every declared route and would otherwise be
    the accepted case. ADR-0059 decision 3 already fixes the operator-facing
    answer for stored bytes that no longer fit the current caps: the
    `BundleTooLarge` 422 `revalidate_stored_bundle` produces at
    `POST /deployments`, named in `test_bundle_ingestion_bounds.py`. A 500 is
    not that answer, and it tells the operator nothing about size.

    The caps are moved the same way the ingestion-bounds suite moves them, by
    patching `deploy.get_settings`, because the preflight calls
    `check_approval_route_bindings` without a `settings` argument and that call
    resolves the name in `deploy`'s own namespace.
    """

    from unittest.mock import patch

    from curie_api import deploy
    from curie_api.config import Settings

    agent_id = _create_agent(client, auth_headers, routes=_routes("ops"))
    version_id = _bundled_version(client, auth_headers, agent_id, [_gate("ops")])
    assert _deploy(client, auth_headers, agent_id, version_id).status_code == 201

    with patch.object(deploy, "get_settings", lambda: Settings(bundle_max_uncompressed_bytes=1)):
        resp = client.patch(
            f"/agents/{agent_id}",
            json={"approval_routes": {"ops": _binding("C0EXAMPLE2")}},
            headers=auth_headers,
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert "size/ratio" in detail, detail
    assert "rebuilt and re-uploaded" in detail, detail
    assert version_id in detail, detail  # actionable: names the affected version

    # Refused BEFORE the write, like every other preflight refusal here.
    routes = _agent(client, auth_headers, agent_id)["approval_routes"]
    assert set(routes) == {"ops"}, routes
    assert routes["ops"]["resolution"]["address"] == "C0EXAMPLE1", routes
