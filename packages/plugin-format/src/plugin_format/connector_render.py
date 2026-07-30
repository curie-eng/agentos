"""Derive Kubernetes objects from a declared connector (ADR-0086).

This is the half of #1063 that deletes the bundle author's Kubernetes. Given a
``ConnectorSpec``, it produces the Deployment, Service, and NetworkPolicy that
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


def render_deployment(
    release: str, agent: str, connector: str, spec: ConnectorSpec, secret_name: str
) -> dict[str, Any]:
    name = object_name(release, agent, connector)
    env: list[dict[str, Any]] = [{"name": k, "value": v} for k, v in sorted(spec.env.items())]
    # Declared secrets arrive by reference, never as literals in the manifest.
    env += [
        {
            "name": s,
            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": s, "optional": False}},
        }
        for s in spec.secrets
    ]
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
                    },
                    "containers": [
                        {
                            "name": "server",
                            "image": spec.image,
                            "args": list(spec.args),
                            "env": env,
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


def render(
    release: str,
    agent: str,
    namespace: str,
    app_name: str,
    connector: str,
    spec: ConnectorSpec,
    secret_name: str,
) -> list[dict[str, Any]]:
    """Every object needed to run one hosted connector. Empty for a remote one."""

    if not spec.is_hosted:
        return []
    return [
        render_service(release, agent, connector, spec),
        render_deployment(release, agent, connector, spec, secret_name),
        render_networkpolicy(release, agent, app_name, connector, spec),
    ]


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
