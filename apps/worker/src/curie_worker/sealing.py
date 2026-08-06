"""Opening connector credentials sealed to this cluster (ADR-0094).

The decrypt half. `curie seal` produces a libsodium sealed box
(`crypto_box_seal`: X25519 + XSalsa20-Poly1305) base64-encoded; this reads it
back with the private key the chart mounts into the worker.

**Wire compatibility with the CLI is the load-bearing property**, and it is not
an assumption: both sides use the same named primitive rather than a
hand-assembled construction, and a test in this module opens a blob produced by
the Rust implementation. Two "compatible" AEAD stacks that disagree about nonce
derivation would fail only at deploy, on a credential nobody can read from the
logs.

**Two keys, tried in order.** ADR-0094 requires a rotation to overlap: the
current key and, while a rotation is in flight, the previous one. Without that,
rotating the cluster keypair invalidates every blob every agent repository has
committed, all at once.
"""

from __future__ import annotations

import base64
import logging
import os

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, SealedBox

logger = logging.getLogger(__name__)

CURRENT_KEY_ENV = "CURIE_SEALING_PRIVATE_KEY"
PREVIOUS_KEY_ENV = "CURIE_SEALING_PREVIOUS_PRIVATE_KEY"


class SealedSecretError(Exception):
    """A declared sealed value could not be opened.

    Deliberately fatal for the agent being reconciled. ADR-0094's alternative --
    deploy the connector without the credential -- produces a pod that starts,
    passes its health check, and 401s every call: healthy in `kubectl get pods`
    and broken for every user (the #1156 shape).
    """


def active_private_keys(env: dict[str, str] | None = None) -> list[str]:
    """The sealing keys this release holds, current first.

    Empty means the release has none, which is a legitimate state: an install
    that uses only operator-supplied credentials never needs one. It stops
    being legitimate the moment a bundle declares a sealed secret, and that is
    where the error belongs -- not here.
    """

    source = os.environ if env is None else env
    keys = []
    for name in (CURRENT_KEY_ENV, PREVIOUS_KEY_ENV):
        value = (source.get(name) or "").strip()
        if value:
            keys.append(value)
    return keys


def open_sealed(blob: str, private_keys: list[str]) -> str:
    """Open one sealed value, trying each active key.

    Never logs the blob or the plaintext. The blob is not secret in principle,
    but a log line pairing a connector name with its ciphertext is a standing
    invitation to anyone who later obtains the key.
    """

    if not private_keys:
        raise SealedSecretError(
            f"this release has no sealing key, so a sealed credential cannot be opened. "
            f"The chart supplies it as {CURRENT_KEY_ENV}; run `curie cluster up` to "
            f"generate one."
        )
    try:
        raw = base64.b64decode(blob.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001 -- any decode failure is the same operator error
        raise SealedSecretError(
            "a sealed value is not valid base64; re-seal it with `curie seal`"
        ) from exc

    for key in private_keys:
        try:
            secret = PrivateKey(base64.b64decode(key, validate=True))
        except Exception:  # noqa: BLE001 -- a malformed key is skipped, not fatal on its own
            continue
        try:
            return SealedBox(secret).decrypt(raw).decode("utf-8")
        except (CryptoError, UnicodeDecodeError):
            continue

    raise SealedSecretError(
        "a sealed value does not decrypt with any of this cluster's active sealing "
        "keys. It was sealed to a different cluster, or to a key that has since been "
        "rotated out. Re-seal it with `curie seal` against this cluster's public key."
    )


def open_all(sealed: dict[str, str], private_keys: list[str]) -> dict[str, str]:
    """Open every sealed value for one connector, or raise.

    All-or-nothing on purpose: a connector brought up with three of its four
    credentials is the same silently-broken pod as one brought up with none.
    """

    return {name: open_sealed(blob, private_keys) for name, blob in sorted(sealed.items())}
