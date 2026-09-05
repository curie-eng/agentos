#!/usr/bin/python3
"""Recording external process boundary for the upgrade driver's CLI tests."""

import base64
import copy
import hashlib
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ["UPGRADE_DRIVER_ROOT"])
scenario = os.environ["UPGRADE_DRIVER_SCENARIO"]
program = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]


def api_image(values):
    api = values.get("api", {})
    api = api if isinstance(api, dict) else {}
    image = api.get("image", {})
    image = image if isinstance(image, dict) else {}
    return (image.get("repository") or "ghcr.io/curie-eng/curie-api") + ":0.9.0"


retained_values = json.loads((root / "values.json").read_text())

workload = {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "metadata": {
        "name": "acme-bot-api",
        "namespace": "upgrade-test",
        "generation": 2,
        "labels": {"app.kubernetes.io/instance": "acme-bot", "app.kubernetes.io/component": "api"},
    },
    "spec": {
        "replicas": 1,
        "selector": {
            "matchLabels": {
                "app.kubernetes.io/instance": "acme-bot",
                "app.kubernetes.io/component": "api",
            }
        },
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "api",
                        "image": api_image(retained_values),
                        "env": [{"name": "TEST", "value": "retained"}],
                    }
                ]
            }
        },
    },
}
if scenario.startswith("init-image-"):
    workload["spec"]["template"]["spec"]["initContainers"] = [
        {"name": "init", "image": "busybox:1.36"}
    ]
database = {
    "apiVersion": "apps/v1",
    "kind": "StatefulSet",
    "metadata": {
        "name": "acme-bot-postgres",
        "generation": 1,
        "labels": {"app.kubernetes.io/instance": "acme-bot"},
    },
    "spec": {
        "replicas": 1,
        "serviceName": "acme-bot-postgres",
        "selector": {
            "matchLabels": {
                "app.kubernetes.io/instance": "acme-bot",
                "app.kubernetes.io/component": "postgres",
            }
        },
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "postgres",
                        "image": "postgres:16-alpine",
                        "env": [
                            {
                                "name": "POSTGRES_DB",
                                "value": "other"
                                if scenario == "recovery-db-mismatch"
                                else "postgres",
                            },
                            {"name": "POSTGRES_USER", "value": "postgres"},
                        ],
                    }
                ]
            }
        },
    },
    "status": {"readyReplicas": 1, "updatedReplicas": 1, "observedGeneration": 1},
}
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps([program, *args]) + "\n")

if program == "helm":
    if args[0] in ("status", "list"):
        installed = (
            (root / "installed-version").read_text()
            if (root / "installed-version").exists()
            else "0.8.5"
        )
        if scenario == "helm-forbidden":
            print("Error: Kubernetes API forbidden", file=sys.stderr)
            sys.exit(1)
        if args[0] == "list":
            print(
                json.dumps(
                    [
                        {
                            "name": "acme-bot",
                            "chart": f"curie-{installed}",
                            "app_version": installed,
                            "status": "deployed",
                        }
                    ]
                )
            )
        else:
            print(
                json.dumps(
                    {
                        "chart": {"metadata": {"version": installed}},
                        "info": {"status": "failed" if scenario == "helm-failed" else "deployed"},
                        "hooks": []
                        if scenario == "missing-hooks"
                        else [
                            {
                                "events": [event],
                                "last_run": {"phase": "Succeeded"},
                                "manifest": json.dumps(
                                    {
                                        "metadata": {
                                            "labels": {"app.kubernetes.io/component": component}
                                        }
                                    }
                                ),
                            }
                            for component, event in [
                                ("schema-migrate", "pre-upgrade"),
                                ("upgrade-drain", "pre-upgrade"),
                                ("upgrade-drain-release", "post-upgrade"),
                            ]
                        ],
                    }
                )
            )
    elif args[:2] == ["show", "chart"]:
        version = "0.8.5" if scenario == "wrong-chart" else "0.9.0"
        print(f"name: curie\nversion: {version}\nappVersion: {version}")
    elif args[0] == "template":
        rendered_values = json.loads(pathlib.Path(args[args.index("-f") + 1]).read_text())
        rendered_image = api_image(rendered_values)
        if "templates/api.yaml" in args:
            # The actual api.yaml emits a Service followed by the Deployment.
            print(json.dumps({"apiVersion": "v1", "kind": "Service"}))
            if scenario == "rendered-api-missing":
                sys.exit(0)
            print("---")
            rendered = copy.deepcopy(workload)
            rendered["spec"]["template"]["spec"]["containers"][0]["image"] = (
                "example.com/acme-other-api:0.9.0"
                if scenario == "rendered-api-image-mismatch"
                else rendered_image
            )
            print(json.dumps(rendered))
            if scenario == "rendered-api-duplicate":
                print("---")
                print(json.dumps(rendered))
            sys.exit(0)
        metadata = {
            "schema_min": "0040",
            "schema_head": "0040",
            "revisions": [
                {"revision": "0039", "parents": [], "kind": "expand", "sha256": "a" * 64},
                {
                    "revision": "0040",
                    "parents": ["0039"],
                    "kind": "contract" if scenario == "schema-contract" else "expand",
                    "sha256": "b" * 64,
                },
            ],
        }
        data = {
            "application-version": "0.8.5" if scenario == "schema-metadata-mismatch" else "0.9.0",
            "compatibility.json": json.dumps(metadata),
            "api-image": "example.com/acme-other-api:0.9.0"
            if scenario == "metadata-image-mismatch"
            else rendered_image,
        }
        if scenario == "metadata-image-missing":
            data.pop("api-image")
        elif scenario == "metadata-image-empty":
            data["api-image"] = ""
        elif scenario == "metadata-image-invalid":
            data["api-image"] = ["invalid"]
        print(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "acme-bot-schema-compat"},
                    "data": data,
                }
            )
        )
    elif args[:2] == ["get", "values"]:
        print((root / "values.json").read_text())
    elif args[:2] == ["get", "manifest"]:
        print(json.dumps(workload))
        if scenario.startswith("recovery-"):
            if scenario == "recovery-foreign-namespace":
                database["metadata"]["namespace"] = "other-namespace"
            print("---")
            print(json.dumps(database))
            print("---")
            print(
                json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Service",
                        "metadata": {"name": "acme-bot-postgres"},
                        "spec": {"ports": [{"name": "postgres", "port": 5432}]},
                    }
                )
            )
            if scenario == "recovery-duplicate-db":
                second = copy.deepcopy(database)
                second["metadata"]["name"] = "acme-bot-other-postgres"
                print("---")
                print(json.dumps(second))
        if scenario == "secret-string-data":
            print("---")
            print(
                json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": "acme-bot-secret"},
                        "stringData": {"key": "fixture-secret-value"},
                    }
                )
            )
    elif args[0] == "upgrade":
        if scenario == "helm-hook-fails":
            sys.exit(1)
        if "-f" in args:
            values = pathlib.Path(args[args.index("-f") + 1]).read_text()
            (root / "applied-values.json").write_text(values)
            (root / "values.json").write_text(values)
        (root / "installed-version").write_text("0.9.0")
        (root / "api-unavailable").unlink(missing_ok=True)
        if scenario == "helm-success-reply-lost":
            sys.exit(1)
    else:
        print("unsupported recording Helm command", file=sys.stderr)
        sys.exit(64)
elif program == "kubectl":
    if args[:2] == ["get", "configmap"]:
        record = root / "record.json"
        if record.exists():
            if "json" in args:
                version = (
                    (root / "record-version").read_text()
                    if (root / "record-version").exists()
                    else "1"
                )
                print(
                    json.dumps(
                        {
                            "metadata": {"resourceVersion": version},
                            "data": {"record": record.read_text()},
                        }
                    )
                )
                if scenario == "checkpoint-conflict":
                    (root / "record-version").write_text(str(int(version) + 1))
            else:
                print(record.read_text())
        elif "--ignore-not-found" not in args:
            print("Error from server (NotFound): configmaps not found", file=sys.stderr)
            sys.exit(1)
    elif args[0] in ("apply", "create", "replace"):
        if scenario == "checkpoint-fails":
            print("Error from server (Forbidden): checkpoint write denied", file=sys.stderr)
            sys.exit(1)
        manifest = json.loads(pathlib.Path(args[args.index("-f") + 1]).read_text())
        version = (
            int((root / "record-version").read_text()) if (root / "record-version").exists() else 0
        )
        if args[0] == "replace" and manifest["metadata"].get("resourceVersion") != str(version):
            print("Error from server (Conflict): stale resource version", file=sys.stderr)
            sys.exit(1)
        if args[0] == "create" and (root / "record.json").exists():
            sys.exit(1)
        version += 1
        (root / "record-version").write_text(str(version))
        (root / "record.json").write_text(manifest["data"]["record"])
        record = json.loads(manifest["data"]["record"])
        if scenario.startswith("interrupt-after-") and record["completed"][
            -1
        ] == scenario.removeprefix("interrupt-after-"):
            sys.exit(1)
        print(json.dumps({"metadata": {"resourceVersion": str(version)}}))
    elif args[:2] == ["get", "deploy,sts,ds"] or (args[0] == "get" and "-f" in args):
        workload["status"] = {
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
            "observedGeneration": 2,
        }
        if scenario == "stale-generation":
            workload["status"]["observedGeneration"] = 1
        if scenario == "wrong-image":
            workload["spec"]["template"]["spec"]["containers"][0]["image"] = (
                "ghcr.io/curie-eng/curie-api:0.8.5"
            )
        if scenario == "wrong-manifest":
            workload["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "drift"
        if scenario == "missing-object":
            if "--ignore-not-found" not in args:
                sys.exit(1)
            print(json.dumps({"items": []}))
            sys.exit(0)
        objects = [workload]
        if scenario.startswith("recovery-"):
            objects.append(database)
            objects.append(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": "acme-bot-postgres"},
                    "spec": {"ports": [{"name": "postgres", "port": 5432}]},
                }
            )
        if scenario == "secret-string-data":
            objects.append(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "acme-bot-secret"},
                    "data": {"key": base64.b64encode(b"fixture-secret-value").decode()},
                }
            )
        print(json.dumps({"items": objects}))
    elif args[:2] == ["get", "deployment"]:
        print(
            json.dumps(
                {
                    "status": {
                        "readyReplicas": 1 if scenario == "recovery-running-api-probe-fails" else 0
                    }
                }
            )
        )
    elif args[:2] == ["get", "statefulset"]:
        database["metadata"]["namespace"] = "upgrade-test"
        database["metadata"]["annotations"] = {
            "meta.helm.sh/release-name": "acme-bot",
            "meta.helm.sh/release-namespace": "upgrade-test",
        }
        print(json.dumps(database))
    elif args[:2] == ["get", "pods"]:
        pods = []
        workloads = [workload] + ([database] if scenario.startswith("recovery-") else [])
        for item in workloads:
            containers = copy.deepcopy(item["spec"]["template"]["spec"]["containers"])
            statuses = [
                {
                    "name": container["name"],
                    "image": (
                        "docker.io/library/" + container["image"]
                        if container["name"] == "postgres"
                        else container["image"]
                    ),
                    "imageID": "containerd://sha256:" + "d" * 64,
                    "ready": True,
                    "state": {"running": {}},
                }
                for container in containers
            ]
            pods.append(
                {
                    "metadata": {
                        "name": item["metadata"]["name"] + "-pod",
                        "labels": item["spec"]["selector"]["matchLabels"],
                    },
                    "spec": {"containers": containers},
                    "status": {"phase": "Running", "containerStatuses": statuses},
                }
            )
        if scenario.startswith("init-image-"):
            pods[0]["spec"]["initContainers"] = copy.deepcopy(
                workload["spec"]["template"]["spec"]["initContainers"]
            )
            pods[0]["status"]["initContainerStatuses"] = [
                {
                    "name": "init",
                    "image": "docker.io/library/busybox:1.36",
                    "imageID": ""
                    if scenario == "init-image-missing-id"
                    else "containerd://sha256:" + "e" * 64,
                    "state": {"terminated": {"exitCode": 0}},
                }
            ]
        if scenario == "wrong-running-image":
            pods[0]["status"]["containerStatuses"][0]["image"] = "ghcr.io/curie-eng/curie-api:0.8.5"
        if scenario == "missing-running-image-id":
            pods[0]["status"]["containerStatuses"][0]["imageID"] = ""
        if scenario == "missing-running-pod":
            pods = []
        if scenario == "stale-extra-pod":
            old = copy.deepcopy(pods[0])
            old["metadata"]["name"] = "acme-bot-api-old"
            old["spec"]["containers"][0]["image"] = "ghcr.io/curie-eng/curie-api:0.8.5"
            old["status"]["containerStatuses"][0]["image"] = "ghcr.io/curie-eng/curie-api:0.8.5"
            pods.append(old)
        print(json.dumps({"items": pods}))
    elif args[0] == "exec":
        if "upgrade-database-recovery" in args:
            if scenario == "recovery-db-fails":
                sys.exit(1)
            print(
                json.dumps(
                    {
                        "current_revision": "0040"
                        if scenario == "recovery-db-advanced"
                        else "9999"
                        if scenario == "recovery-db-unknown"
                        else "0039",
                        "database_name": "other"
                        if scenario == "recovery-live-catalog-mismatch"
                        else "postgres",
                    }
                )
            )
            sys.exit(0)
        if "upgrade-schema" in args:
            if (root / "api-unavailable").exists():
                sys.exit(1)
            if scenario == "schema-probe-fails":
                sys.exit(1)
            print(
                json.dumps(
                    {
                        "current_revision": None
                        if scenario == "schema-null"
                        else "unknown"
                        if scenario == "schema-unknown"
                        else ("0040" if (root / "installed-version").exists() else "0039"),
                        "source_head": "0039",
                        "database_endpoint_fingerprint": hashlib.sha256(
                            json.dumps(
                                ["acme-bot-postgres", 5432, "postgres"], separators=(",", ":")
                            ).encode()
                        ).hexdigest(),
                        "source_revisions": {
                            "0039": ("c" if scenario == "schema-content-mismatch" else "a") * 64,
                            "0040": "b" * 64,
                        },
                    }
                )
            )
            sys.exit(0)
        if scenario == "canary-fails" and "upgrade-canary" in args:
            sys.exit(1)
        if "upgrade-canary" in args or "upgrade-source-canary" in args:
            print(
                json.dumps(
                    {
                        "passed": True,
                        "agents_fingerprint": (
                            "b"
                            if (scenario == "lost-agents" and "upgrade-canary" in args)
                            or scenario == "additional-agents"
                            else "a"
                        )
                        * 64,
                    }
                )
            )
        elif "upgrade-queue-probe" in args:
            print(json.dumps({"queues_drained": True}))
        else:
            sys.exit(64)
    elif args[:2] == ["get", "deploy"]:
        pass
    else:
        print("unsupported recording kubectl command", file=sys.stderr)
        sys.exit(64)
