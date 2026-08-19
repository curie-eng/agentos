"""Derive Kubernetes objects from a declared connector (ADR-0086).

This is the half of #1063 that deletes the bundle author's Kubernetes. Given a
``ConnectorSpec``, it produces the Deployment, Service, and the two
NetworkPolicies -- egress from the sandbox, ingress to the connector -- that
Curie previously expected every author to write by hand.

Deriving rather than documenting is the point. Two defects in the hand-written
version were hit in practice, and both are unrepresentable here:

1. **A NetworkPolicy naming a Service ClusterIP can never match.** kube-proxy
   DNATs the destination to a pod IP before NetworkPolicy is evaluated
   (netfilter runs ``nat`` before ``filter``), so an ``ipBlock`` of the
   ClusterIP is dead on arrival -- and the symptom is a bare connection
   refused. Worse, it *appears* to work on a cluster whose CNI ignores
   NetworkPolicy (minikube's default), so a broken rule and a correct one are
   indistinguishable until the bundle reaches a cluster that enforces. This
   renderer only ever emits a ``podSelector``, so the broken form cannot be
   written.

2. **Host-header validation.** Servers that guard against DNS rebinding
   (``mcp-grafana`` 0.17+) default their allowlist to loopback, so an
   in-cluster caller reaching them by Service DNS gets
   ``forbidden: host not allowed``. Curie names the Service, so Curie can
   supply every name the sandbox may dial -- see ``host_aliases``.

Hardening is applied here, not left to the author: non-root, read-only rootfs,
all capabilities dropped, resource bounds. An author cannot forget what they do
not write.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any

from .connectors import ConnectorSpec

# Service names are DNS labels, so 63 characters is the hard ceiling. Names that
# would exceed it are truncated and disambiguated with a digest rather than
# clipped, since clipping alone can collide two distinct connectors back
# together -- the exact failure this module's naming is meant to prevent.
_DNS_LABEL_MAX = 63
_DIGEST_LEN = 8


def sandbox_selector(release: str, app_name: str) -> dict[str, str]:
    """The pods Rail 1's default-deny egress selects, exactly.

    Must match the chart's own selector. Two failure modes if it does not:

    - Too NARROW and it selects nothing, so the allow widens nothing and the
      sandbox still cannot reach the connector (NetworkPolicy is additive; it
      can only add to what another permits -- ADR-0067).
    - Too BROAD -- e.g. keying only on ``component`` -- and it also selects the
      sandbox pods of every OTHER Curie release in the namespace, granting them
      egress to a connector that is not theirs. That is a cross-release leak,
      and it is silent.

    ``instance`` is the Helm release, ``name`` is nameOverride; they differ
    whenever a release is installed under a different name than the app.
    """

    return {
        "app.kubernetes.io/name": app_name,
        "app.kubernetes.io/instance": release,
        "app.kubernetes.io/component": "runner-sandbox",
    }


def object_name(release: str, agent: str, connector: str) -> str:
    """Kubernetes object name for a connector. Stable and derivable.

    Scoped to the AGENT, not just the release. Curie runs many agents per
    release, so a release-scoped name means two agents that each declare a
    connector called ``grafana`` render byte-identical objects and silently
    overwrite one another -- with different images, different env, and a
    different credential. The dev-tier agent ends up pointed at the prod
    endpoint holding the prod token, and nothing errors (#1116).

    It also makes pruning safe. Objects are pruned by an owner label; with
    colliding names, ownership silently transfers to whoever deployed last, and
    one agent removing a connector deletes another agent's running server.
    """

    base = f"{release}-{agent}-mcp-{connector}"
    if len(base) <= _DNS_LABEL_MAX:
        return base
    # Truncate WITH a digest of the full name: clipping alone would map two long
    # names that share a prefix onto the same object, reintroducing the very
    # collision this function exists to prevent.
    digest = hashlib.sha256(base.encode()).hexdigest()[:_DIGEST_LEN]
    keep = _DNS_LABEL_MAX - _DIGEST_LEN - 1
    return f"{base[:keep].rstrip('-')}-{digest}"


def service_dns(release: str, agent: str, connector: str, namespace: str) -> str:
    return f"{object_name(release, agent, connector)}.{namespace}.svc.cluster.local"


def host_aliases(release: str, agent: str, connector: str, namespace: str, port: int) -> list[str]:
    """Every name the sandbox might use to reach this connector.

    Passed to servers that validate the Host header. Curie created the Service,
    so Curie knows the full set -- the author would have had to guess it, and
    guessing wrong yields ``forbidden: host not allowed`` with no hint.
    """

    short = object_name(release, agent, connector)
    return [
        f"{short}:{port}",
        f"{short}.{namespace}:{port}",
        f"{service_dns(release, agent, connector, namespace)}:{port}",
    ]


def render_service(release: str, agent: str, connector: str, spec: ConnectorSpec) -> dict[str, Any]:
    name = object_name(release, agent, connector)
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "labels": _labels(release, agent, connector)},
        "spec": {
            "type": "ClusterIP",
            "selector": _labels(release, agent, connector),
            "ports": [{"name": "http", "port": spec.port, "targetPort": "http"}],
        },
    }


# Placeholders an author may write in `args`/`env`, substituted at render with
# values only Curie can know. Declared here and re-exported for the validator,
# so the accepted set and the substituted set cannot drift apart.
PLACEHOLDERS = (
    "CURIE_ALLOWED_HOSTS",
    "CURIE_CONNECTOR_HOST",
    "CURIE_CONNECTOR_PORT",
    "CURIE_CONNECTOR_URL",
)


def substitutions(
    release: str, agent: str, connector: str, namespace: str, port: int
) -> dict[str, str]:
    """What each ``${CURIE_*}`` placeholder expands to for this connector.

    Every value here is one the AUTHOR cannot know: they are all derived from
    the Service name Curie invents, which embeds the release, the agent, and
    the namespace, and which differs per agent since #1116. A bundle deployed
    as two agents needs two different answers from the same file.
    """

    short = object_name(release, agent, connector)
    return {
        # Servers that guard against DNS rebinding default their allowlist to
        # loopback, so an in-cluster caller reaching them by Service DNS gets
        # `forbidden: host not allowed` (ADR-0086). All three forms, because
        # which one the sandbox dials depends on how the URL was written.
        "CURIE_ALLOWED_HOSTS": ",".join(host_aliases(release, agent, connector, namespace, port)),
        "CURIE_CONNECTOR_HOST": short,
        "CURIE_CONNECTOR_PORT": str(port),
        "CURIE_CONNECTOR_URL": f"http://{service_dns(release, agent, connector, namespace)}:{port}",
    }


def substitute(value: str, subs: dict[str, str]) -> str:
    """Expand ``${CURIE_*}`` placeholders in one arg or env value."""

    for key, replacement in subs.items():
        value = value.replace(f"${{{key}}}", replacement)
    return value


def render_deployment(
    release: str,
    agent: str,
    namespace: str,
    connector: str,
    spec: ConnectorSpec,
    secret_name: str,
) -> dict[str, Any]:
    name = object_name(release, agent, connector)
    subs = substitutions(release, agent, connector, namespace, spec.port)
    env: list[dict[str, Any]] = [
        {"name": k, "value": substitute(v, subs)} for k, v in sorted(spec.env.items())
    ]
    # Declared secrets arrive by reference, never as literals in the manifest.
    # A declared secret points either at the Secret Curie owns for this agent,
    # or at one provisioned out of band (#1163). Both render the same shape --
    # a secretKeyRef, never a literal -- so the container cannot tell them
    # apart and nothing downstream needs to.
    for declared in spec.secrets:
        if isinstance(declared, str):
            env.append(
                {
                    "name": declared,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": secret_name,
                            "key": declared,
                            "optional": False,
                        }
                    },
                }
            )
        else:
            env.append(
                {
                    "name": declared.name,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": declared.from_secret,
                            "key": declared.secret_key(),
                            # Not optional: a referenced Secret that does not
                            # exist must stop the pod, not start it without the
                            # credential and 401 on every call.
                            "optional": False,
                        }
                    },
                }
            )
    # secret_files project the SAME per-agent Secret as a file instead of an
    # env var, for servers that authenticate from disk (#1402). One volume per
    # file so each carries only its own key: a single volume with several items
    # would put every credential in one directory, and a server that reads a
    # directory would see the others.
    volumes: list[dict[str, Any]] = []
    volume_mounts: list[dict[str, Any]] = []
    for index, (secret_key, mount_path) in enumerate(sorted(spec.secret_files.items())):
        vol = f"secret-file-{index}"
        volumes.append(
            {
                "name": vol,
                "secret": {
                    "secretName": secret_name,
                    "items": [{"key": secret_key, "path": PurePosixPath(mount_path).name}],
                    # 0440, not 0400, and paired with the pod's fsGroup below.
                    # Secret volume files are owned by root:fsGroup -- NOT by
                    # runAsUser -- so an owner-only 0400 is unreadable by the
                    # 65532 the container actually runs as, and the server dies
                    # with `permission denied` on a file that is right there.
                    # Group-readable plus a matching fsGroup is the narrowest
                    # mode that a non-root container can actually open.
                    "defaultMode": 0o440,
                    # Not optional, matching `secrets:` -- a missing key must
                    # stop the pod rather than start a server that 401s on every
                    # call.
                    "optional": False,
                },
            }
        )
        volume_mounts.append(
            {
                "name": vol,
                "mountPath": mount_path,
                "subPath": PurePosixPath(mount_path).name,
                "readOnly": True,
            }
        )

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": _labels(release, agent, connector)},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": _labels(release, agent, connector)},
            "template": {
                "metadata": {"labels": _labels(release, agent, connector)},
                "spec": {
                    # Hardened by construction. The author never writes this, so
                    # the author cannot omit it.
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                        # Only when a secret is projected as a file: it makes
                        # the kubelet chgrp the volume so the non-root user can
                        # read a 0440 credential. Omitted otherwise, because
                        # fsGroup applies to every volume in the pod and there
                        # is no reason to widen ownership for a connector that
                        # mounts nothing.
                        **({"fsGroup": 65532} if spec.secret_files else {}),
                    },
                    "containers": [
                        {
                            "name": "server",
                            "image": spec.image,
                            "args": [substitute(a, subs) for a in spec.args],
                            "env": env,
                            **({"volumeMounts": volume_mounts} if volume_mounts else {}),
                            "ports": [{"name": "http", "containerPort": spec.port}],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "64Mi"},
                                "limits": {"cpu": "500m", "memory": "256Mi"},
                            },
                        }
                    ],
                    **({"volumes": volumes} if volumes else {}),
                },
            },
        },
    }


def render_networkpolicy(
    release: str, agent: str, app_name: str, connector: str, spec: ConnectorSpec
) -> dict[str, Any]:
    """Egress from the sandbox to this connector.

    Always a ``podSelector``. See the module docstring for why an ``ipBlock`` of
    the Service ClusterIP silently never matches.
    """

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"{object_name(release, agent, connector)}-allow",
            "labels": _labels(release, agent, connector),
        },
        "spec": {
            "podSelector": {"matchLabels": sandbox_selector(release, app_name)},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [{"podSelector": {"matchLabels": _labels(release, agent, connector)}}],
                    "ports": [{"protocol": "TCP", "port": spec.port}],
                }
            ],
        },
    }


def render_ingress_networkpolicy(
    release: str, agent: str, app_name: str, connector: str, spec: ConnectorSpec
) -> dict[str, Any]:
    """Ingress to this connector: any sandbox in this release, and nothing else.

    The egress policy above says where the sandbox may GO. It says nothing
    about who may ARRIVE, and those are not the same question. Without this,
    every pod in the namespace can call the connector -- and the connector is
    deliberately unauthenticated, because the sandbox holds no credential to
    authenticate WITH. So the network is not one layer of the access control
    here, it is the whole of it.

    What that is worth is concrete: a connector holds a production credential
    and answers anyone who asks. In a namespace that also runs Postgres,
    ClickHouse, Valkey, an object store and an OTLP collector, "any neighbour
    can read production through it" is a real step in a compromise, and those
    datastores already carry a default-deny of their own. The connector holding
    the credential should not be the one object without one.

    One policy, not the customary deny/allow PAIR. A NetworkPolicy that selects
    a pod and declares ``policyTypes: [Ingress]`` already denies every source it
    does not list (ADR-0067: policies are additive, and selecting a pod at all
    switches it from allow-by-default to deny-by-default for that direction).
    A separate default-deny object would be inert.

    Safe here specifically because the connector Deployment declares no probes:
    an ingress policy that omits the kubelet would otherwise fail readiness and
    take the connector out of its Service endpoints -- the failure mode being a
    connector that is healthy, running, and unreachable. If probes are ever
    added to ``render_deployment``, this rule has to grow a companion for them
    in the same commit.
    """

    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        # Not "-allow-ingress" paired with a renamed "-allow-egress": the
        # existing object ships as "-allow" on every live install, and renaming
        # it strands a policy that still grants egress under a name nothing
        # reconciles any more. Additive is the safe shape for a rule whose
        # failure mode is silent.
        "metadata": {
            "name": f"{object_name(release, agent, connector)}-allow-ingress",
            "labels": _labels(release, agent, connector),
        },
        "spec": {
            # Selects the CONNECTOR, where the egress policy selects the sandbox.
            "podSelector": {"matchLabels": _labels(release, agent, connector)},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [{"podSelector": {"matchLabels": sandbox_selector(release, app_name)}}],
                    "ports": [{"protocol": "TCP", "port": spec.port}],
                }
            ],
        },
    }


def render(
    *,
    release: str,
    agent: str,
    namespace: str,
    app_name: str,
    connector: str,
    spec: ConnectorSpec,
    secret_name: str,
) -> list[dict[str, Any]]:
    """Every object needed to run one hosted connector. Empty for a remote one.

    Keyword-only, deliberately. Four of the seven parameters are strings that
    look interchangeable at a call site -- release, agent, namespace, app_name --
    and they are not: in a real install `curie cluster up --namespace my-agent
    --release my-agent` against a chart whose `nameOverride` is empty leaves
    app_name as `curie` while release and namespace are both `my-agent`, so no
    one value can stand in for all four. Swapping release and app_name
    positionally makes `sandbox_selector` emit `app.kubernetes.io/name: my-agent`
    instead of `curie`, and both NetworkPolicies then parse, apply, and select
    no pod: every connector tool call dies as a bare connection timeout with no
    policy error anywhere. A positional call site cannot express that mistake
    here because there is no positional call site.

    A `build:` connector whose lock has not been applied raises instead of
    rendering (ADR 0113). The alternative is `render_deployment` emitting
    `"image": None`, which applies as an invalid Deployment and surfaces as an
    opaque Kubernetes error a long way from its cause. `connector_lock.apply_lock`
    is what resolves it, so the message names the artifact that is missing.
    """

    if not spec.is_hosted:
        return []
    if spec.image is None:
        raise ValueError(
            f"connector {connector!r} declares `build:` and has no resolved image. "
            "Its digest lives in connectors.lock.yaml, which `curie build "
            "--plugin-dir <dir>` writes, and connector_lock.apply_lock applies "
            "before anything renders."
        )
    return [
        render_service(release, agent, connector, spec),
        render_deployment(release, agent, namespace, connector, spec, secret_name),
        render_networkpolicy(release, agent, app_name, connector, spec),
        render_ingress_networkpolicy(release, agent, app_name, connector, spec),
    ]


def unhosted_mcp_entry(spec: ConnectorSpec) -> dict[str, Any] | None:
    """The entry for a tier that cannot host this connector, or None.

    None is a real answer, not a failure: a hosted connector with nowhere to
    point at IS "declared but not exercisable here" (#1093). Mounting a URL that
    resolves nowhere would turn that into a connection refused mid-turn.
    """

    if not spec.is_hosted:
        return mcp_entry("", "", "", "", spec)
    if not spec.unhosted_url:
        return None
    return {"type": "http", "url": spec.unhosted_url}


def mcp_entry(
    release: str, agent: str, namespace: str, connector: str, spec: ConnectorSpec
) -> dict[str, Any]:
    """The ``.mcp.json`` entry Curie injects, so the author writes no URL.

    For a hosted connector the URL is derived from the Service that Curie just
    created; hand-writing it is how a bundle ends up with an address that does
    not resolve in the tier it is deployed to.
    """

    if spec.is_hosted:
        return {
            "type": "http",
            "url": f"http://{service_dns(release, agent, connector, namespace)}:{spec.port}/mcp",
        }
    entry: dict[str, Any] = {"type": "http", "url": spec.url}
    if spec.headers:
        entry["headers"] = dict(spec.headers)
    return entry


def _labels(release: str, agent: str, connector: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": object_name(release, agent, connector),
        "app.kubernetes.io/part-of": release,
    }
