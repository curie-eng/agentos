#!/usr/bin/env python3
"""CI catalog probe of the declared images and build contexts, without model calls.

Credentials below are inert fixture values. No real upstream tool is invoked.
Containers and the temporary directory belong only to this invocation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "examples/sre-bot"


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=600
    ).stdout.strip()


def main():
    owned = []
    images = []
    prefix = f"sre-catalog-{uuid.uuid4().hex[:12]}"
    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as scratch:
            temp = Path(scratch)
            kubeconfig = temp / "kubeconfig"
            kubeconfig.write_text("""apiVersion: v1
kind: Config
clusters:
- name: fixture
  cluster: {server: "https://127.0.0.1:9", insecure-skip-tls-verify: true}
contexts:
- name: fixture
  context: {cluster: fixture, user: fixture}
current-context: fixture
users:
- name: fixture
  user: {token: inert-catalog-fixture}
""")
            # Readable by nonroot container uid; contains no operational credential.
            kubeconfig.chmod(0o644)
            endpoints = {}
            for name, spec in yaml.safe_load((BUNDLE / "connectors.yaml").read_text())[
                "connectors"
            ].items():
                container = f"{prefix}-{name}"
                image = spec.get("image")
                if not image:
                    image = f"{prefix}-{name}:test"
                    context = (BUNDLE / spec["build"]["context"]).resolve()
                    if not context.is_relative_to(BUNDLE):
                        raise ValueError("build context outside bundle")
                    images.append(image)
                    docker("build", "-t", image, str(context))
                args = [
                    "run",
                    "-d",
                    "--name",
                    container,
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--memory=256m",
                    "-p",
                    "127.0.0.1::8000",
                    "-v",
                    f"{kubeconfig}:/secrets/kubeconfig:ro",
                ]
                environment = dict(spec.get("env", {}))
                environment.update(
                    {
                        "GRAFANA_URL": "http://127.0.0.1:9",
                        "GRAFANA_SERVICE_ACCOUNT_TOKEN": "inert-catalog-fixture",
                        "SELF_UPGRADE_KUBECONFIG": "/secrets/kubeconfig",
                    }
                )
                for key, value in environment.items():
                    args += ["-e", f"{key}={value}"]
                owned.append(container)
                docker(
                    *args,
                    image,
                    *[str(a).replace("${CURIE_ALLOWED_HOSTS}", "*") for a in spec.get("args", [])],
                )
                address = docker("port", container, "8000/tcp")
                endpoints[name] = f"http://{address}/mcp"
            path = temp / "endpoints.json"
            path.write_text(json.dumps(endpoints))
            # Bounded startup readiness by TCP only, not retries of failed assertions.
            import socket
            from urllib.parse import urlsplit

            for url in endpoints.values():
                target = urlsplit(url)
                deadline = time.monotonic() + 60
                while True:
                    try:
                        with socket.create_connection((target.hostname, target.port), timeout=1):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("connector startup") from None
                        time.sleep(0.2)
            return subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/sre-contract/check.py"),
                    "--bundle",
                    str(BUNDLE),
                    "--endpoints",
                    str(path),
                ],
                timeout=300,
            ).returncode
    except Exception as exc:
        print(f"SRE catalog CI failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        for container in reversed(owned):
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
        for image in images:
            subprocess.run(["docker", "image", "rm", image], capture_output=True, timeout=30)


if __name__ == "__main__":
    raise SystemExit(main())
