"""Typed configuration for the dispatcher, read from the process environment.

The dispatcher needs two Slack tokens (an app-level token for Socket Mode and a
bot token for Web API calls), a Valkey connection, and the stream/dedupe/backoff
knobs. ``DispatcherConfig`` is a ``pydantic_settings.BaseSettings`` (the house
pattern, see ``apps/api``): construct it with no arguments and it reads the
environment on init, falling back to the defaults below for anything absent.

Env mapping:
    SLACK_APP_TOKEN            -> slack_app_token   (xapp-..., Socket Mode)
    SLACK_BOT_TOKEN            -> slack_bot_token   (xoxb-..., Web API)
    SLACK_SIGNING_SECRET       -> slack_signing_secret (optional; unused in
                                  Socket Mode, kept for Bolt App construction)
    VALKEY_HOST                -> valkey_host
    VALKEY_PORT                -> valkey_port
    VALKEY_PASSWORD            -> valkey_password
    VALKEY_DB                  -> valkey_db
    CURIE_STREAM             -> stream
    CURIE_DEDUPE_PREFIX      -> dedupe_prefix
    CURIE_DEDUPE_TTL_SECONDS -> dedupe_ttl_seconds
    CURIE_PLACEHOLDER_TEXT   -> placeholder_text
    CURIE_STATUS_TEXT        -> status_text (the shimmer caption, NOT the message)
    CURIE_SHIMMER            -> shimmer (assistant-thread status while working)
    CURIE_BACKOFF_INITIAL_SECONDS -> backoff_initial_seconds
    CURIE_BACKOFF_MAX_SECONDS     -> backoff_max_seconds
    CURIE_BACKOFF_MULTIPLIER      -> backoff_multiplier
    CURIE_API_URL            -> api_base_url  (CURIE_API_BASE_URL: deprecated alias)
    CURIE_API_KEY            -> api_key
    CURIE_API_PREFLIGHT_TIMEOUT_SECONDS -> api_preflight_timeout_s
    CURIE_HEARTBEAT_FILE             -> heartbeat_file
    CURIE_HEARTBEAT_INTERVAL_SECONDS -> heartbeat_interval_s
"""

from typing import Annotated

from aci_protocol.service_config import (
    API_KEY_ENV,
    HEARTBEAT_FILE_ENV,
    HEARTBEAT_INTERVAL_ENV,
    RUNS_STREAM_DEFAULT,
    SHIMMER_ENV,
    STREAM_ENV,
    AliasOnlyEnvSource,
    api_url_validation_alias,
    warn_if_deprecated_api_url_env,
)
from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import (
    PydanticBaseSettingsSource,
)


def _parse_bool(value: object) -> bool:
    """Parse the truthy env-string set the dispatcher has always accepted.

    A real bool passes through (so kwarg construction in tests is unchanged); any
    other string is truthy only when it is one of the accepted tokens, matching
    the previous hand-rolled ``_set_bool``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


Bool = Annotated[bool, BeforeValidator(_parse_bool)]


class DispatcherConfig(BaseSettings):
    """Everything the dispatcher needs to run, in one typed object."""

    model_config = SettingsConfigDict(
        frozen=True, populate_by_name=True, extra="ignore"
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Swap the env source so aliased fields read only their alias."""
        # Surface the CURIE_API_BASE_URL -> CURIE_API_URL rename (#496).
        warn_if_deprecated_api_url_env()
        return (
            init_settings,
            AliasOnlyEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    slack_app_token: str = ""
    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    valkey_host: str = "localhost"
    valkey_port: int = 6379
    valkey_password: str = ""
    valkey_db: int = 0

    stream: str = Field(default=RUNS_STREAM_DEFAULT, validation_alias=STREAM_ENV)
    dedupe_prefix: str = Field(
        default="curie:dedupe:", validation_alias="CURIE_DEDUPE_PREFIX"
    )
    dedupe_ttl_seconds: int = Field(
        default=3600, validation_alias="CURIE_DEDUPE_TTL_SECONDS"
    )

    # Platform API for the approval click-to-resolve flow (#246). Defaults match
    # the API's dev stack, mirroring the worker's settings for the same seam.
    api_base_url: str = Field(
        default="http://localhost:8000", validation_alias=api_url_validation_alias()
    )
    api_key: str = Field(default="curie-dev-key", validation_alias=API_KEY_ENV)
    # Deadline for the boot-time gate on that wiring (see preflight.py). Long
    # enough to absorb the API's own startup. Must be positive: the gate is the
    # AC2 requirement, so a non-positive value is a config error at boot rather
    # than a silent way to turn it off. Non-finite is rejected for the same
    # reason: `inf` passes `gt=0` but hangs the boot gate forever, so the pod
    # never exits, never crash-loops, and the operator never gets the signal the
    # gate exists to produce.
    api_preflight_timeout_s: float = Field(
        default=30.0,
        gt=0,
        allow_inf_nan=False,
        validation_alias="CURIE_API_PREFLIGHT_TIMEOUT_SECONDS",
    )

    placeholder_text: str = Field(
        default="On it. Working on your request.",
        validation_alias="CURIE_PLACEHOLDER_TEXT",
    )
    # The shimmer caption, kept SEPARATE from placeholder_text because the two
    # surfaces have different grammar. Slack renders an assistant-thread status
    # as "<App Name> <status>" and inserts the app name itself, so the status has
    # to read as a CONTINUATION of the app name ("Curie is working on your
    # request...") while the message body is a standalone sentence ("On it.
    # Working on your request."). Reusing one string for both produced "Curie On
    # it. Working on your request." in the shimmer.
    # https://docs.slack.dev/reference/methods/assistant.threads.setStatus
    status_text: str = Field(
        default="is working on your request...",
        validation_alias="CURIE_STATUS_TEXT",
    )
    # When true, also set a Slack assistant-thread status (the native "shimmer"
    # on the app name) to status_text while a turn runs. The worker clears it
    # when the turn ends.
    #
    # ON by default: during a turn the placeholder only changes when the model
    # emits text, so a model that spends 30s reasoning before its first token
    # leaves the thread visibly frozen, and an operator cannot tell that from a
    # wedge (issue #1182). The shimmer is Slack's own affordance for exactly
    # that, and it is strictly additive -- it neither edits the message nor
    # notifies anyone. A workspace whose app lacks the "Agents & AI Apps"
    # feature just skips it (the call is best-effort and logs at debug), so
    # defaulting it on cannot break a deployment; see slack-app-manifest.yaml
    # for the one-click enablement that has no manifest key.
    shimmer: Bool = Field(default=True, validation_alias=SHIMMER_ENV)

    backoff_initial_seconds: float = Field(
        default=1.0, gt=0, validation_alias="CURIE_BACKOFF_INITIAL_SECONDS"
    )
    backoff_max_seconds: float = Field(
        default=30.0, gt=0, validation_alias="CURIE_BACKOFF_MAX_SECONDS"
    )
    backoff_multiplier: float = Field(
        default=2.0, gt=1, validation_alias="CURIE_BACKOFF_MULTIPLIER"
    )

    # A daemon thread touches heartbeat_file every heartbeat_interval_s so an exec
    # liveness probe can restart the pod if the process fully wedges.
    heartbeat_file: str = Field(
        default="/tmp/curie-dispatcher.heartbeat",
        validation_alias=HEARTBEAT_FILE_ENV,
    )
    heartbeat_interval_s: float = Field(
        default=10.0, validation_alias=HEARTBEAT_INTERVAL_ENV
    )

    def dedupe_key(self, slack_event_id: str) -> str:
        """The Valkey key that guards a single Slack event id against retries."""
        return f"{self.dedupe_prefix}{slack_event_id}"
