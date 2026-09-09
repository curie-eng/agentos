"""Completeness guard for the #2427 synthetic restore inventory.

The consumer path is tools/restore-drill/restore_inventory.py (invoked by
`curie dev restore-drill --check-backup`). A missing or corrupt required
component must refuse with recovery instructions. Secret values must never be
accepted inside the backup. Valkey stream replay is not a Curie restore
contract and must stay reported as missing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from restore_inventory import (
    SEPARATELY_SUPPLIED,
    RestoreRefused,
    check_backup,
    write_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
INVENTORY = REPO_ROOT / "tools" / "restore-drill" / "restore_inventory.py"


def _supplied() -> dict[str, str]:
    return {
        "POSTGRES_PASSWORD": "pg-secret-example",
        "VALKEY_PASSWORD": "vk-secret-example",
        "S3_ACCESS_KEY": "rustfs",
        "S3_SECRET_KEY": "rustfs-secret-example",
        "API_KEY": "api-secret-example",
    }


def _write(path: Path, body: bytes | str = b"payload") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body if isinstance(body, bytes) else body.encode())


def _complete_backup(tmp_path: Path) -> Path:
    backup = tmp_path / "backup"
    _write(backup / "postgres" / "curie.dump", b"pg-dump-bytes")
    _write(backup / "bundles" / "acme-bot" / "v1.tar.gz", b"bundle-bytes")
    _write(backup / "mail-adapter" / "state.sqlite3", b"sqlite-bytes")
    _write(backup / "valkey" / "dump.rdb", b"rdb-bytes")
    write_manifest(backup, candidate="abc123")
    return backup


def test_complete_backup_with_separately_supplied_config_is_accepted(tmp_path: Path) -> None:
    backup = _complete_backup(tmp_path)
    result = check_backup(backup, _supplied())
    assert result["ok"] is True
    assert result["rpo_rto_claimed"] is False
    assert result["valkey_replay_contract"] == "missing"
    assert result["separately_supplied"] == list(SEPARATELY_SUPPLIED)
    manifest = json.loads((backup / "MANIFEST.json").read_text())
    joined = json.dumps(manifest)
    for value in _supplied().values():
        assert value not in joined


@pytest.mark.parametrize(
    "component,needle",
    [
        ("postgres", "postgres"),
        ("bundles", "bundle"),
        ("mail-adapter-state", "mail"),
        ("valkey", "valkey"),
    ],
)
def test_omitted_required_component_refuses_and_explains_recovery(
    tmp_path: Path, component: str, needle: str
) -> None:
    backup = _complete_backup(tmp_path)
    if component == "postgres":
        (backup / "postgres" / "curie.dump").unlink()
    elif component == "bundles":
        for path in (backup / "bundles").rglob("*"):
            if path.is_file():
                path.unlink()
    elif component == "mail-adapter-state":
        (backup / "mail-adapter" / "state.sqlite3").unlink()
    else:
        (backup / "valkey" / "dump.rdb").unlink()

    with pytest.raises(RestoreRefused) as raised:
        check_backup(backup, _supplied())
    assert needle in str(raised.value).lower()
    assert raised.value.fix
    assert "rpo" not in raised.value.fix.lower()
    assert "rto" not in raised.value.fix.lower()


def test_corrupt_bundle_digest_refuses_before_serving(tmp_path: Path) -> None:
    backup = _complete_backup(tmp_path)
    (backup / "bundles" / "acme-bot" / "v1.tar.gz").write_bytes(b"tampered-bytes")
    with pytest.raises(RestoreRefused) as raised:
        check_backup(backup, _supplied())
    assert "digest" in str(raised.value).lower() or "sha256" in str(raised.value).lower()
    assert "bundle" in str(raised.value).lower()
    assert raised.value.fix


def test_missing_separately_supplied_key_refuses_and_does_not_read_it_from_backup(
    tmp_path: Path,
) -> None:
    backup = _complete_backup(tmp_path)
    supplied = _supplied()
    supplied.pop("API_KEY")
    with pytest.raises(RestoreRefused) as raised:
        check_backup(backup, supplied)
    assert "API_KEY" in str(raised.value)
    assert "separately" in str(raised.value).lower() or "supplied" in str(raised.value).lower()
    assert "api-secret-example" not in str(raised.value)


def test_secret_value_inside_backup_is_refused(tmp_path: Path) -> None:
    backup = _complete_backup(tmp_path)
    (backup / "notes.txt").write_text("password=pg-secret-example\n")
    with pytest.raises(RestoreRefused) as raised:
        check_backup(backup, _supplied())
    assert "secret" in str(raised.value).lower()
    assert "pg-secret-example" not in str(raised.value)
    assert "pg-secret-example" not in raised.value.fix


def test_secret_value_in_manifest_is_refused(tmp_path: Path) -> None:
    backup = _complete_backup(tmp_path)
    manifest_path = backup / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["operator_note"] = _supplied()["API_KEY"]
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RestoreRefused) as raised:
        check_backup(backup, _supplied())
    assert "secret" in str(raised.value).lower()
    assert _supplied()["API_KEY"] not in str(raised.value)


def test_keys_and_config_directory_in_backup_is_refused(tmp_path: Path) -> None:
    backup = _complete_backup(tmp_path)
    _write(backup / "keys-and-config" / "api-key", b"nope")
    with pytest.raises(RestoreRefused) as raised:
        check_backup(backup, _supplied())
    assert "keys-and-config" in str(raised.value) or "separately" in str(raised.value).lower()


def test_manifest_may_not_claim_rpo_rto_or_invent_valkey_replay(tmp_path: Path) -> None:
    backup = _complete_backup(tmp_path)
    manifest_path = backup / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["rpo_rto_claimed"] = True
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RestoreRefused):
        check_backup(backup, _supplied())

    write_manifest(backup, candidate="abc123")
    manifest = json.loads(manifest_path.read_text())
    manifest["valkey_replay_contract"] = "resume-pending-entries"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RestoreRefused) as raised:
        check_backup(backup, _supplied())
    assert "missing" in raised.value.fix.lower() or "replay" in str(raised.value).lower()


def test_check_backup_cli_is_the_consumer_path_for_the_guard(tmp_path: Path) -> None:
    import subprocess
    import sys

    backup = _complete_backup(tmp_path)
    (backup / "postgres" / "curie.dump").unlink()
    supplied = tmp_path / "supplied.json"
    supplied.write_text(json.dumps(_supplied()))
    completed = subprocess.run(
        [
            sys.executable,
            str(INVENTORY),
            "check",
            str(backup),
            "--supplied-config",
            str(supplied),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    payload = json.loads(completed.stdout)
    assert payload["error"]
    assert payload["fix"]
    assert "postgres" in payload["error"].lower()
    for value in _supplied().values():
        assert value not in completed.stdout
        assert value not in completed.stderr
