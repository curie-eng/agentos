"""G1: the Agent Sandbox substrate (claim, affinity, suspend/resume, reap).

Public surface for F1 (the worker kernel):

- ``SandboxSubstrate`` -- claim/lookup/suspend/resume/release/reap_orphans
- ``AffinityStore`` -- the Valkey ``thread_ts -> sandbox_id`` route store
- ``KubernetesSandboxClient`` / ``SandboxClient`` -- the cluster seam
- ``SandboxHandle`` -- the claimed sandbox identity + ACI dial target
"""

from .affinity import AffinityStore
from .docker import DockerError, DockerSandboxClient, RunnerHardening
from .k8s import KubernetesSandboxClient
from .substrate import HISTORY_ENV, SESSION_ENV, SandboxSubstrate
from .types import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    THREAD_HASH_LABEL,
    CapacityExhaustedError,
    ClaimTimeoutError,
    ClaimView,
    NoRouteError,
    QuotaRejection,
    RouteRecord,
    RouteState,
    SandboxClient,
    SandboxError,
    SandboxHandle,
    SandboxView,
    SubstrateConfig,
    SuspendedThreadError,
)

__all__ = [
    "HISTORY_ENV",
    "MANAGED_BY_LABEL",
    "MANAGED_BY_VALUE",
    "SESSION_ENV",
    "THREAD_HASH_LABEL",
    "AffinityStore",
    "CapacityExhaustedError",
    "ClaimTimeoutError",
    "ClaimView",
    "DockerError",
    "DockerSandboxClient",
    "KubernetesSandboxClient",
    "NoRouteError",
    "QuotaRejection",
    "RouteRecord",
    "RouteState",
    "RunnerHardening",
    "SandboxClient",
    "SandboxError",
    "SandboxHandle",
    "SandboxSubstrate",
    "SandboxView",
    "SubstrateConfig",
    "SuspendedThreadError",
]
