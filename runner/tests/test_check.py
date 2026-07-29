"""Tests for the offline MCP load check (`curie_runner.check`, issue #337).

Test-first: these pin the frozen runner<->CLI JSON seam (plan Section 3) and the
verdict rules. Until ``check.py`` exists the module import fails collection --
that is the intended red for the Stage-2 test-writer pass.

Mocking discipline (plan Section 5): nothing here is mocked. Tests 1-3 are pure
functions over real fixture dirs and literal ``McpServerStatus``-shaped dicts;
test 4 runs the module as a subprocess; test 5 drives the *real* Claude Code
loader (gated on the ``claude`` CLI being present).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from curie_runner.check import evaluate, extract_declared, run_check

_HERE = Path(__file__).resolve().parent
_FIXTURES = _HERE / "fixtures"
_MCP_GREEN = _FIXTURES / "mcp_green"
_MCP_RED_POINTER = _FIXTURES / "mcp_red_pointer"
_MCP_RED_BROKEN = _FIXTURES / "mcp_red_broken"
_PLUGIN_FORMAT_FIXTURES = _HERE.parents[1] / "packages/plugin-format/tests/fixtures"

_CLAUDE_ON_PATH = shutil.which("claude") is not None

# The github-issues example forwards this credential env var. Held as a named
# constant (not an inline "<NAME>": "<placeholder>" literal pair) so the
# secret-scan pre-commit hook does not false-positive on the access-token-shaped
# placeholder; the value is a ${VAR} interpolation reference, never a real token.
_GH_TOKEN_ENV = "GITHUB_PERSONAL_ACCESS_TOKEN"
_GH_TOKEN_PLACEHOLDER = "${" + _GH_TOKEN_ENV + "}"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _declared(
    name: str,
    source: str = "plugin.json",
    form: str = "inline",
    *,
    authed: bool = False,
    cred_vars: list[str] | None = None,
    remote: bool = False,
) -> dict:
    return {
        "name": name,
        "source": source,
        "form": form,
        "authed": authed,
        "cred_vars": list(cred_vars or []),
        "remote": remote,
    }


def _tool(name: str) -> dict:
    return {"name": name}


def _status(
    name: str,
    *,
    status: str = "connected",
    scope: str | None = "dynamic",
    tools: list[dict] | None = None,
    error: str | None = None,
) -> dict:
    entry: dict = {"name": name, "status": status, "tools": list(tools or [])}
    if scope is not None:
        entry["scope"] = scope
    if error is not None:
        entry["error"] = error
    return entry


def _write_bundle(root: Path, manifest: dict, *, mcp_files: dict[str, dict] | None = None) -> Path:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    for rel, payload in (mcp_files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# Test 1 -- declared extraction over real bundle dirs (no mocks)
# --------------------------------------------------------------------------- #
def test_extract_inline_object_form() -> None:
    # Canonical declared shape: exactly {name, source, form, authed, cred_vars,
    # remote},
    # nothing else. A plain stdio server with no env is authed=False, cred_vars=[].
    assert extract_declared(str(_MCP_GREEN)) == [
        {
            "name": "green-probe",
            "source": "plugin.json",
            "form": "inline",
            "authed": False,
            "cred_vars": [],
            "remote": False,
        }
    ]


def test_extract_string_pointer_form() -> None:
    declared = extract_declared(str(_MCP_RED_POINTER))
    by_name = {d["name"]: d for d in declared}
    assert "pointer-probe" in by_name
    assert by_name["pointer-probe"]["form"] == "string_pointer"
    assert by_name["pointer-probe"]["source"] == "plugin.json"


def test_extract_string_pointer_missing_file_is_red_path(tmp_path: Path) -> None:
    # Intent still surfaces (a string-pointer declaration is visible) even though
    # the pointed file does not exist; and the missing target drives a red verdict.
    bundle = _write_bundle(
        tmp_path / "missing_ptr", {"name": "missing-ptr", "mcpServers": "config/nope.json"}
    )
    declared = extract_declared(str(bundle))
    assert declared, "a string-pointer declaration must still surface as a declared entry"
    assert all(d["form"] == "string_pointer" for d in declared)
    result = evaluate(declared, [])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_extract_bare_mcp_json_form(tmp_path: Path) -> None:
    bundle = _write_bundle(
        tmp_path / "bare",
        {"name": "bare-bundle"},
        mcp_files={".mcp.json": {"mcpServers": {"bare-probe": {"command": "python3"}}}},
    )
    declared = extract_declared(str(bundle))
    by_name = {d["name"]: d for d in declared}
    assert "bare-probe" in by_name
    assert by_name["bare-probe"]["form"] == "bare_file"
    assert by_name["bare-probe"]["source"] == ".mcp.json"


def test_extract_root_plugin_json_fallback_is_reported(tmp_path: Path) -> None:
    # Root-level plugin.json (no .claude-plugin/ dir) is the accepted fallback
    # location (packages/plugin-format's MANIFEST_LOCATIONS, issue #653); the
    # resolver must find it and the declared server must still surface.
    bundle = tmp_path / "root_manifest"
    bundle.mkdir()
    (bundle / "plugin.json").write_text(
        json.dumps({"name": "root-bundle", "mcpServers": {"root-probe": {"command": "python3"}}}),
        encoding="utf-8",
    )
    declared = extract_declared(str(bundle))
    by_name = {d["name"]: d for d in declared}
    assert "root-probe" in by_name
    assert by_name["root-probe"]["form"] == "inline"
    assert by_name["root-probe"]["source"] == "plugin.json"


def test_extract_no_mcp_anywhere_is_empty(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path / "nomcp", {"name": "no-mcp-bundle"})
    assert extract_declared(str(bundle)) == []


# --------------------------------------------------------------------------- #
# Test 1b -- authed flag: a server carrying a credential (env/headers) is marked
# so the report can say it was NOT exercised by the credential-free offline check.
# --------------------------------------------------------------------------- #
def test_extract_authed_inline_env_marks_only_the_credentialed_server(
    tmp_path: Path,
) -> None:
    # Inline plugin.json form: a non-empty `env` map is the authed signal; a plain
    # stdio server with no env is authed=False.
    bundle = _write_bundle(
        tmp_path / "authed_inline",
        {
            "name": "authed-inline",
            "mcpServers": {
                "github": {
                    "command": "mcp-server-github",
                    "args": [],
                    "env": {_GH_TOKEN_ENV: _GH_TOKEN_PLACEHOLDER},
                },
                "plain": {"command": "python3", "args": []},
            },
        },
    )
    by_name = {d["name"]: d for d in extract_declared(str(bundle))}
    assert by_name["github"]["authed"] is True
    # cred_vars names the real env-var to forward via --secret, not the server name.
    assert by_name["github"]["cred_vars"] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]
    assert by_name["plain"]["authed"] is False
    assert by_name["plain"]["cred_vars"] == []


def test_extract_authed_bare_mcp_json_env_marks_server(tmp_path: Path) -> None:
    # Bare .mcp.json form (the real github-issues example shape): the env block
    # with a ${VAR} value is the authed signal.
    bundle = _write_bundle(
        tmp_path / "authed_bare",
        {"name": "authed-bare"},
        mcp_files={
            ".mcp.json": {
                "mcpServers": {
                    "github": {
                        "command": "mcp-server-github",
                        "env": {_GH_TOKEN_ENV: _GH_TOKEN_PLACEHOLDER},
                    },
                    "plain": {"command": "python3"},
                }
            }
        },
    )
    by_name = {d["name"]: d for d in extract_declared(str(bundle))}
    assert by_name["github"]["authed"] is True
    assert by_name["github"]["cred_vars"] == ["GITHUB_PERSONAL_ACCESS_TOKEN"]
    assert by_name["plain"]["authed"] is False
    assert by_name["plain"]["cred_vars"] == []


def test_extract_empty_env_is_not_authed(tmp_path: Path) -> None:
    # The signal is a NON-EMPTY env map; an empty env block does not carry a
    # credential and must stay authed=False.
    bundle = _write_bundle(
        tmp_path / "empty_env",
        {"name": "empty-env", "mcpServers": {"noenv": {"command": "python3", "env": {}}}},
    )
    by_name = {d["name"]: d for d in extract_declared(str(bundle))}
    assert by_name["noenv"]["authed"] is False


def test_extract_authed_remote_headers_marks_server(tmp_path: Path) -> None:
    # For a REMOTE server, an Authorization `headers` map is the authed signal.
    bundle = _write_bundle(
        tmp_path / "authed_remote",
        {
            "name": "authed-remote",
            "mcpServers": {
                "remote": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer ${REMOTE_TOKEN}"},
                }
            },
        },
    )
    by_name = {d["name"]: d for d in extract_declared(str(bundle))}
    assert by_name["remote"]["authed"] is True
    # The ${VAR} placeholder inside the header value is the credential var name.
    assert by_name["remote"]["cred_vars"] == ["REMOTE_TOKEN"]


# --------------------------------------------------------------------------- #
# Test 2 -- verdict pure function over literal McpServerStatus-shaped dicts
# --------------------------------------------------------------------------- #
def test_verdict_zero_declared_is_green() -> None:
    result = evaluate([], [_status("plugin:x:y", tools=[_tool("t")])])
    assert result["verdict"] == "green"
    assert result["reasons"] == []


def test_verdict_declared_none_registered_is_red() -> None:
    result = evaluate([_declared("a")], [])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_connected_with_zero_tools_is_red() -> None:
    result = evaluate([_declared("a")], [_status("plugin:x:a", tools=[])])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_failed_propagates_error_into_reasons() -> None:
    result = evaluate(
        [_declared("a")],
        [_status("plugin:x:a", status="failed", tools=[], error="spawn ENOENT: bad-command")],
    )
    assert result["verdict"] == "red"
    assert any("spawn ENOENT" in r for r in result["reasons"])


def test_verdict_needs_auth_is_red() -> None:
    result = evaluate([_declared("a")], [_status("plugin:x:a", status="needs-auth", tools=[])])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_pending_at_deadline_is_red() -> None:
    result = evaluate([_declared("a")], [_status("plugin:x:a", status="pending", tools=[])])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_scoped_dynamic_name_matches_declared() -> None:
    # plugin:<bundle>:<server> with scope "dynamic" matches declared "probe" by
    # the :-segment suffix rule.
    result = evaluate(
        [_declared("probe")],
        [_status("plugin:mybundle:probe", scope="dynamic", tools=[_tool("word_count")])],
    )
    assert result["verdict"] == "green"
    assert result["reasons"] == []


def test_verdict_scope_missing_name_prefix_fallback_is_green() -> None:
    # scope key intentionally absent -> plugin_owned via the "plugin:" name-prefix
    # fallback (Section 3 rule 1), so the server still counts and the verdict is green.
    own = _status("plugin:mybundle:probe", scope=None, tools=[_tool("word_count")])
    assert "scope" not in own
    result = evaluate([_declared("probe")], [own])
    assert result["verdict"] == "green"


def test_verdict_partial_registration_is_red() -> None:
    # Two declared, only one connected-with-tools -> red (partial coverage).
    result = evaluate(
        [_declared("a"), _declared("b")],
        [_status("plugin:x:a", tools=[_tool("t")])],
    )
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_unmatched_declared_with_ambient_connected_is_red() -> None:
    # F2 hole (load-bearing): a declared own-server that never registered must go
    # RED even when the host has an AMBIENT (scope project/user) connected-with-tools
    # server present. Deletion check: without the own-server filter + declared-
    # anchoring, a globally-aggregate "is anything connected with tools?" rule would
    # count the ambient server and FALSE-GREEN this exact shape -- the #336 slip.
    #
    # The ambient server's NAME collides with the declared server by the
    # :-segment-suffix rule ("something:probe" ends with ":probe"), so declared-
    # anchoring ALONE is not enough: _find_match would match it. Only the scope
    # own-filter (scope=="project" is not plugin-owned) excludes it and keeps this
    # RED. Deletion check: remove the scope own-filter and this test false-greens.
    ambient = _status("something:probe", scope="project", tools=[_tool("ambient_tool")])
    result = evaluate([_declared("probe")], [ambient])
    assert result["verdict"] == "red"
    assert result["reasons"]


def test_verdict_ambient_server_never_flips_green() -> None:
    own = _status("plugin:x:a", scope="dynamic", tools=[_tool("t")])
    ambient = _status("proj-server", scope="user", tools=[_tool("ambient_tool")])
    result = evaluate([_declared("a")], [own, ambient])
    assert result["verdict"] == "green"


def test_verdict_ambient_server_never_rescues_red() -> None:
    failed_own = _status("plugin:x:a", status="failed", tools=[], error="boom")
    ambient = _status("proj-server", scope="project", tools=[_tool("ambient_tool")])
    result = evaluate([_declared("a")], [failed_own, ambient])
    assert result["verdict"] == "red"


# --------------------------------------------------------------------------- #
# Test 3 -- string-pointer fingerprint (hints, not reasons) + reasons invariant
# --------------------------------------------------------------------------- #
def test_string_pointer_fingerprint_lives_in_hints_not_reasons() -> None:
    # The real loader silently ignores the string-pointer form, so nothing
    # registers; the diagnostic fingerprint must surface in `hints`.
    # Deletion check: remove the hint emission and this fails.
    declared = extract_declared(str(_MCP_RED_POINTER))
    result = evaluate(declared, [])
    assert result["verdict"] == "red"
    assert any("string pointer" in h.lower() for h in result["hints"])
    assert not any("string pointer" in r.lower() for r in result["reasons"])


def test_reasons_nonempty_iff_verdict_not_green_and_green_may_carry_hints() -> None:
    green = evaluate([], [])
    assert green["verdict"] == "green"
    assert green["reasons"] == []

    red = evaluate([_declared("a")], [])
    assert red["verdict"] == "red"
    assert red["reasons"]

    # E8 bare-file-rescued shape: declared as a string pointer but registered
    # anyway (a coexisting bare .mcp.json). Verdict is GREEN, reasons empty, yet
    # the advisory string-pointer fingerprint still appears in hints.
    rescued = evaluate(
        [_declared("probe", form="string_pointer")],
        [_status("plugin:x:probe", scope="dynamic", tools=[_tool("t")])],
    )
    assert rescued["verdict"] == "green"
    assert rescued["reasons"] == []
    assert any("string pointer" in h.lower() for h in rescued["hints"])


# --------------------------------------------------------------------------- #
# Test 3b -- authed-server advisory always lands in hints (green AND red), so a
# demo-watcher can't misread a credential-free result. The offline check runs
# --network none and forwards no secret: a green proves only wiring (tool-list
# needs no auth) and a red may mean only "no token", not "broken".
# --------------------------------------------------------------------------- #
_AUTHED_ADVISORY = "not exercised offline"


def _authed_hint(result: dict) -> str:
    return next(h for h in result["hints"] if _AUTHED_ADVISORY in h)


def test_authed_advisory_in_hints_when_registered_green() -> None:
    # Authed server connected with tools -> verdict green, yet the advisory must
    # still fire so the green is not read as "auth verified".
    declared = [_declared("github", authed=True, cred_vars=["GITHUB_PERSONAL_ACCESS_TOKEN"])]
    result = evaluate(declared, [_status("plugin:x:github", tools=[_tool("list_issues")])])
    assert result["verdict"] == "green"
    assert result["reasons"] == []
    advisory = _authed_hint(result)
    assert "github" in advisory
    # --secret forwards an env-var NAME, so the advisory must name the real
    # credential var, NOT the server name (following `--secret github` leaves the
    # token absent and the advertised end-to-end test fails).
    assert "--secret GITHUB_PERSONAL_ACCESS_TOKEN" in advisory
    assert "--secret github" not in advisory


def test_authed_advisory_in_hints_when_absent_red() -> None:
    # Authed server never registered -> verdict red; the advisory still fires so
    # the red is not necessarily read as "broken" (may just be "no token").
    declared = [_declared("github", authed=True, cred_vars=["GITHUB_PERSONAL_ACCESS_TOKEN"])]
    result = evaluate(declared, [])
    assert result["verdict"] == "red"
    advisory = _authed_hint(result)
    assert "github" in advisory
    assert "--secret GITHUB_PERSONAL_ACCESS_TOKEN" in advisory
    assert "--secret github" not in advisory


def test_authed_advisory_names_header_var() -> None:
    # A remote server's credential var comes from the ${VAR} header placeholder.
    declared = [_declared("remote", authed=True, cred_vars=["REMOTE_TOKEN"])]
    advisory = _authed_hint(evaluate(declared, []))
    assert "--secret REMOTE_TOKEN" in advisory


def test_authed_advisory_names_multiple_vars() -> None:
    declared = [_declared("multi", authed=True, cred_vars=["VAR_ONE", "VAR_TWO"])]
    advisory = _authed_hint(evaluate(declared, []))
    assert "--secret VAR_ONE --secret VAR_TWO" in advisory


def test_authed_advisory_falls_back_to_generic_when_no_cred_var() -> None:
    # Authed by heuristic but no extractable var (e.g. a literal header token):
    # fall back to the generic placeholder rather than emit a wrong concrete name.
    declared = [_declared("mystery", authed=True)]
    advisory = _authed_hint(evaluate(declared, []))
    assert "--secret <NAME>" in advisory


def test_non_authed_server_gets_no_offline_advisory() -> None:
    # Deletion check: the advisory is gated on authed=True. A plain server that
    # needs no credential must NOT carry the "not exercised offline" hint.
    declared = [_declared("plain", authed=False)]
    result = evaluate(declared, [_status("plugin:x:plain", tools=[_tool("t")])])
    assert not any(_AUTHED_ADVISORY in h for h in result["hints"]), result["hints"]


# --------------------------------------------------------------------------- #
# Test 4 -- JSON purity + exit codes (module run as a subprocess)
# --------------------------------------------------------------------------- #
def _run_module(
    plugin_dir: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CURIE_PLUGIN_DIR": plugin_dir}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "curie_runner.check"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_module_nonexistent_dir_exits_2_with_invalid_bundle_json(tmp_path: Path) -> None:
    proc = _run_module(str(tmp_path / "does-not-exist"), env_extra={"HOME": str(tmp_path)})
    assert proc.returncode == 2
    # stdout must be exactly ONE json.loads-able document (stderr may carry logs).
    doc = json.loads(proc.stdout)
    assert doc["verdict"] == "invalid_bundle"
    assert doc["reasons"]


def test_module_invalid_bundle_exits_2_with_json(tmp_path: Path) -> None:
    bad = _PLUGIN_FORMAT_FIXTURES / "bad_manifest_name"
    proc = _run_module(str(bad), env_extra={"HOME": str(tmp_path)})
    assert proc.returncode == 2
    doc = json.loads(proc.stdout)
    assert doc["verdict"] == "invalid_bundle"
    assert doc["reasons"]


# --------------------------------------------------------------------------- #
# Test 5 -- real-loader integration (gated on the real `claude` CLI, isolated HOME)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not _CLAUDE_ON_PATH,
    reason="requires the real `claude` CLI on PATH to spawn MCP servers",
)
def test_run_check_green_bundle_registers_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolated HOME (plan Section 5, E7): the CLI caches a failed MCP connection
    # ~15 min so consecutive host runs go flaky, and an ambient HOME leaks
    # project/user MCP servers into the status; a clean HOME leaves only the
    # bundle's own servers. No credential env is set (spike: connect() is
    # credential-free and there is no query() in the code path).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = anyio.run(run_check, str(_MCP_GREEN))
    assert result["verdict"] == "green", result
    assert result["reasons"] == []


def test_run_check_red_pointer_bundle_is_invalid_bundle() -> None:
    # The `.mcp.json` string-pointer form is rejected statically by `load_plugins`
    # at run_check's step 1, so this never reaches `red`: a genuinely malformed
    # bundle is `invalid_bundle`. Detection is static -- no container boot and no
    # MCP server spawn -- so this needs no real `claude` CLI and stays unskipped.
    result = anyio.run(run_check, str(_MCP_RED_POINTER))
    assert result["verdict"] == "invalid_bundle", result
    assert result["reasons"], result
    joined = " ".join(result["reasons"])
    # Prove it was rejected for THIS reason (the pointer form), not some other
    # bundle error: the stable diagnostic code plus the offending path.
    assert "[mcp.declared_pointer]" in joined, joined
    assert "config/mcp.json" in joined, joined


# --------------------------------------------------------------------------- #
# Remote servers: unreachable by design, not a bundle defect (#1093)
# --------------------------------------------------------------------------- #
def test_remote_server_declared_by_url_only_is_marked_remote(tmp_path: Path) -> None:
    # The common remote shape carries neither `env` nor `headers` -- the endpoint
    # itself is the parameterised part -- so it was invisible to both the authed
    # flag and cred-var extraction, and produced a bare red with no explanation.
    root = _write_bundle(
        tmp_path,
        {"name": "b", "version": "0.1.0"},
        mcp_files={
            ".mcp.json": {"mcpServers": {"grafana": {"type": "http", "url": "${GRAFANA_MCP_URL}"}}}
        },
    )
    rows = extract_declared(str(root))
    assert rows == [
        _declared(
            "grafana",
            source=".mcp.json",
            form="bare_file",
            cred_vars=["GRAFANA_MCP_URL"],
            remote=True,
        )
    ]


def test_remote_url_var_strips_a_shell_style_default(tmp_path: Path) -> None:
    # `${VAR:-fallback}` is how a bundle keeps one declaration working across
    # tiers; the operator still forwards VAR, so name VAR and not the default.
    root = _write_bundle(
        tmp_path,
        {"name": "b", "version": "0.1.0"},
        mcp_files={
            ".mcp.json": {
                "mcpServers": {
                    "grafana": {"type": "http", "url": "${GRAFANA_MCP_URL:-http://svc:8000/mcp}"}
                }
            }
        },
    )
    assert extract_declared(str(root))[0]["cred_vars"] == ["GRAFANA_MCP_URL"]


def test_remote_advisory_says_the_red_is_expected_and_names_the_secret() -> None:
    from curie_runner.check import _remote_advisory

    hint = _remote_advisory("grafana", ["GRAFANA_MCP_URL"])
    # The point of the advisory: distinguish "offline contract did its job" from
    # "your bundle is broken", which both rendered as red before.
    assert "cannot be reached offline" in hint
    assert "NOT evidence" in hint
    assert "--secret GRAFANA_MCP_URL" in hint


def test_remote_advisory_takes_precedence_over_the_authed_one() -> None:
    # A remote server WITH headers is both remote and authed. Unreachable-by-
    # design is the more specific explanation, so it should be the one shown.
    from curie_runner.check import evaluate

    declared = [
        {
            "name": "grafana",
            "source": ".mcp.json",
            "form": "bare_file",
            "authed": True,
            "cred_vars": ["TOKEN"],
            "remote": True,
        }
    ]
    result = evaluate(declared, [])
    hints = " ".join(result["hints"])
    assert "cannot be reached offline" in hints
    assert "was not exercised offline" not in hints
