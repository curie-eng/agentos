"""Warm-pool capability projection (#1492 D1): deterministic, secret-free, cold-honest.

The projection is the version-stable subset of the SAME boot env the cold path
renders (``BindingResolver.boot_env``), so a new boot key cannot silently drift
between a cold claim and a warm template: an unclassified key refuses the
projection rather than being guessed into or out of the hash. Credentials are
identity only -- a ``secretKeyRef`` name/key or a nonsecret generation/expiry --
and no token byte may reach the hash, ``repr`` or a log line.

Nothing here touches Kubernetes, a Secret, Postgres or Valkey.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from aci_protocol import BootEnv
from curie_worker import sandbox_token
from curie_worker.binding import (
    DECISION_ENV,
    FALSE_COMPLETION_CHECK_ENV,
    GRANT_TOOL_ENV,
    RESUMED_KIND_ENV,
    SANDBOX_TOKEN_TTL_SECONDS,
    BindingResolver,
    ResolvedDeployment,
)
from curie_worker.config import WorkerConfig
from curie_worker.sandbox.warm_pool_contract import (
    PROJECTION_THREAD_SENTINEL,
    STATE_APP_SCOPE,
    STATE_SCOPE,
    CapabilityProjection,
    ColdReason,
    CredentialGeneration,
    ProjectionError,
    SecretKeyRef,
    classify_claim,
    mint_credential_generation,
    project_boot_env,
    warm_boot_projection,
    worker_env_classes,
)
from curie_worker.workspace import WORKSPACE_REF_ENV, WORKSPACE_SHA256_ENV

PLATFORM_KEY = "unit-test-platform-key"
AGENT_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
VERSION_ID = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
DEPLOYMENT_ID = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
BASELINE_CREDENTIAL = SecretKeyRef(name="curie-runner-credentials", key="agentCredentials")
CONNECTOR_SECRET_NAME = "curie-agent-sre-connector-secrets"
GENERATION = CredentialGeneration.for_window(str(AGENT_ID), issued_at=1_800_000_000)

SESSION_ENV = BootEnv.env_key("session_id")
HISTORY_REF_ENV = BootEnv.env_key("history_ref")
RUNNER_TOKEN_ENV = BootEnv.env_key("runner_token")
HISTORY_TOKEN_ENV = BootEnv.env_key("history_token")
MEMORY_TOKEN_ENV = BootEnv.env_key("memory_token")
STATE_TOKEN_ENV = BootEnv.env_key("state_token")
STATE_URL_ENV = BootEnv.env_key("state_url")
CREDENTIALS_ENV = BootEnv.env_key("credentials_ref")
MODEL_ENV = BootEnv.env_key("model")
MEMORY_REF_ENV = BootEnv.env_key("memory_ref")
CONNECTOR_SECRET_KEYS_ENV = BootEnv.env_key("connector_secret_keys")


def _resolved(**overrides: object) -> ResolvedDeployment:
    base: dict[str, object] = {
        "agent_id": AGENT_ID,
        "agent_name": "sre",
        "deployment_id": DEPLOYMENT_ID,
        "version_id": VERSION_ID,
        "version_label": "v3",
        "bundle_ref": "bundles/sre/v3.tgz",
        "max_usd_per_day": None,
        "max_output_tokens_per_run": None,
        "model": "claude-opus-5",
        "thinking": None,
        "approval_required_tools": ["Bash"],
        "secrets": None,
        "memory": True,
    }
    base.update(overrides)
    return ResolvedDeployment.model_validate(base)


def _config(**overrides: object) -> WorkerConfig:
    base: dict[str, object] = {
        "api_key": PLATFORM_KEY,
        "credentials": "sk-live-model-credential-value",
        "connector_release": "curie",
        "connector_namespace": "curie",
        "model": "claude-sonnet-5",
    }
    base.update(overrides)
    return WorkerConfig(**base)  # type: ignore[arg-type]


def _resolver(config: WorkerConfig | None = None) -> BindingResolver:
    # boot_env never touches the engine; the projection is a pure render.
    return BindingResolver(object(), config or _config())  # type: ignore[arg-type]


def _project(
    resolved: ResolvedDeployment | None = None,
    config: WorkerConfig | None = None,
    **overrides: object,
) -> CapabilityProjection:
    cfg = config or _config()
    resolved = resolved or _resolved()
    generation = (
        CredentialGeneration.for_window(str(resolved.agent_id), issued_at=GENERATION.issued_at)
        if cfg.api_key
        else None
    )
    kwargs: dict[str, object] = {
        "bundle_sha256": "ab" * 32,
        "model_credential_ref": BASELINE_CREDENTIAL,
        "connector_secret_name": CONNECTOR_SECRET_NAME,
        "credential_generation": generation,
    }
    kwargs.update(overrides)
    return warm_boot_projection(_resolver(cfg), resolved, **kwargs)  # type: ignore[arg-type]


# --- projection derives from the real cold render -------------------------------


def test_projection_is_the_version_stable_subset_of_the_cold_boot_env() -> None:
    resolver = _resolver()
    resolved = _resolved()
    cold = resolver.boot_env(resolved, "1234.5678", kind="slack", address="C0EXAMPLE1")
    projection = _project(resolved)

    for key in (
        BootEnv.env_key("plugin_dir"),
        BootEnv.env_key("budget"),
        MEMORY_REF_ENV,
        BootEnv.env_key("bundle_ref"),
        BootEnv.env_key("bundle_version"),
        MODEL_ENV,
        BootEnv.env_key("approval_required_tools"),
        BootEnv.env_key("connector_release"),
        BootEnv.env_key("connector_agent"),
        BootEnv.env_key("connector_namespace"),
        STATE_URL_ENV,
    ):
        assert projection.env[key] == cold[key], key
    for key in (
        SESSION_ENV,
        HISTORY_REF_ENV,
        RUNNER_TOKEN_ENV,
        HISTORY_TOKEN_ENV,
        MEMORY_TOKEN_ENV,
        STATE_TOKEN_ENV,
        CREDENTIALS_ENV,
    ):
        assert key not in projection.env, key
    assert projection.credential_keys == (HISTORY_TOKEN_ENV, MEMORY_TOKEN_ENV, STATE_TOKEN_ENV)
    assert projection.secret_refs[CREDENTIALS_ENV] == BASELINE_CREDENTIAL
    assert projection.credential_generation == GENERATION
    assert PROJECTION_THREAD_SENTINEL not in projection.canonical_json()
    assert projection.bundle_sha256 == "ab" * 32
    assert projection.version_id == str(VERSION_ID)
    assert projection.deployment_id == str(DEPLOYMENT_ID)


def test_generation_is_deterministic_for_identical_inputs() -> None:
    assert _project().capability_generation == _project().capability_generation
    assert len(_project().capability_generation) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        {"model": "claude-sonnet-5"},
        {"thinking": "high"},
        {"max_usd_per_day": 3.5},
        {"max_output_tokens_per_run": 4096},
        {"approval_required_tools": ["Bash", "Write"]},
        {"agent_id": uuid.UUID("00000000-0000-4000-8000-000000000002")},
        {"bundle_ref": "bundles/sre/v4.tgz"},
        {"version_label": "v4"},
        {"version_id": uuid.UUID("00000000-0000-4000-8000-0000000000a2")},
    ],
    ids=lambda m: next(iter(m)),
)
def test_mutable_agent_or_version_change_mints_a_new_generation(mutation: dict) -> None:
    assert _project().capability_generation != _project(_resolved(**mutation)).capability_generation


def test_worker_default_model_change_mints_a_new_generation_when_agent_pins_none() -> None:
    resolved = _resolved(model=None)
    a = _project(resolved, _config(model="claude-sonnet-5"))
    b = _project(resolved, _config(model="claude-opus-5"))
    assert a.env[MODEL_ENV] == "claude-sonnet-5"
    assert a.capability_generation != b.capability_generation


def test_bundle_sha256_and_credential_window_are_part_of_the_generation() -> None:
    base = _project()
    assert base.capability_generation != _project(bundle_sha256="cd" * 32).capability_generation
    renewed = CredentialGeneration.for_window(str(AGENT_ID), issued_at=1_800_000_000 + 3600)
    assert (
        base.capability_generation != _project(credential_generation=renewed).capability_generation
    )


def test_memory_ref_is_included_and_omitting_it_is_refused() -> None:
    projection = _project()
    assert projection.env[MEMORY_REF_ENV].endswith(f"/agents/{AGENT_ID}/state/memory")
    env = dict(_resolver().boot_env(_resolved(), PROJECTION_THREAD_SENTINEL))
    env.pop(MEMORY_REF_ENV)
    with pytest.raises(ProjectionError, match="memory_ref"):
        project_boot_env(env, **_identity(), credential_generation=GENERATION)


# --- per-conversation and credential material never reach the hash --------------


def _identity(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "agent_id": str(AGENT_ID),
        "version_id": str(VERSION_ID),
        "deployment_id": str(DEPLOYMENT_ID),
        "version_label": "v3",
        "bundle_ref": "bundles/sre/v3.tgz",
        "bundle_sha256": "ab" * 32,
        "memory": True,
        "model_credential_ref": BASELINE_CREDENTIAL,
        "connector_secret_name": CONNECTOR_SECRET_NAME,
    }
    base.update(overrides)
    return base


def _cold_env(**overrides: str) -> dict[str, str]:
    env = dict(_resolver().boot_env(_resolved(), "thread-a", kind="slack", address="C0EXAMPLE1"))
    env.update(overrides)
    return env


def test_per_conversation_inputs_do_not_change_the_generation() -> None:
    a = project_boot_env(_cold_env(), **_identity(), credential_generation=GENERATION)
    b = project_boot_env(
        _cold_env(
            **{
                SESSION_ENV: "agent-x-thread-other",
                HISTORY_REF_ENV: "http://api/agents/x/state/transcript/other",
                RUNNER_TOKEN_ENV: "another-per-claim-token",
                HISTORY_TOKEN_ENV: "sbx.other.sig",
                MEMORY_TOKEN_ENV: "sbx.other.sig",
                STATE_TOKEN_ENV: "sbx.other2.sig",
            }
        ),
        **_identity(),
        credential_generation=GENERATION,
    )
    assert a.capability_generation == b.capability_generation


def test_token_and_credential_values_never_reach_json_repr_str_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    material = {
        RUNNER_TOKEN_ENV: "RUNNER-MATERIAL-7f3a",
        HISTORY_TOKEN_ENV: "HISTORY-MATERIAL-7f3a",
        MEMORY_TOKEN_ENV: "MEMORY-MATERIAL-7f3a",
        STATE_TOKEN_ENV: "STATE-MATERIAL-7f3a",
        CREDENTIALS_ENV: "CREDENTIAL-MATERIAL-7f3a",
        "GH_TOKEN": "CONNECTOR-MATERIAL-7f3a",
        CONNECTOR_SECRET_KEYS_ENV: "GH_TOKEN",
    }
    projection = project_boot_env(
        _cold_env(**material), **_identity(), credential_generation=GENERATION
    )
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").info("projection %s %r", projection, projection)
    rendered = "\n".join(
        [projection.canonical_json(), repr(projection), str(projection), caplog.text]
    )
    for key, value in material.items():
        if key == CONNECTOR_SECRET_KEYS_ENV:
            continue
        assert value not in rendered, key
    assert projection.env[CONNECTOR_SECRET_KEYS_ENV] == "GH_TOKEN"
    assert "GH_TOKEN" not in projection.env
    assert projection.secret_refs["GH_TOKEN"] == SecretKeyRef(
        name=CONNECTOR_SECRET_NAME, key="GH_TOKEN"
    )


def test_a_credential_value_leaking_through_another_key_is_refused() -> None:
    env = _cold_env(**{CREDENTIALS_ENV: "LEAK-ME", "CURIE_MODEL": "LEAK-ME"})
    with pytest.raises(ProjectionError, match="credential value"):
        project_boot_env(env, **_identity(), credential_generation=GENERATION)


def test_unclassified_boot_key_refuses_the_projection() -> None:
    env = _cold_env(CURIE_FUTURE_KNOB="1")
    with pytest.raises(ProjectionError, match="CURIE_FUTURE_KNOB"):
        project_boot_env(env, **_identity(), credential_generation=GENERATION)


def test_every_worker_boot_key_is_classified() -> None:
    classes = worker_env_classes()
    expected = set(BootEnv.env_keys("worker")) | {
        GRANT_TOOL_ENV,
        RESUMED_KIND_ENV,
        DECISION_ENV,
        FALSE_COMPLETION_CHECK_ENV,
        WORKSPACE_REF_ENV,
        WORKSPACE_SHA256_ENV,
    }
    assert expected <= set(classes)
    assert BootEnv.env_key("runner_bootstrap_token") not in classes  # substrate-only, never worker


def test_model_credential_without_a_secret_ref_is_refused() -> None:
    with pytest.raises(ProjectionError, match=CREDENTIALS_ENV):
        project_boot_env(
            _cold_env(),
            **_identity(model_credential_ref=None),
            credential_generation=GENERATION,
        )


def test_fake_model_install_without_credential_needs_no_secret_ref() -> None:
    projection = _project(
        config=_config(credentials="", fake_model=True), model_credential_ref=None
    )
    assert CREDENTIALS_ENV not in projection.secret_refs
    assert projection.env[BootEnv.env_key("fake_model")] == "1"


def test_connector_secret_without_a_secret_name_is_refused() -> None:
    resolved = _resolved(secrets={"GH_TOKEN": "value"})
    with pytest.raises(ProjectionError, match="GH_TOKEN"):
        _project(resolved, connector_secret_name=None)


def test_state_tokens_and_credential_generation_must_agree() -> None:
    with pytest.raises(ProjectionError, match="credential generation"):
        _project(credential_generation=None)
    with pytest.raises(ProjectionError, match="credential generation"):
        _project(config=_config(api_key=""), credential_generation=GENERATION)
    no_key = _project(config=_config(api_key=""), credential_generation=None)
    assert no_key.credential_keys == ()
    assert no_key.credential_generation is None


def test_generation_for_another_agent_is_refused() -> None:
    other = CredentialGeneration.for_window("not-this-agent", issued_at=1_800_000_000)
    with pytest.raises(ProjectionError, match="agent"):
        _project(credential_generation=other)


# --- memory=false: binding-scoped state cannot be pre-baked ------------------------


def test_memory_false_drops_the_binding_scoped_state_url_from_the_template() -> None:
    projection = _project(_resolved(memory=False))
    assert STATE_URL_ENV not in projection.env
    assert STATE_TOKEN_ENV not in projection.credential_keys
    assert projection.memory is False


def test_memory_true_keeps_only_the_agent_wide_state_url() -> None:
    projection = _project()
    assert projection.env[STATE_URL_ENV].endswith(f"/agents/{AGENT_ID}/state")
    assert STATE_TOKEN_ENV in projection.credential_keys


# --- credential generation identity and the existing codec ------------------------


def test_credential_generation_is_nonsecret_identity_only() -> None:
    gen = CredentialGeneration.for_window(str(AGENT_ID), issued_at=100)
    assert gen == CredentialGeneration.for_window(str(AGENT_ID), issued_at=100)
    assert gen.expires_at == 100 + SANDBOX_TOKEN_TTL_SECONDS
    assert gen.scopes == (STATE_SCOPE, STATE_APP_SCOPE)
    assert gen.admits(now=gen.expires_at - 1)
    assert not gen.admits(now=gen.expires_at)
    assert not gen.admits(now=gen.expires_at - 60, margin_seconds=60)
    with pytest.raises(ValueError):
        CredentialGeneration.for_window(str(AGENT_ID), issued_at=100, ttl_seconds=0)


def test_minted_material_uses_the_existing_scoped_codec_and_is_redacted() -> None:
    gen = GENERATION
    material = mint_credential_generation(PLATFORM_KEY, gen)
    projection = _project()
    assert tuple(material.keys()) == projection.credential_keys
    agent = str(AGENT_ID)
    now = gen.issued_at + 1
    assert sandbox_token.verify(
        material.value(HISTORY_TOKEN_ENV), PLATFORM_KEY, agent=agent, scope=STATE_SCOPE, now=now
    )
    assert sandbox_token.verify(
        material.value(MEMORY_TOKEN_ENV), PLATFORM_KEY, agent=agent, scope=STATE_SCOPE, now=now
    )
    assert sandbox_token.verify(
        material.value(STATE_TOKEN_ENV), PLATFORM_KEY, agent=agent, scope=STATE_APP_SCOPE, now=now
    )
    # Causal negatives: wrong agent, wrong scope, expired, wrong key.
    assert not sandbox_token.verify(
        material.value(HISTORY_TOKEN_ENV), PLATFORM_KEY, agent="other", scope=STATE_SCOPE, now=now
    )
    assert not sandbox_token.verify(
        material.value(STATE_TOKEN_ENV), PLATFORM_KEY, agent=agent, scope=STATE_SCOPE, now=now
    )
    assert not sandbox_token.verify(
        material.value(HISTORY_TOKEN_ENV),
        PLATFORM_KEY,
        agent=agent,
        scope=STATE_SCOPE,
        now=gen.expires_at,
    )
    assert not sandbox_token.verify(
        material.value(HISTORY_TOKEN_ENV), "other-key", agent=agent, scope=STATE_SCOPE, now=now
    )
    rendered = repr(material) + str(material) + json.dumps(material.identity())
    for key in material.keys():
        assert material.value(key) not in rendered
    assert material.value(HISTORY_TOKEN_ENV) != PLATFORM_KEY
    assert material.identity()["expires_at"] == gen.expires_at


def test_minting_refuses_an_empty_key() -> None:
    with pytest.raises(ValueError):
        mint_credential_generation("", GENERATION)


# --- cold eligibility --------------------------------------------------------------


def _claim_env(resolved: ResolvedDeployment | None = None, **overrides: str) -> dict[str, str]:
    env = dict(
        _resolver().boot_env(
            resolved or _resolved(), "1234.5678", kind="slack", address="C0EXAMPLE1"
        )
    )
    env.update(overrides)
    return env


def test_fresh_matching_turn_is_warm_eligible() -> None:
    projection = _project()
    verdict = classify_claim(_claim_env(), projection, now=GENERATION.issued_at + 1)
    assert verdict.warm, verdict
    assert verdict.reasons == ()


def test_generation_mismatch_is_cold_rather_than_another_pool() -> None:
    projection = _project()
    verdict = classify_claim(
        _claim_env(_resolved(model="claude-sonnet-5")), projection, now=GENERATION.issued_at + 1
    )
    assert not verdict.warm
    assert ColdReason.GENERATION_MISMATCH in verdict.reasons
    assert MODEL_ENV in verdict.mismatched_keys


def test_memory_false_binding_turn_is_cold() -> None:
    resolved = _resolved(memory=False)
    projection = _project(resolved)
    verdict = classify_claim(_claim_env(resolved), projection, now=GENERATION.issued_at + 1)
    assert not verdict.warm
    assert verdict.reasons == (ColdReason.MEMORY_FALSE_BINDING_STATE,)


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        (GRANT_TOOL_ENV, ColdReason.APPROVAL_GRANT),
        (RESUMED_KIND_ENV, ColdReason.APPROVAL_RESUMED_KIND),
        (DECISION_ENV, ColdReason.APPROVAL_DECISION),
        (WORKSPACE_REF_ENV, ColdReason.WORKSPACE_ENV),
        (WORKSPACE_SHA256_ENV, ColdReason.WORKSPACE_ENV),
    ],
)
def test_per_claim_overlay_env_is_cold(key: str, reason: ColdReason) -> None:
    verdict = classify_claim(_claim_env(**{key: "x"}), _project(), now=GENERATION.issued_at + 1)
    assert not verdict.warm
    assert verdict.reasons == (reason,)


def test_resume_eval_and_workspace_flags_are_cold() -> None:
    projection = _project()
    now = GENERATION.issued_at + 1
    assert classify_claim(_claim_env(), projection, resume=True, now=now).reasons == (
        ColdReason.RESUME_HISTORY,
    )
    assert classify_claim(_claim_env(), projection, eval_lane=True, now=now).reasons == (
        ColdReason.EVAL_LANE,
    )
    assert classify_claim(_claim_env(), projection, workspace_stage=True, now=now).reasons == (
        ColdReason.WORKSPACE_STAGE,
    )


def test_unknown_extra_claim_env_is_cold_not_guessed() -> None:
    verdict = classify_claim(
        _claim_env(CURIE_FUTURE_KNOB="1"), _project(), now=GENERATION.issued_at + 1
    )
    assert not verdict.warm
    assert verdict.reasons == (ColdReason.EXTRA_CLAIM_ENV,)
    assert verdict.mismatched_keys == ("CURIE_FUTURE_KNOB",)


def test_expired_credential_generation_is_cold_until_a_new_generation() -> None:
    projection = _project()
    verdict = classify_claim(_claim_env(), projection, now=GENERATION.expires_at)
    assert not verdict.warm
    assert verdict.reasons == (ColdReason.CREDENTIAL_GENERATION_EXPIRED,)


def test_missing_version_stable_key_on_the_claim_is_a_mismatch() -> None:
    env = _claim_env()
    env.pop(BootEnv.env_key("approval_required_tools"))
    verdict = classify_claim(env, _project(), now=GENERATION.issued_at + 1)
    assert verdict.reasons == (ColdReason.GENERATION_MISMATCH,)
