#!/usr/bin/python3
"""Recording external process boundary for the upgrade driver's CLI tests."""

import base64
import json
import os
import pathlib
import sys

root = pathlib.Path(os.environ["UPGRADE_DRIVER_ROOT"])
scenario = os.environ["UPGRADE_DRIVER_SCENARIO"]
program = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]

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
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "api",
                        "image": "ghcr.io/curie-eng/curie-api:0.9.0",
                        "env": [{"name": "TEST", "value": "retained"}],
                    }
                ]
            }
        },
    },
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
        print(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "acme-bot-schema-compat"},
                    "data": {
                        "application-version": "0.8.5"
                        if scenario == "schema-metadata-mismatch"
                        else "0.9.0",
                        "compatibility.json": json.dumps(metadata),
                    },
                }
            )
        )
    elif args[:2] == ["get", "values"]:
        print((root / "values.json").read_text())
    elif args[:2] == ["get", "manifest"]:
        print(json.dumps(workload))
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
    elif args[0] == "exec":
        if "upgrade-schema" in args:
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
