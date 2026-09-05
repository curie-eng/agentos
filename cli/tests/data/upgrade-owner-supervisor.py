#!/usr/bin/python3
"""Own the actual CLI and its exact Helm child across an owner-death test."""

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


def exited(fd):
    poll = select.poll()
    poll.register(fd, select.POLLIN)
    return bool(poll.poll(0))


def terminate(fd):
    if fd is not None:
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass


def interrupted(signum, frame):
    raise KeyboardInterrupt("owned process test interrupted")


def main():
    # pidfds keep both observation and cleanup bound to the exact processes,
    # even if a numeric PID is reused while an assertion unwinds.
    root = Path(os.environ["UPGRADE_DRIVER_ROOT"])
    assert Path(os.environ["TMPDIR"]) == root
    assert sys.argv[1] in ("SIGKILL", "SIGTERM")
    cli = None
    cli_fd = None
    helm_fd = None
    proof = {}
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        with (root / "cli.stdout").open("w") as stdout, (root / "cli.stderr").open("w") as stderr:
            cli = subprocess.Popen(
                sys.argv[2:], stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr
            )
            cli_fd = os.pidfd_open(cli.pid)
            deadline = time.monotonic() + 10
            while not (root / "helm-owner.json").exists():
                assert cli.poll() is None, "CLI ended before its owned Helm started"
                assert time.monotonic() < deadline, "owned Helm did not start"
                time.sleep(0.01)
            owner = json.loads((root / "helm-owner.json").read_text())
            assert owner["parent"] == cli.pid, "Helm must be the direct CLI child"
            helm_fd = os.pidfd_open(owner["pid"])
            assert not exited(helm_fd)
            signal.pidfd_send_signal(cli_fd, getattr(signal, sys.argv[1]))
            cli.wait(timeout=5)
            proof["owner_exited"] = True
            (root / "release-helm").write_text("owner stopped")
            deadline = time.monotonic() + 5
            while not exited(helm_fd):
                assert time.monotonic() < deadline, "Helm survived its owning CLI"
                time.sleep(0.01)
            proof["direct_child_exited"] = True
            proof["after_owner_mutation"] = (root / "after-owner-mutation").exists()
            assert not proof["after_owner_mutation"], (
                "Helm continued to mutate after its actual CLI owner died"
            )
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        terminate(cli_fd)
        if cli is not None:
            if cli_fd is None:
                cli.kill()
            cli.wait(timeout=5)
        terminate(helm_fd)
        deadline = time.monotonic() + 5
        while helm_fd is not None and not exited(helm_fd):
            assert time.monotonic() < deadline, "owned Helm cleanup failed"
            time.sleep(0.01)
        for fd in (cli_fd, helm_fd):
            if fd is not None:
                os.close(fd)
        proof["cleanup_complete"] = True
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
