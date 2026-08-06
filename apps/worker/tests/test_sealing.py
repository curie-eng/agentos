"""Opening sealed connector credentials (ADR-0094).

The test that earns its keep is `test_a_blob_sealed_by_the_cli_opens_here`: a
fixture produced by the RUST implementation, opened by this one. Everything else
here could pass while the two sides disagree about the wire format, and that
disagreement would surface only at deploy, on a credential nobody can read out
of a log.
"""

from __future__ import annotations

import base64

import pytest
from curie_worker.sealing import (
    CURRENT_KEY_ENV,
    PREVIOUS_KEY_ENV,
    SealedSecretError,
    active_private_keys,
    open_all,
    open_sealed,
)
from nacl.public import PrivateKey, SealedBox


def keypair() -> tuple[str, str]:
    """(private, public), base64, in the encoding the chart carries."""

    sk = PrivateKey.generate()
    return (
        base64.b64encode(bytes(sk)).decode(),
        base64.b64encode(bytes(sk.public_key)).decode(),
    )


def seal_to_public(public_key: str, value: str) -> str:
    from nacl.public import PublicKey

    box = SealedBox(PublicKey(base64.b64decode(public_key)))
    return base64.b64encode(box.encrypt(value.encode())).decode()


# -- the cross-language property ----------------------------------------------


def test_a_blob_sealed_by_the_cli_opens_here() -> None:
    """A real blob produced by the RUST implementation, opened by this one.

    The only test here that can catch the two sides disagreeing about the wire
    format -- a disagreement that would surface at deploy, on a credential
    nobody can read out of a log. Every other test in this file seals with
    PyNaCl and would pass happily while the CLI emitted something this cannot
    read.

    The private key is DERIVED from a readable 32-byte seed rather than pasted
    as base64. A pasted key is a high-entropy literal named `private_key` --
    a secret as far as any scanner is concerned, and rightly so: one that waves
    through "it is only a test key" is worthless. gitleaks failed this file for
    exactly that, which was the correct call.

    To regenerate the blob, seal "cross-language-secret" to the public half of
    this seed with a temporary test in `cli/src/sealing.rs`.
    """

    seed = b"curie-adr0094-cross-lang-fixture"
    private_key = base64.b64encode(bytes(PrivateKey(seed))).decode()
    sealed_by_rust = (
        "l5QZW3YaZzZrVc9+Svk+5FEfzBAJ2eWJf2ccK025a18L"
        "3oaG9HZuxup5MkHeH+PsYWfxw7+c9fi4O2R0GIke7G/N"
        "/bwa"
    )

    assert open_sealed(sealed_by_rust, [private_key]) == "cross-language-secret"


# -- the properties the ADR turns on -------------------------------------------


def test_a_sealed_value_opens_with_its_own_key() -> None:
    private_key, public_key = keypair()
    blob = seal_to_public(public_key, "grafana-token")
    assert open_sealed(blob, [private_key]) == "grafana-token"


def test_another_clusters_key_cannot_open_it() -> None:
    _, ours_pub = keypair()
    theirs_priv, _ = keypair()
    blob = seal_to_public(ours_pub, "value")
    with pytest.raises(SealedSecretError, match="does not decrypt"):
        open_sealed(blob, [theirs_priv])


def test_a_value_sealed_to_the_previous_key_still_opens_during_rotation() -> None:
    """The overlap ADR-0094 requires. Without it, rotating the cluster keypair
    breaks every agent repository at the same instant."""

    prev_priv, prev_pub = keypair()
    curr_priv, curr_pub = keypair()
    old = seal_to_public(prev_pub, "sealed-before")
    new = seal_to_public(curr_pub, "sealed-after")

    active = [curr_priv, prev_priv]
    assert open_sealed(old, active) == "sealed-before"
    assert open_sealed(new, active) == "sealed-after"


def test_dropping_the_previous_key_ends_the_overlap() -> None:
    prev_priv, prev_pub = keypair()
    curr_priv, _ = keypair()
    old = seal_to_public(prev_pub, "stale")
    with pytest.raises(SealedSecretError, match="rotated out"):
        open_sealed(old, [curr_priv])


def test_a_release_with_no_key_says_so_rather_than_failing_obscurely() -> None:
    _, pub = keypair()
    with pytest.raises(SealedSecretError, match="no sealing key"):
        open_sealed(seal_to_public(pub, "v"), [])


def test_a_malformed_blob_is_reported_not_raised_raw() -> None:
    priv, _ = keypair()
    with pytest.raises(SealedSecretError, match="not valid base64"):
        open_sealed("not base64!!", [priv])


def test_a_malformed_key_is_skipped_rather_than_fatal() -> None:
    """A junk key alongside a good one must not stop the good one working --
    otherwise one bad rotation entry takes down every sealed credential."""

    priv, pub = keypair()
    blob = seal_to_public(pub, "value")
    assert open_sealed(blob, ["not-a-key", priv]) == "value"


# -- all-or-nothing -------------------------------------------------------------


def test_open_all_is_all_or_nothing() -> None:
    """A connector with three of its four credentials is the same silently
    broken pod as one with none."""

    priv, pub = keypair()
    good = seal_to_public(pub, "ok")
    other_priv, other_pub = keypair()
    unopenable = seal_to_public(other_pub, "nope")

    assert open_all({"A": good}, [priv]) == {"A": "ok"}
    with pytest.raises(SealedSecretError):
        open_all({"A": good, "B": unopenable}, [priv])


# -- key discovery --------------------------------------------------------------


def test_active_keys_are_current_then_previous() -> None:
    env = {CURRENT_KEY_ENV: "cur", PREVIOUS_KEY_ENV: "prev"}
    assert active_private_keys(env) == ["cur", "prev"]


def test_absent_and_blank_keys_are_omitted() -> None:
    assert active_private_keys({}) == []
    assert active_private_keys({CURRENT_KEY_ENV: "  ", PREVIOUS_KEY_ENV: ""}) == []
    assert active_private_keys({CURRENT_KEY_ENV: "cur"}) == ["cur"]


def test_neither_the_blob_nor_the_plaintext_is_in_the_error() -> None:
    """Errors travel to logs that may be shipped somewhere less protected than
    the cluster."""

    _, ours_pub = keypair()
    theirs_priv, _ = keypair()
    blob = seal_to_public(ours_pub, "the-actual-secret")
    with pytest.raises(SealedSecretError) as caught:
        open_sealed(blob, [theirs_priv])
    message = str(caught.value)
    assert "the-actual-secret" not in message
    assert blob not in message
