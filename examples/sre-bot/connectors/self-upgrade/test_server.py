"""Tests for the self-upgrade connector.

Two are load-bearing.

`test_the_posted_spec_is_the_cronjob_template_verbatim` is the security
property in a single assertion: this connector's argument for holding
namespace-wide `create` on `jobs` is that no caller input reaches the body. If
the body is ever assembled rather than copied, that argument is gone and the
grant is just a broad grant.

`test_the_label_it_stamps_is_the_one_it_selects_on` guards a failure that is
invisible when it happens: if the label written at creation and the selector used
to find running Jobs drift apart, the concurrency check silently matches nothing
and every call starts another overlapping upgrade.
"""

import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
import yaml

_MODULE_NAME = "sre_bot_self_upgrade_server"
_SERVER_PY = Path(__file__).parent / "server.py"

GOOD_KUBECONFIG = {
    "clusters": [{"cluster": {"server": "https://k8s.example:6443"}}],
    "users": [{"user": {"token": "upgrade-token"}}],
}

# What a real CronJob's `spec.jobTemplate` looks like, trimmed to the parts this
# file touches. The container is deliberately distinctive: an assertion that the
# posted spec still contains it is an assertion that nothing reassembled it.
JOB_TEMPLATE = {
    "metadata": {"labels": {"app": "sre-bot-self-upgrade"}},
    "spec": {
        "backoffLimit": 2,
        "template": {
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "check",
                        "image": "ghcr.io/curie-eng/curie-api:0.7.2",
                        "command": ["python3", "/opt/self-upgrade/redeploy.py"],
                    }
                ],
            }
        },
    },
}


def _load(
    tmp_path,
    kubeconfig=GOOD_KUBECONFIG,
    cronjob="sre-bot-self-upgrade",
    release_repo="curie-eng/curie",
    platform_cronjob="platform-upgrade",
):
    cfg = tmp_path / "kubeconfig"
    cfg.write_text(yaml.safe_dump(kubeconfig), encoding="utf-8")
    os.environ["KUBECONFIG_PATH"] = str(cfg)
    os.environ["SELF_UPGRADE_CRONJOB"] = cronjob
    os.environ["SELF_UPGRADE_NAMESPACE"] = "curie"
    os.environ["SELF_UPGRADE_RELEASE_REPO"] = release_repo
    os.environ["PLATFORM_UPGRADE_CRONJOB"] = platform_cronjob
    os.environ["SELF_UPGRADE_RELEASE_API"] = "https://api.github.test"
    sys.modules.pop(_MODULE_NAME, None)
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeClient:
    """Records what the tool asked the API server to do."""

    def __init__(
        self,
        seen,
        cronjob=(200, {"spec": {"jobTemplate": JOB_TEMPLATE}}),
        jobs=(200, {"items": []}),
        create=(201, {"metadata": {"name": "sre-bot-self-upgrade-abc12"}}),
    ):
        self.seen = seen
        self._cronjob = cronjob
        self._jobs = jobs
        self._create = create

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # These signatures MIRROR httpx.Client, keyword-only marker included. The
    # sibling scale connector shipped a verb that could never run because its
    # fake took the body positionally while the real client does not (#1947);
    # a fake looser than the thing it stands in for tests the fake.
    def get(self, path, *, params=None):
        if "/cronjobs/" in path:
            self.seen["cronjob_path"] = path
            return _Response(*self._cronjob)
        self.seen["jobs_path"] = path
        self.seen["jobs_params"] = params
        return _Response(*self._jobs)

    def post(self, path, *, json=None, content=None, headers=None):
        self.seen["post_path"] = path
        self.seen["post_body"] = json
        return _Response(*self._create)


def test_the_posted_spec_is_the_cronjob_template_verbatim(tmp_path, monkeypatch):
    """No caller input reaches the body -- the whole basis for the jobs grant."""
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen))
    result = json.loads(srv.upgrade_self())
    assert result["ok"] is True
    assert seen["post_body"]["spec"] == JOB_TEMPLATE["spec"]


def test_the_tool_exposes_no_parameters_at_all(tmp_path):
    """A tool with no arguments has nothing to validate and nothing to escape."""
    srv = _load(tmp_path)
    assert inspect.signature(srv.upgrade_self).parameters == {}


def test_the_label_it_stamps_is_the_one_it_selects_on(tmp_path, monkeypatch):
    """Drift between the two makes the concurrency guard silently match nothing."""
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen))
    srv.upgrade_self()
    stamped = seen["post_body"]["metadata"]["labels"]
    selector = seen["jobs_params"]["labelSelector"]
    key, _, value = selector.partition("=")
    assert stamped[key] == value


def test_it_refuses_while_an_upgrade_is_still_running(tmp_path, monkeypatch):
    """Two overlapping runs race on creating the version; the loser leaves a row."""
    srv = _load(tmp_path)
    seen = {}
    running = {"items": [{"metadata": {"name": "in-flight"}, "status": {"active": 1}}]}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen, jobs=(200, running)))
    result = json.loads(srv.upgrade_self())
    assert result["ok"] is False
    assert "in-flight" in result["summary"]
    assert "post_body" not in seen


def test_a_finished_job_does_not_block_a_new_one(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    seen = {}
    done = {"items": [{"metadata": {"name": "old"}, "status": {"succeeded": 1}}]}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen, jobs=(200, done)))
    assert json.loads(srv.upgrade_self())["ok"] is True


def test_an_unset_cronjob_refuses_before_reaching_the_api(tmp_path, monkeypatch):
    """A missing config fails closed, like the other connectors' allowlists."""
    srv = _load(tmp_path, cronjob="")
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen))
    result = json.loads(srv.upgrade_self())
    assert result["ok"] is False
    assert seen == {}


def test_a_missing_cronjob_says_it_is_not_installed(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient({}, cronjob=(404, {})))
    result = json.loads(srv.upgrade_self())
    assert result["ok"] is False
    assert "not installed" in result["summary"]


@pytest.mark.parametrize("status", [401, 403])
def test_permission_errors_name_the_missing_verb(tmp_path, monkeypatch, status):
    """So the reader fixes RBAC instead of retrying a call that cannot succeed."""
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient({}, create=(status, {})))
    result = json.loads(srv.upgrade_self())
    assert result["ok"] is False
    assert "create on jobs" in result["summary"]


def test_prior_is_null_even_when_it_succeeds(tmp_path, monkeypatch):
    """This action has no snapshot; the platform must never record one."""
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient({}))
    result = json.loads(srv.upgrade_self())
    assert result["ok"] is True
    assert result["prior"] is None
    assert "no undo" in result["summary"]


def test_success_does_not_claim_the_upgrade_finished(tmp_path, monkeypatch):
    """Starting a Job is not finishing it, and the bot reports from this text."""
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient({}))
    summary = json.loads(srv.upgrade_self())["summary"]
    assert "does not" in summary and "succeeded" in summary


def test_the_created_job_name_comes_back(tmp_path, monkeypatch):
    """The bot needs it to watch the Job with the read-only tools."""
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient({}))
    result = json.loads(srv.upgrade_self())
    assert result["target"] == {
        "kind": "Job",
        "namespace": "curie",
        "name": "sre-bot-self-upgrade-abc12",
    }


def test_the_name_is_generated_server_side(tmp_path, monkeypatch):
    """Two approvals landing together must not collide on a fixed name."""
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen))
    srv.upgrade_self()
    metadata = seen["post_body"]["metadata"]
    assert metadata["generateName"].startswith("sre-bot-self-upgrade")
    assert "name" not in metadata


def test_tool_is_annotated_as_a_destructive_non_idempotent_write(tmp_path):
    srv = _load(tmp_path)
    assert srv.UPGRADE.readOnlyHint is False
    assert srv.UPGRADE.destructiveHint is True
    assert srv.UPGRADE.idempotentHint is False


def test_insecure_skip_tls_verify_is_refused(tmp_path):
    """A write path that skips verification can be pointed at an impostor."""
    srv = _load(
        tmp_path,
        kubeconfig={
            "clusters": [
                {"cluster": {"server": "https://k8s", "insecure-skip-tls-verify": True}}
            ],
            "users": [{"user": {"token": "upgrade-token"}}],
        },
    )
    assert "insecure-skip-tls-verify" in srv._client()


def test_missing_kubeconfig_is_a_sentence_not_a_traceback(tmp_path):
    srv = _load(tmp_path)
    srv.KUBECONFIG = str(tmp_path / "absent")
    result = json.loads(srv.upgrade_self())
    assert result["ok"] is False
    assert "not mounted" in result["summary"]


def test_the_token_never_appears_in_a_returned_message(tmp_path, monkeypatch):
    """A refusal is posted back into Slack; a credential must not ride along."""
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient({}, cronjob=(500, {})))
    assert "upgrade-token" not in srv.upgrade_self()


def test_the_fake_client_cannot_accept_a_call_the_real_one_rejects():
    """Pinned for the reason the sibling connector had to learn (#1947).

    `k8s-scale` passed its patch body positionally and every one of its tests
    passed, because its fake accepted it positionally too while the real
    `httpx.Client` takes everything after the URL keyword-only. The verb could
    never run. This connector's calls are already keyword, and this keeps the
    fake from quietly drifting looser than the client it stands in for.
    """

    for method in ("get", "post"):
        real = inspect.signature(getattr(httpx.Client, method)).parameters
        fake = inspect.signature(getattr(_FakeClient, method)).parameters
        # [0] is self, [1] is the URL; both are positional in httpx too.
        for name, param in list(fake.items())[2:]:
            assert name in real, f"_FakeClient.{method} accepts {name!r}, httpx does not"
            assert param.kind is inspect.Parameter.KEYWORD_ONLY
            assert real[name].kind is inspect.Parameter.KEYWORD_ONLY


# --- latest_release -------------------------------------------------------
#
# These keep the REAL httpx.Client and swap only its transport, for the reason
# the sibling scale connector had to learn the hard way (#1947): a hand-written
# double accepted a call the real client rejects, and every test passed while the
# tool could never run.

RELEASE_BODY = {
    "tag_name": "v0.8.1",
    "name": "v0.8.1",
    "html_url": "https://github.test/curie-eng/curie/releases/tag/v0.8.1",
    "published_at": "2026-08-30T20:40:35Z",
}


def _with_release_response(srv, monkeypatch, status=200, body=RELEASE_BODY, seen=None):
    """Point the module's httpx at a mock transport, keeping the real client."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["request"] = request
        return httpx.Response(status, json=body)

    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(srv.httpx, "Client", factory)


def test_it_reports_the_newest_published_tag(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    _with_release_response(srv, monkeypatch)
    result = json.loads(srv.latest_release())
    assert result["ok"] is True
    assert result["latest"]["tag"] == "v0.8.1"
    assert result["latest"]["published_at"] == "2026-08-30T20:40:35Z"


def test_the_summary_says_this_is_not_what_is_installed(tmp_path, monkeypatch):
    """The exact confusion this tool exists next to, so it is pinned.

    A bot that reports the newest tag as its own version is worse than one that
    cannot answer: the installed version is a property of the cluster and this
    number is a property of a repository, and they are routinely different.
    """
    srv = _load(tmp_path)
    _with_release_response(srv, monkeypatch)
    summary = json.loads(srv.latest_release())["summary"]
    assert "NOT what this install is running" in summary


def test_it_sends_no_credential(tmp_path, monkeypatch):
    """A connector whose whole job is one public read must not hold a token."""
    srv = _load(tmp_path)
    seen = {}
    _with_release_response(srv, monkeypatch, seen=seen)
    srv.latest_release()
    assert "authorization" not in {k.lower() for k in seen["request"].headers}


def test_it_asks_the_configured_repository(tmp_path, monkeypatch):
    srv = _load(tmp_path, release_repo="acme/widget")
    seen = {}
    _with_release_response(srv, monkeypatch, seen=seen)
    srv.latest_release()
    assert seen["request"].url.path == "/repos/acme/widget/releases/latest"


def test_the_tool_exposes_no_parameters(tmp_path):
    srv = _load(tmp_path)
    assert inspect.signature(srv.latest_release).parameters == {}


def test_it_is_annotated_as_a_read(tmp_path):
    srv = _load(tmp_path)
    assert srv.READ.readOnlyHint is True
    assert srv.READ.destructiveHint is False


def test_an_unset_repository_refuses_without_a_network_call(tmp_path, monkeypatch):
    srv = _load(tmp_path, release_repo="")
    seen = {}
    _with_release_response(srv, monkeypatch, seen=seen)
    result = json.loads(srv.latest_release())
    assert result["ok"] is False
    assert result["latest"] is None
    assert seen == {}


@pytest.mark.parametrize(
    "status,expected",
    [(404, "no published releases"), (403, "rate limits"), (500, "HTTP 500")],
)
def test_failures_explain_themselves(tmp_path, monkeypatch, status, expected):
    srv = _load(tmp_path)
    _with_release_response(srv, monkeypatch, status=status, body={})
    result = json.loads(srv.latest_release())
    assert result["ok"] is False
    assert result["latest"] is None
    assert expected in result["summary"]


def test_a_body_with_no_tag_is_reported_not_guessed(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    _with_release_response(srv, monkeypatch, body={"name": "no tag here"})
    result = json.loads(srv.latest_release())
    assert result["ok"] is False
    assert "no tag_name" in result["summary"]


# --- upgrade_platform -----------------------------------------------------


def test_it_starts_the_platform_template_not_the_bundle_one(tmp_path, monkeypatch):
    """The two verbs must not be able to run each other's template."""
    srv = _load(tmp_path)
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen))
    result = json.loads(srv.upgrade_platform())
    assert result["ok"] is True
    assert seen["cronjob_path"].endswith("/cronjobs/platform-upgrade")
    assert seen["post_body"]["metadata"]["generateName"].startswith("platform-upgrade")


def test_an_unset_platform_cronjob_refuses(tmp_path, monkeypatch):
    """An install that has not opted in does not get a platform upgrade."""
    srv = _load(tmp_path, platform_cronjob="")
    seen = {}
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen))
    result = json.loads(srv.upgrade_platform())
    assert result["ok"] is False
    assert "PLATFORM_UPGRADE_CRONJOB" in result["summary"]
    assert seen == {}


def test_the_two_verbs_do_not_block_each_other(tmp_path, monkeypatch):
    """A running bundle redeploy must not refuse a platform upgrade.

    They run different templates against different credentials and are
    independent. The concurrency guard is scoped to the CronJob being started,
    so a Job labelled for one does not look active to the other -- if that
    scoping regresses, one in-flight upgrade silently blocks the unrelated verb.
    """
    srv = _load(tmp_path)
    running_self = {
        "items": [
            {
                "metadata": {
                    "name": "in-flight",
                    "labels": {"curie.dev/self-upgrade-of": "sre-bot-self-upgrade"},
                },
                "status": {"active": 1},
            }
        ]
    }
    seen = {}
    # The fake echoes back whatever the selector asked for; a real API server
    # filters. Assert on the selector the tool SENT, which is what the server
    # would have filtered on.
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient(seen, jobs=(200, running_self)))
    srv.upgrade_platform()
    assert seen["jobs_params"]["labelSelector"] == (
        "curie.dev/self-upgrade-of=platform-upgrade"
    )


def test_it_is_annotated_as_a_destructive_write(tmp_path):
    srv = _load(tmp_path)
    assert srv.UPGRADE.readOnlyHint is False
    assert srv.UPGRADE.destructiveHint is True


def test_the_platform_tool_exposes_no_parameters(tmp_path):
    srv = _load(tmp_path)
    assert inspect.signature(srv.upgrade_platform).parameters == {}


def test_its_summary_refuses_to_imply_a_rollback(tmp_path, monkeypatch):
    srv = _load(tmp_path)
    monkeypatch.setattr(srv, "_client", lambda: _FakeClient({}))
    result = json.loads(srv.upgrade_platform())
    assert result["prior"] is None
    assert "no undo" in result["summary"]
