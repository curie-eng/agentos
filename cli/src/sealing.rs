//! The cluster keypair that sealed connector credentials are sealed to
//! (ADR-0094).
//!
//! The property this buys, and the only one that matters: **a committed blob is
//! useless to anyone without the cluster's private key.** That is what lets a
//! credential live in an agent repository — public or private — beside the
//! connector that reads it, instead of being typed onto the cluster by hand.
//!
//! NaCl sealed boxes (X25519 + XSalsa20-Poly1305) rather than a hand-assembled
//! construction: "encrypt anonymously to a recipient public key" is exactly one
//! named primitive, and using it means there is no nonce discipline, key
//! derivation, or AEAD pairing for this repository to get wrong. The sender is
//! anonymous by design here, which is correct — a sealed value authenticates
//! nothing about who sealed it, and nothing downstream should believe it does.
//!
//! **Two active keys, always.** ADR-0094 names rotation as the strongest
//! argument against this whole approach: a cluster keypair cannot be rotated
//! the way a GitHub App key can, because changing it invalidates every blob
//! every agent repository has committed. So the release carries a CURRENT key
//! and an optional PREVIOUS one, and decryption tries both. That makes rotation
//! an overlap — seal new values to the current key, keep opening old ones with
//! the previous — instead of a flag day.

use anyhow::{bail, Context, Result};
use base64::Engine as _;
use crypto_box::{aead::OsRng, PublicKey, SecretKey};

/// The chart value holding the private half of the current keypair.
pub const SEALING_PRIVATE_KEY: &str = "sealing.privateKey";
/// The chart value holding the private half of the PREVIOUS keypair, kept so a
/// rotation can overlap rather than invalidate every committed blob at once.
pub const SEALING_PREVIOUS_PRIVATE_KEY: &str = "sealing.previousPrivateKey";

/// Every chart value this feature owns, for the preserved-across-upgrade set.
///
/// A plain `cluster up` does a FULL upgrade and drops anything it does not
/// re-pass. For a generated password that means rotating a live store's
/// credential (#1256's class); for THIS key it would mean every sealed
/// credential in every agent repository becoming permanently unreadable. It is
/// the same bug with a far worse blast radius, so the keys ride the same
/// preservation path the other generated secrets do.
pub const SEALING_MANAGED_KEYS: &[&str] = &[SEALING_PRIVATE_KEY, SEALING_PREVIOUS_PRIVATE_KEY];

fn b64() -> base64::engine::general_purpose::GeneralPurpose {
    base64::engine::general_purpose::STANDARD
}

/// A freshly generated keypair, base64-encoded for transport as chart values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Keypair {
    pub private_key: String,
    pub public_key: String,
}

/// Generate a keypair from the OS CSPRNG.
pub fn generate_keypair() -> Keypair {
    let secret = SecretKey::generate(&mut OsRng);
    let public = secret.public_key();
    Keypair {
        private_key: b64().encode(secret.to_bytes()),
        public_key: b64().encode(public.as_bytes()),
    }
}

fn decode_key(encoded: &str, what: &str) -> Result<[u8; 32]> {
    let raw = b64()
        .decode(encoded.trim())
        .with_context(|| format!("{what} is not valid base64"))?;
    let len = raw.len();
    raw.try_into()
        .map_err(|_| anyhow::anyhow!("{what} must be 32 bytes, got {len}"))
}

/// The public half of a stored private key.
///
/// Derived rather than stored alongside it, so the two can never disagree. An
/// operator publishing a public key that does not match the cluster's private
/// key would hand out a padlock nothing can open, and the failure would only
/// appear at the first deploy of a sealed credential.
pub fn public_key_of(private_key: &str) -> Result<String> {
    let bytes = decode_key(private_key, "the sealing private key")?;
    let secret = SecretKey::from(bytes);
    Ok(b64().encode(secret.public_key().as_bytes()))
}

/// Seal a value to a cluster public key.
///
/// The output is base64 of a NaCl sealed box: an ephemeral public key followed
/// by the ciphertext. Safe to commit — it reveals nothing without the matching
/// private key, and re-sealing the same value produces a different blob, so a
/// repository's history does not leak that two secrets are equal.
pub fn seal(public_key: &str, value: &str) -> Result<String> {
    let bytes = decode_key(public_key, "the sealing public key")?;
    let recipient = PublicKey::from(bytes);
    let sealed = recipient
        .seal(&mut OsRng, value.as_bytes())
        .map_err(|_| anyhow::anyhow!("could not seal the value"))?;
    Ok(b64().encode(sealed))
}

/// Open a sealed blob with any of the cluster's active private keys.
///
/// Tries each in order and returns the first success. That is what makes a
/// rotation an overlap: during one, values sealed to either key still open.
pub fn open_with_any(private_keys: &[String], blob: &str) -> Result<String> {
    if private_keys.iter().all(|k| k.trim().is_empty()) {
        bail!("no sealing private key is configured on this release");
    }
    let raw = b64()
        .decode(blob.trim())
        .context("the sealed value is not valid base64")?;

    for key in private_keys.iter().filter(|k| !k.trim().is_empty()) {
        let Ok(bytes) = decode_key(key, "a sealing private key") else {
            continue;
        };
        if let Ok(plain) = SecretKey::from(bytes).unseal(&raw) {
            return String::from_utf8(plain)
                .context("a sealed value decrypted to something that is not UTF-8 text");
        }
    }
    // Deliberately does NOT say which key failed or why. The caller turns this
    // into "skip this agent and leave the running connector alone" (ADR-0094):
    // deploying the connector without its credential would produce a pod that
    // starts, passes its health check, and 401s every call.
    bail!(
        "this sealed value does not decrypt with any of the cluster's active sealing keys. \
         It was probably sealed to a different cluster, or to a key that has since been \
         rotated out. Re-seal it with `curie seal` against this cluster's public key."
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_sealed_value_opens_with_its_own_key() {
        let kp = generate_keypair();
        let blob = seal(&kp.public_key, "grafana-token-value").expect("seals");
        assert_eq!(
            open_with_any(&[kp.private_key], &blob).expect("opens"),
            "grafana-token-value"
        );
    }

    /// The whole point: the blob is committed, so it must reveal nothing.
    #[test]
    fn a_sealed_blob_does_not_contain_the_plaintext() {
        let kp = generate_keypair();
        let secret = "super-secret-grafana-token";
        let blob = seal(&kp.public_key, secret).expect("seals");
        assert!(!blob.contains(secret));
        let decoded = b64().decode(&blob).expect("base64");
        assert!(
            !String::from_utf8_lossy(&decoded).contains(secret),
            "the plaintext must not survive in the raw bytes either"
        );
    }

    /// A different cluster's key must not open it, or the property is worthless.
    #[test]
    fn another_clusters_key_cannot_open_it() {
        let ours = generate_keypair();
        let theirs = generate_keypair();
        let blob = seal(&ours.public_key, "value").expect("seals");
        let err = open_with_any(&[theirs.private_key], &blob).expect_err("must not open");
        assert!(format!("{err:#}").contains("does not decrypt"), "{err:#}");
    }

    /// Re-sealing the same value must differ, or the repository history leaks
    /// which credentials are equal to each other.
    #[test]
    fn sealing_the_same_value_twice_gives_different_blobs() {
        let kp = generate_keypair();
        let a = seal(&kp.public_key, "same").expect("seals");
        let b = seal(&kp.public_key, "same").expect("seals");
        assert_ne!(a, b);
        assert_eq!(
            open_with_any(std::slice::from_ref(&kp.private_key), &a).unwrap(),
            "same"
        );
        assert_eq!(open_with_any(&[kp.private_key], &b).unwrap(), "same");
    }

    /// The rotation overlap ADR-0094 requires: during one, a value sealed to
    /// EITHER active key still opens. Without this, rotating the keypair
    /// silently breaks every agent repository at once.
    #[test]
    fn a_value_sealed_to_the_previous_key_still_opens_during_rotation() {
        let previous = generate_keypair();
        let current = generate_keypair();
        let old_blob = seal(&previous.public_key, "sealed-before-rotation").expect("seals");
        let new_blob = seal(&current.public_key, "sealed-after-rotation").expect("seals");

        let active = vec![current.private_key, previous.private_key];
        assert_eq!(
            open_with_any(&active, &old_blob).expect("the previous key must still work"),
            "sealed-before-rotation"
        );
        assert_eq!(
            open_with_any(&active, &new_blob).expect("the current key must work"),
            "sealed-after-rotation"
        );
    }

    /// Once the previous key is dropped, blobs sealed to it stop opening -- and
    /// must say so rather than failing obscurely.
    #[test]
    fn dropping_the_previous_key_ends_the_overlap_with_a_clear_error() {
        let previous = generate_keypair();
        let current = generate_keypair();
        let old_blob = seal(&previous.public_key, "stale").expect("seals");
        let err = open_with_any(&[current.private_key], &old_blob).expect_err("must fail");
        let msg = format!("{err:#}");
        assert!(msg.contains("rotated out"), "{msg}");
        assert!(msg.contains("curie seal"), "must name the remedy: {msg}");
    }

    /// The public half is derived, never stored beside the private half, so the
    /// two cannot drift into a padlock nothing can open.
    #[test]
    fn the_public_key_is_derivable_from_the_private_key() {
        let kp = generate_keypair();
        assert_eq!(
            public_key_of(&kp.private_key).expect("derives"),
            kp.public_key
        );
    }

    #[test]
    fn a_release_with_no_sealing_key_says_so() {
        let err = open_with_any(&["".to_string()], "AAAA").expect_err("must fail");
        assert!(
            format!("{err:#}").contains("no sealing private key"),
            "{err:#}"
        );
    }

    #[test]
    fn a_malformed_blob_is_rejected_rather_than_panicking() {
        let kp = generate_keypair();
        for bad in ["not base64!!", "c2hvcnQ="] {
            assert!(
                open_with_any(std::slice::from_ref(&kp.private_key), bad).is_err(),
                "{bad}"
            );
        }
    }

    #[test]
    fn generated_keypairs_are_distinct() {
        assert_ne!(
            generate_keypair().private_key,
            generate_keypair().private_key
        );
    }
}
