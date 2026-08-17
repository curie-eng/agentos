"""Regression tests for WorkerConfig env-source resolution.

``populate_by_name=True`` lets tests construct the config with field-name
kwargs, but it must NOT make the env source read the bare uppercased field name
as a fallback for a field that carries a ``validation_alias``. An aliased field
must read only its ``CURIE_*`` alias; a stray generic env var (``API_KEY``,
``CREDENTIALS``, ...) in the pod env must be ignored, as it was before the
BaseSettings refactor.
"""

from __future__ import annotations

import os
import socket

import pytest
from curie_worker.config import WorkerConfig


def _clear_all_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every env var the config could read, for a clean-env baseline.

    ``BaseSettings`` reads the process environment for every field (aliased
    fields via their ``validation_alias``, the rest via the uppercased field
    name). The kernel suite runs against real Valkey/Postgres, so vars like
    ``VALKEY_HOST``/``DATABASE_URL`` may be set in the ambient env; strip them
    all so the defaults assertions below see only the code defaults.
    """
    for name, field in WorkerConfig.model_fields.items():
        alias = field.validation_alias
        key = alias if isinstance(alias, str) else name.upper()
        monkeypatch.delenv(key, raising=False)


# Every env var the OLD hand-rolled ``WorkerConfig.from_env`` read (on
# ``origin/main``), paired with a distinct sentinel and the value the field
# should hold after coercion. This is the parity oracle: the names are the exact
# old ones, so the override test proves no name drifted and no var was dropped.
_WORKER_OVERRIDES: dict[str, tuple[str, str, object]] = {
    # env var name -> (field name, raw env value, expected coerced value)
    "VALKEY_HOST": ("valkey_host", "valkey.host.example", "valkey.host.example"),
    "VALKEY_PORT": ("valkey_port", "6380", 6380),
    "VALKEY_PASSWORD": ("valkey_password", "vk-pass", "vk-pass"),
    "VALKEY_DB": ("valkey_db", "7", 7),
    "SLACK_BOT_TOKEN": ("slack_bot_token", "xoxb-sentinel", "xoxb-sentinel"),
    "SLACK_API_BASE_URL": (
        "slack_api_base_url",
        "http://slack.stub:9",
        "http://slack.stub:9",
    ),
    "DATABASE_URL": (
        "database_url",
        "postgresql+asyncpg://u:p@db:5432/x",
        "postgresql+asyncpg://u:p@db:5432/x",
    ),
    "DB_SCHEMA": ("db_schema", "myschema", "myschema"),
    "CURIE_PLUGIN_DIR": ("bundle_plugin_dir", "/custom/bundles", "/custom/bundles"),
    "CURIE_FAKE_MODEL": ("fake_model", "true", True),
    # Deliberately the NON-default value: shimmer now defaults to True, so a
    # truthy-token case would pass even if the alias were never read at all.
    "CURIE_SHIMMER": ("shimmer", "no", False),
    "CURIE_CREDENTIALS": ("credentials", "cred-sentinel", "cred-sentinel"),
    "CURIE_MODEL_BASE_URL": (
        "model_base_url",
        "http://model.local:1",
        "http://model.local:1",
    ),
    "CURIE_MODEL": ("model", "claude-sentinel", "claude-sentinel"),
    "CURIE_EVAL_STREAM": ("eval_stream", "sentinel:evals", "sentinel:evals"),
    "CURIE_EVAL_CONSUMER_GROUP": (
        "eval_consumer_group",
        "sentinel-eval-workers",
        "sentinel-eval-workers",
    ),
    "S3_ENDPOINT_URL": ("s3_endpoint_url", "http://s3.local:2", "http://s3.local:2"),
    "S3_ACCESS_KEY": ("s3_access_key", "ak-sentinel", "ak-sentinel"),
    "S3_SECRET_KEY": ("s3_secret_key", "sk-sentinel", "sk-sentinel"),
    "S3_REGION": ("s3_region", "eu-west-9", "eu-west-9"),
    "BUNDLE_BUCKET": ("bundle_bucket", "sentinel-bundles", "sentinel-bundles"),
    "CURIE_API_URL": (
        "api_base_url",
        "http://api.local:3",
        "http://api.local:3",
    ),
    "CURIE_API_KEY": ("api_key", "key-sentinel", "key-sentinel"),
    "LANGFUSE_HOST": ("langfuse_host", "http://lf.local:4", "http://lf.local:4"),
    "LANGFUSE_PUBLIC_KEY": ("langfuse_public_key", "pk-sentinel", "pk-sentinel"),
    "LANGFUSE_SECRET_KEY": ("langfuse_secret_key", "sk-lf-sentinel", "sk-lf-sentinel"),
    "CURIE_STREAM": ("stream", "sentinel:runs", "sentinel:runs"),
    "CURIE_CONSUMER_GROUP": (
        "consumer_group",
        "sentinel-workers",
        "sentinel-workers",
    ),
    "CURIE_CONSUMER_NAME": (
        "consumer_name",
        "sentinel-consumer",
        "sentinel-consumer",
    ),
    "CURIE_MAX_ATTEMPTS": ("max_attempts", "9", 9),
}


def test_aliased_field_ignores_bare_field_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray bare-name env var must not leak into an aliased field."""
    monkeypatch.setenv("API_KEY", "stray")
    monkeypatch.setenv("CREDENTIALS", "stray-creds")

    config = WorkerConfig()

    assert config.api_key == "curie-dev-key"  # the default, not "stray"
    assert config.credentials == ""  # the default, not "stray-creds"


def test_aliased_field_reads_its_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """The intended CURIE_* alias is still read from the env."""
    monkeypatch.setenv("CURIE_API_KEY", "intended")
    monkeypatch.setenv("CURIE_CREDENTIALS", "intended-creds")

    config = WorkerConfig()

    assert config.api_key == "intended"
    assert config.credentials == "intended-creds"


def test_alias_wins_over_bare_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """With both set, only the alias is read and the bare name is ignored."""
    monkeypatch.setenv("API_KEY", "stray")
    monkeypatch.setenv("CURIE_API_KEY", "intended")

    assert WorkerConfig().api_key == "intended"


def test_api_url_accepts_the_deprecated_base_url_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#496: the platform API base URL is canonically CURIE_API_URL, but the
    historical CURIE_API_BASE_URL still resolves for one release, and the
    canonical name wins when both are set."""
    monkeypatch.setenv("CURIE_API_BASE_URL", "http://deprecated:8000")
    assert WorkerConfig().api_base_url == "http://deprecated:8000"

    monkeypatch.setenv("CURIE_API_URL", "http://canonical:8000")
    assert WorkerConfig().api_base_url == "http://canonical:8000"


def test_field_name_kwargs_still_populate() -> None:
    """populate_by_name construction (used by tests) is unchanged."""
    config = WorkerConfig(fake_model=True, api_key="x", credentials="c")

    assert config.fake_model is True
    assert config.api_key == "x"
    assert config.credentials == "c"


def test_non_aliased_field_still_reads_plain_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fields without an alias keep reading their uppercased field name."""
    monkeypatch.setenv("VALKEY_HOST", "valkey.internal")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/curie"
    )

    config = WorkerConfig()

    assert config.valkey_host == "valkey.internal"
    assert config.database_url == "postgresql+asyncpg://u:p@db:5432/curie"


# --- Env-var parity vs the pre-pydantic from_env (review #178) ---------------


def test_defaults_parity_with_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean env: every field must equal the exact default the old from_env produced.

    Comprehensive check -- every field is enumerated. Config drift on a default
    is a silent prod break, so this locks each default to the value the
    hand-rolled ``WorkerConfig.from_env`` (on ``origin/main``) resolved to.
    """
    _clear_all_config_env(monkeypatch)

    config = WorkerConfig()

    # Valkey
    assert config.valkey_host == "localhost"
    assert config.valkey_port == 6379
    assert config.valkey_password == ""
    assert config.valkey_db == 0
    # Slack
    assert config.slack_bot_token == ""
    assert config.slack_api_base_url == ""
    # Postgres
    assert (
        config.database_url
        == "postgresql+asyncpg://postgres:postgres@localhost:25432/postgres"
    )
    assert config.db_schema == "curie"
    # Deployment-to-runtime binding
    assert config.bundle_plugin_dir == "/bundles/current"
    assert config.default_max_usd_per_day == 10.0
    assert config.default_max_output_tokens_per_run == 100000
    # Runner model + credentials
    assert config.fake_model is False
    assert config.credentials == ""
    assert config.model_base_url == ""
    assert config.model == ""
    # Shimmer: on by default so a reasoning model's pre-token silence is not
    # indistinguishable from a wedge (#1182). Must agree with the dispatcher's
    # default -- one env name drives both services.
    assert config.shimmer is True
    # Stream / consumer group
    assert config.stream == "curie:runs"
    assert config.consumer_group == "curie-workers"
    # Read loop
    assert config.read_count == 16
    assert config.read_block_ms == 5000
    # Per-thread lock
    assert config.lock_ttl_ms == 120000
    assert config.lock_acquire_timeout_s == 45.0
    assert config.lock_poll_interval_s == 0.02
    # Retry
    assert config.max_attempts == 3
    assert config.retry_backoff_base_s == 1.0
    assert config.retry_backoff_max_s == 20.0
    # Markers
    assert config.idempotency_ttl_s == 86400
    # Crash recovery
    assert config.reclaim_min_idle_ms == 900000
    assert config.reclaim_interval_s == 30.0
    # Slack edit throttle
    assert config.slack_edit_min_interval_s == 0.7
    # Runner HTTP timeouts
    assert config.runner_connect_timeout_s == 10.0
    assert config.runner_total_timeout_s == 600.0
    # Eval stream
    assert config.eval_stream == "curie:evals"
    assert config.eval_consumer_group == "curie-eval-workers"
    # RustFS / S3
    assert config.s3_endpoint_url == "http://localhost:29000"
    # Empty by default (#1559): a baked-in dev key is still an explicit credential
    # to boto3, so it shadows the ambient cloud identity (IRSA, instance role) the
    # key-free BYO object-store path relies on. Do not restore the RustFS dev pair
    # here; compose supplies it via env, and the S3_ACCESS_KEY/S3_SECRET_KEY
    # override entries above stay the guard that an operator value still wins.
    assert config.s3_access_key == ""
    assert config.s3_secret_key == ""
    assert config.s3_region == "us-east-1"
    assert config.bundle_bucket == "curie-bundles"
    # Platform API
    assert config.api_base_url == "http://localhost:8000"
    assert config.api_key == "curie-dev-key"
    assert config.report_max_attempts == 3
    assert config.report_backoff_base_s == 0.5
    # Langfuse
    assert config.langfuse_host == "http://localhost:23000"
    assert config.langfuse_public_key == "pk-lf-curie-dev"
    assert config.langfuse_secret_key == "sk-lf-curie-dev"
    # Key prefix
    assert config.key_prefix == "curie:worker"

    # Factory-defaulted names have no static default: the old from_env produced
    # ``f"{hostname}-{pid}"`` via ``_default_consumer_name``. Assert that shape.
    expected_consumer = f"{socket.gethostname()}-{os.getpid()}"
    assert config.consumer_name == expected_consumer
    assert config.eval_consumer_name == expected_consumer


def test_overrides_parity_with_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every env var the old from_env read, set to a sentinel under its EXACT old
    name, must be read into the right field with the right coercion.

    Proves no env-var name drifted in the BaseSettings port and no read was
    dropped.
    """
    _clear_all_config_env(monkeypatch)
    for env_var, (_field, raw, _expected) in _WORKER_OVERRIDES.items():
        monkeypatch.setenv(env_var, raw)

    config = WorkerConfig()

    for env_var, (field, _raw, expected) in _WORKER_OVERRIDES.items():
        actual = getattr(config, field)
        assert actual == expected, f"{env_var} -> {field}: {actual!r} != {expected!r}"
        # Coercion parity: ints/bools must be the coerced type, not a raw str.
        assert type(actual) is type(expected), (
            f"{env_var} -> {field}: type {type(actual)} != {type(expected)}"
        )


# --- Operator-scoped model wire declaration (#514) ---------------------------
#
# Two new fields mirroring model_base_url: they read only their CURIE_* alias
# and default to "" (not declared). They are deliberately absent from the
# _WORKER_OVERRIDES parity oracle above -- that dict pins the vars the old
# hand-rolled from_env read, and these are new, not a port of anything.


def test_model_api_backend_and_env_key_default_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undeclared is the default, so the producer emits nothing and the runner
    keeps its own pre-#514 defaults."""
    _clear_all_config_env(monkeypatch)

    config = WorkerConfig()

    assert config.model_api_backend == ""
    assert config.model_env_key == ""


def test_worker_config_reads_model_api_backend_and_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_MODEL_API_BACKEND", "messages")
    monkeypatch.setenv("CURIE_MODEL_ENV_KEY", '["ANTHROPIC_AUTH_TOKEN"]')

    config = WorkerConfig()

    assert config.model_api_backend == "messages"
    assert config.model_env_key == '["ANTHROPIC_AUTH_TOKEN"]'


def test_model_api_backend_and_env_key_ignore_bare_field_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aliased like every other CURIE_* knob: a stray bare-name env var in the
    pod env must not leak in."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("MODEL_API_BACKEND", "chat_completions")
    monkeypatch.setenv("MODEL_ENV_KEY", "STRAY_NAME")

    config = WorkerConfig()

    assert config.model_api_backend == ""
    assert config.model_env_key == ""


def test_model_api_backend_and_env_key_populate_by_field_name() -> None:
    """populate_by_name construction (used by the binding tests) works."""
    config = WorkerConfig(model_api_backend="messages", model_env_key="MY_PROVIDER_KEY")

    assert config.model_api_backend == "messages"
    assert config.model_env_key == "MY_PROVIDER_KEY"


# --- Runner-facing API base (#678) -------------------------------------------
#
# A field distinct from api_base_url (the worker's self-dial URL): the API base a
# SPAWNED RUNNER dials. Defaults to "" (undivided) and reads only its CURIE_*
# alias. Kept out of the _WORKER_OVERRIDES parity oracle -- like the #514 fields,
# it is new, not a port of the old hand-rolled from_env.


def test_runner_api_base_url_defaults_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undivided is the default: the runner reaches the API at the worker's own
    self-dial URL (k8s in-cluster, single-host local)."""
    _clear_all_config_env(monkeypatch)

    config = WorkerConfig()

    assert config.runner_api_base_url == ""


def test_worker_config_reads_runner_api_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_RUNNER_API_URL", "http://curie-api:8000")

    config = WorkerConfig()

    assert config.runner_api_base_url == "http://curie-api:8000"


def test_runner_api_base_url_ignores_bare_field_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aliased like every other CURIE_* knob: a stray bare-name env var in the
    pod env must not leak in."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("RUNNER_API_BASE_URL", "http://stray:9000")

    config = WorkerConfig()

    assert config.runner_api_base_url == ""


def test_runner_facing_api_base_url_falls_back_to_self_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset runner_api_base_url resolves to api_base_url, so k8s and single-host
    local -- where the runner reaches the API at the worker's URL -- are unchanged."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_API_URL", "http://in-cluster-api:8000")

    config = WorkerConfig()

    assert config.runner_api_base_url == ""
    assert config.runner_facing_api_base_url == "http://in-cluster-api:8000"


def test_runner_facing_api_base_url_prefers_the_runner_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the two networks diverge (docker substrate), the runner-facing base
    wins over the worker's localhost self-dial URL."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_API_URL", "http://localhost:28000")
    monkeypatch.setenv("CURIE_RUNNER_API_URL", "http://curie-api:8000")

    config = WorkerConfig()

    assert config.api_base_url == "http://localhost:28000"
    assert config.runner_facing_api_base_url == "http://curie-api:8000"


def test_curie_dead_letter_stream_reaches_the_dead_letter_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CURIE_DEAD_LETTER_STREAM (#505/#668) populates dead_letter_stream and
    is reflected by dead_letter_stream_name(), the graveyard the API's
    dead-letter watcher must agree with (see apps/api/tests/test_config_parity.py)."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_STREAM", "operations")
    monkeypatch.setenv("CURIE_DEAD_LETTER_STREAM", "operations:dead")

    config = WorkerConfig()

    assert config.dead_letter_stream == "operations:dead"
    assert config.dead_letter_stream_name() == "operations:dead"


# --- Per-service bool divergence (review #178) -------------------------------
#
# The old worker ``_b`` accepted only ("1", "true", "yes") as truthy -- notably
# NOT "on", unlike the dispatcher's ``_set_bool``. These lock that divergence.


@pytest.mark.parametrize("token", ["1", "true", "yes", "TRUE", "Yes", " yes "])
def test_bool_shared_truthy_tokens(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """The truthy set shared with the dispatcher parses to True (case/space-insensitive)."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_SHIMMER", token)
    monkeypatch.setenv("CURIE_FAKE_MODEL", token)

    config = WorkerConfig()

    assert config.shimmer is True
    assert config.fake_model is True


@pytest.mark.parametrize("token", ["on", "ON", "0", "no", "off", "", "maybe"])
def test_bool_worker_rejects_on_and_falsy_tokens(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """The worker does NOT treat "on" as truthy (the dispatcher does); falsy tokens are False."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_SHIMMER", token)
    monkeypatch.setenv("CURIE_FAKE_MODEL", token)

    config = WorkerConfig()

    assert config.shimmer is False
    assert config.fake_model is False


# --- Eval claim-creation concurrency bound (#709) ----------------------------
#
# A ceiling on eval SandboxClaims created/bound concurrently, so a single-node
# cluster is not flooded. Defaults to 1 (sequential-with-backpressure) and reads
# only its CURIE_* alias; kept out of the _WORKER_OVERRIDES parity oracle since
# it is new, not a port of the old hand-rolled from_env.


def test_eval_max_concurrent_claims_defaults_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-node-safe by default: claims are created one at a time."""
    _clear_all_config_env(monkeypatch)

    config = WorkerConfig()

    assert config.eval_max_concurrent_claims == 1


def test_worker_config_reads_eval_max_concurrent_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_EVAL_MAX_CONCURRENT_CLAIMS", "4")

    config = WorkerConfig()

    assert config.eval_max_concurrent_claims == 4


def test_eval_max_concurrent_claims_ignores_bare_field_name_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aliased like every other CURIE_* knob: a stray bare-name env var must not
    leak in."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("EVAL_MAX_CONCURRENT_CLAIMS", "7")

    config = WorkerConfig()

    assert config.eval_max_concurrent_claims == 1


def test_eval_max_concurrent_claims_rejects_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Floor of 1: a bound of 0 would create no claims at all (no eval could run)."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_EVAL_MAX_CONCURRENT_CLAIMS", "0")

    with pytest.raises(ValueError):
        WorkerConfig()


# --- Raw-string ingestion of complex-typed fields -----------------------------
#
# ``slack_trusted_origins`` (tuple) and ``adapter_credentials`` (dict) are
# "complex" types, which pydantic-settings JSON-decodes INSIDE the env source,
# BEFORE any field validator runs. Their BeforeValidators accept a bare
# comma-list and a blank string respectively, but those never got the chance:
# the source raised ``SettingsError`` first and the worker died at boot on the
# exact value compose.dev.yaml exports. ``NoDecode`` on the annotated type
# suppresses that decode so the raw env string reaches the validator.
#
# These MUST go through the real settings source (env, not kwargs) -- kwarg
# construction bypasses the env source entirely and passed all along.


def test_trusted_origins_parsed_from_a_bare_comma_list_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compose.dev.yaml value: a comma list, not JSON."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv(
        "CURIE_SLACK_TRUSTED_ORIGINS",
        "http://localhost,http://127.0.0.1,http://host.docker.internal",
    )

    config = WorkerConfig()

    assert config.slack_trusted_origins == (
        "http://localhost",
        "http://127.0.0.1",
        "http://host.docker.internal",
    )


def test_trusted_origins_tolerates_whitespace_around_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv(
        "CURIE_SLACK_TRUSTED_ORIGINS", " http://localhost:8080 , , http://a.b "
    )

    config = WorkerConfig()

    assert config.slack_trusted_origins == ("http://localhost:8080", "http://a.b")


def test_trusted_origins_empty_env_is_the_closed_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank means "no extra trusted origins" -- fail closed, never a boot crash."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_SLACK_TRUSTED_ORIGINS", "")

    config = WorkerConfig()

    assert config.slack_trusted_origins == ()


def test_adapter_credentials_empty_env_is_an_empty_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same defect class: a blank ``CURIE_ADAPTER_CREDENTIALS`` is "none
    configured" (every non-Slack egress then fails closed), not a boot crash."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_ADAPTER_CREDENTIALS", "")

    config = WorkerConfig()

    assert config.adapter_credentials == {}


def test_adapter_credentials_still_parsed_from_json_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_ADAPTER_CREDENTIALS", '{"acme": "s3cret"}')

    config = WorkerConfig()

    assert config.adapter_credentials == {"acme": "s3cret"}


def test_adapter_credentials_malformed_json_is_a_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing closed on garbage is deliberate; it must stay a failure."""
    _clear_all_config_env(monkeypatch)
    monkeypatch.setenv("CURIE_ADAPTER_CREDENTIALS", "not-json")

    with pytest.raises(ValueError):
        WorkerConfig()
