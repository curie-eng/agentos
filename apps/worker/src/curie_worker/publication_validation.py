"""Trusted worker validation of a runner snapshot against its private base."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .runner_client import RunnerWorkspaceSnapshot
from .workspace import WorkspaceClaimCoordinator, WorkspacePreparationError

MAX_PATCH_BYTES = 900_000


def _safe_changed_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return bool(
        path
        and not path.startswith("/")
        and "\\" not in path
        and not pure.is_absolute()
        and all(part not in ("", ".", "..") for part in pure.parts)
        and pure.parts[0] != ".git"
    )


def validate_snapshot_against_base(
    coordinator: WorkspaceClaimCoordinator,
    *,
    thread_key: str,
    snapshot: RunnerWorkspaceSnapshot,
) -> None:
    """Rehash the private base and prove the binary patch applies to it."""

    prepared = coordinator.current(thread_key)
    if prepared is None:
        raise WorkspacePreparationError(
            "publication-validation", "thread has no retained sanitized base"
        )
    if snapshot.repo_full_name.casefold() != prepared.repo_full_name.casefold():
        raise WorkspacePreparationError(
            "publication-validation", "snapshot repository does not match sanitized base"
        )
    if snapshot.base_sha != prepared.base_sha:
        raise WorkspacePreparationError(
            "publication-validation", "snapshot commit does not match sanitized base"
        )
    if len(snapshot.patch) > MAX_PATCH_BYTES:
        raise WorkspacePreparationError(
            "publication-validation", f"patch exceeds {MAX_PATCH_BYTES} raw bytes"
        )
    if not snapshot.changed_paths or not all(
        _safe_changed_path(path) for path in snapshot.changed_paths
    ):
        raise WorkspacePreparationError(
            "publication-validation", "snapshot contains no changes or an unsafe path"
        )

    scratch = Path(tempfile.mkdtemp(prefix="curie-publication-validate-"))
    try:
        archive = scratch / "base.tar.gz"
        total = 0
        with archive.open("wb") as output:
            for chunk in coordinator.stream_current_base(thread_key):
                total += len(chunk)
                if total > coordinator.preparer.limits.max_archive_bytes:
                    raise WorkspacePreparationError(
                        "publication-validation", "private base archive exceeds configured limit"
                    )
                output.write(chunk)
        checkout = scratch / "checkout"
        checkout.mkdir(mode=0o700)
        try:
            with tarfile.open(archive, mode="r:gz") as source:
                source.extractall(checkout, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise WorkspacePreparationError(
                "publication-validation", "private base archive could not be extracted"
            ) from exc
        patch_file = scratch / "changes.patch"
        patch_file.write_bytes(snapshot.patch)
        try:
            subprocess.run(
                ["git", "apply", "--check", "--binary", str(patch_file)],
                cwd=checkout,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
                check=True,
            )
        except FileNotFoundError as exc:
            raise WorkspacePreparationError(
                "publication-validation", "worker image does not contain git"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WorkspacePreparationError(
                "publication-validation", "git apply --check exceeded 30 seconds"
            ) from exc
        except subprocess.CalledProcessError as exc:
            diagnostic = exc.stderr.decode("utf-8", errors="replace").strip()
            raise WorkspacePreparationError(
                "publication-validation", f"patch does not apply to sanitized base: {diagnostic}"
            ) from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

