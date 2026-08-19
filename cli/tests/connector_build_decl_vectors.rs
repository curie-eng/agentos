// The Rust half of the frozen connector declaration / lock / DNS seam (ADR 0113).
//
// `curie build --plugin-dir` reads a bundle's `connectors.yaml` and writes
// `connectors.lock.yaml` before the platform ever sees the bundle, and
// `curie skill up` / `curie local deploy` start the connector containers under
// a Docker network alias the runner independently derives on the Python side.
// So the CLI carries hand mirrors of shapes and derivations that
// `packages/plugin-format` owns, in a language that cannot import them.
//
// The usual gate does not cover this seam. `curie dev field-parity` compares a
// Rust struct against `packages/plugin-format/schema/plugin-format.schema.json`,
// and `schema_export.py` imports only from `.models`, so the committed schema
// carries no `Connector*` `$defs` at all -- the connector structs are declared
// in `cli/plugin-format-mirrors.json`'s `non_mirrors` array and the field
// comparison is a no-op for them today. This file is the seam instead: it reads
// the same five corpora the Python suite reads, so a change made in one
// language and not the other fails that language's suite.
//
//   tests/vectors/connector-build-decl.json    the `build:` declaration
//   tests/vectors/connector-lock.json          connectors.lock.yaml + apply_lock
//   tests/vectors/connector-fields.json        the exact field-name sets
//   tests/vectors/connector-service-dns.json   object_name / service_dns
//   tests/vectors/connector-source-digest.json source_digest_of
//
// Same mechanism as `tests/vectors/approval-action-ids.json` for the
// dispatcher-versus-CLI action ids.

use curie::connector_build;
use serde_json::Value;
use std::collections::BTreeSet;
use std::path::Path;
use tempfile::TempDir;

// ─── Loaders ─────────────────────────────────────────────────────────────────

fn vector_file(name: &str) -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../tests/vectors")
        .join(name);
    let body = std::fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("read {}: {error}", path.display()));
    serde_json::from_str(&body).unwrap_or_else(|error| panic!("parse {}: {error}", path.display()))
}

fn vectors(name: &str) -> Vec<Value> {
    vector_file(name)["vectors"]
        .as_array()
        .unwrap_or_else(|| panic!("{name} has no `vectors` array"))
        .clone()
}

fn name_of(vector: &Value) -> &str {
    vector["name"].as_str().expect("vector has a name")
}

/// Materialize a `{relative path: file}` map under a fresh directory.
///
/// A value is either a content string, or `{"content": ..., "executable": true}`
/// for a file written with the owner execute bit set. The mode is part of the
/// digest -- the build context tar carries it -- so a materializer that ignored
/// the object form would write the executable vectors non-executable and fail
/// against a corpus that is right.
fn materialize(root: &Path, tree: &Value) {
    for (rel, value) in tree.as_object().expect("tree is an object") {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().expect("a parent")).expect("mkdir -p");
        let (content, executable) = match value {
            Value::String(content) => (content.as_str(), false),
            Value::Object(file) => (
                file["content"].as_str().expect("file content is a string"),
                file.get("executable")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
            ),
            other => panic!("a tree value is a string or an object, got {other}"),
        };
        std::fs::write(&path, content).expect("write");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).expect("stat").permissions().mode() & 0o7777;
            let mode = if executable {
                mode | 0o100
            } else {
                mode & !0o111
            };
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode))
                .expect("chmod the materialized file");
        }
        #[cfg(not(unix))]
        let _ = executable;
    }
}

fn scratch() -> TempDir {
    TempDir::new().expect("a scratch directory")
}

// ─── The `build:` declaration ────────────────────────────────────────────────

#[test]
fn the_rust_reader_accepts_and_rejects_exactly_the_documents_python_does() {
    // Accept/reject parity is the whole point: `curie build` reads the bundle
    // BEFORE upload, so a document the CLI accepts and the platform validator
    // rejects means an operator builds and pushes an image for a bundle that
    // can never deploy, and a document the CLI rejects and the platform accepts
    // means a bundle that deploys through git flow but cannot be built locally.
    for vector in vectors("connector-build-decl.json") {
        if vector.get("fixture").is_some() {
            continue; // filesystem cases have their own test below
        }
        let document = serde_norway::to_string(&vector["document"]).expect("serialize document");
        let parsed = connector_build::parse_connectors(&document);
        match vector["expect"].as_str().expect("expect") {
            "accept" => {
                let file = parsed.unwrap_or_else(|error| {
                    panic!("{} must be accepted, got {error}", name_of(&vector))
                });
                if let Some(expected) = vector.get("resolved_dockerfile") {
                    for (connector, dockerfile) in expected.as_object().expect("an object") {
                        let spec = file
                            .connectors
                            .get(connector)
                            .unwrap_or_else(|| panic!("{connector} is declared"));
                        let build = spec.build.as_ref().expect("a build block");
                        assert_eq!(
                            build.dockerfile,
                            dockerfile.as_str().expect("a string"),
                            "{}: resolved dockerfile",
                            name_of(&vector)
                        );
                    }
                }
            }
            "reject" => {
                assert!(
                    parsed.is_err(),
                    "{} must be rejected by the Rust reader",
                    name_of(&vector)
                );
            }
            other => panic!("unknown expect {other:?}"),
        }
    }
}

#[test]
fn an_unknown_key_inside_a_build_block_is_refused_not_dropped() {
    // The `deny_unknown_fields` proof. Without the attribute the field is
    // silently dropped and `curie dev field-parity` stays green, which is
    // exactly the tolerance gap review finding 9 named: a future Python field
    // would vanish on the Rust side with every gate reporting success.
    // REMOVING the attribute makes this test pass, which is how the attribute
    // is proved to be the thing that catches it.
    let document = "connectors:\n  tempo:\n    build:\n      context: connectors/tempo\n      \
                    platforms: [linux/amd64]\n      target: runtime\n";
    assert!(
        connector_build::parse_connectors(document).is_err(),
        "an unmodelled key inside build: must be refused, not dropped"
    );
}

#[test]
fn a_symlinked_dockerfile_leaving_the_context_is_refused() {
    // The textual absolute / `..` check the Python validator performs cannot
    // see this: the declared path is clean and only the filesystem knows the
    // file is a link out of the bundle. `curie build` reads the Dockerfile
    // BEFORE the bundle is packed, so `cli/src/bundle.rs`'s symlink refusal
    // never runs. `resolve_dockerfile` must refuse rather than dereference,
    // for the same reason that packer guard exists.
    let vector = vectors("connector-build-decl.json")
        .into_iter()
        .find(|v| v.get("fixture").is_some())
        .expect("the corpus carries a filesystem fixture");
    let fixture = &vector["fixture"];
    let tmp = scratch();
    let root = tmp.path();
    let bundle = root.join("bundle");
    std::fs::create_dir_all(&bundle).expect("mkdir bundle");
    materialize(root, &fixture["outside_files"]);
    materialize(&bundle, &fixture["bundle_files"]);
    for (link, target) in fixture["bundle_symlinks"]
        .as_object()
        .expect("symlink recipe")
    {
        let path = bundle.join(link);
        std::fs::create_dir_all(path.parent().expect("a parent")).expect("mkdir -p");
        #[cfg(unix)]
        std::os::unix::fs::symlink(target.as_str().expect("a target"), &path).expect("symlink");
    }

    let context = connector_build::resolve_context(&bundle, "connectors/k8s-write")
        .expect("the context itself is inside the bundle");
    assert!(
        connector_build::resolve_dockerfile(&context, "Dockerfile").is_err(),
        "a Dockerfile that resolves through a symlink out of the context must be refused"
    );
}

#[test]
fn a_context_outside_the_bundle_is_refused() {
    // The other half of the containment rule, on the filesystem rather than in
    // the declaration: canonicalization must be what decides, so a symlinked
    // directory cannot walk out of the bundle either.
    let tmp = scratch();
    let root = tmp.path();
    let bundle = root.join("bundle");
    std::fs::create_dir_all(bundle.join("connectors/k8s-write")).expect("mkdir -p");
    std::fs::create_dir_all(root.join("outside")).expect("mkdir -p");

    assert!(connector_build::resolve_context(&bundle, "connectors/k8s-write").is_ok());
    assert!(connector_build::resolve_context(&bundle, "../outside").is_err());
    assert!(connector_build::resolve_context(&bundle, "/etc").is_err());
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(root.join("outside"), bundle.join("escape")).expect("symlink");
        assert!(
            connector_build::resolve_context(&bundle, "escape").is_err(),
            "a symlinked context directory must not walk out of the bundle"
        );
    }
}

// ─── connectors.lock.yaml ────────────────────────────────────────────────────

#[test]
fn the_rust_lock_reader_agrees_with_python_on_every_vector() {
    // `curie cluster deploy` preflights the lock locally so the operator gets
    // an actionable failure instead of an upload rejection, and the platform
    // validates the same file at intake so a git push cannot bypass it. The two
    // halves must agree on which documents are well formed, or one of them is
    // the only gate that ever fires.
    for vector in vectors("connector-lock.json") {
        if vector["lock"].is_null() {
            continue;
        }
        let document = serde_norway::to_string(&vector["lock"]).expect("serialize lock");
        let parsed = connector_build::parse_lock(&document);
        match vector["expect"].as_str().expect("expect") {
            // "raise" documents parse and are refused by apply_lock, which is
            // the Python side's job; the Rust reader only has to accept them.
            "apply" | "raise" => assert!(
                parsed.is_ok(),
                "{} must parse on the Rust side",
                name_of(&vector)
            ),
            "invalid" => assert!(
                parsed.is_err(),
                "{} must be refused by the Rust reader too",
                name_of(&vector)
            ),
            other => panic!("unknown expect {other:?}"),
        }
    }
}

#[test]
fn an_unknown_key_in_a_lock_entry_is_refused_not_dropped() {
    // The `deny_unknown_fields` proof for the lock mirrors. An operator who
    // believes they pinned something they did not is the failure this refuses.
    let document = "version: 1\nconnectors:\n  tempo:\n    image: \
                    ghcr.io/acme-corp/acme-bot-tempo-mcp@sha256:\
                    0000000000000000000000000000000000000000000000000000000000000000\n    \
                    delivery: registry\n    platforms: [linux/amd64]\n    source_digest: \
                    sha256:2222222222222222222222222222222222222222222222222222222222222222\n    \
                    digest: sha256:0000\n";
    assert!(
        connector_build::parse_lock(document).is_err(),
        "an unmodelled key in a lock entry must be refused, not dropped"
    );
}

// ─── The field-name corpus ───────────────────────────────────────────────────

#[test]
fn the_mirror_structs_carry_exactly_the_frozen_field_names() {
    // The mechanism that answers review finding r2-1's exact scenario. Adding a
    // Python field with no vector edit fails the Python suite; editing the
    // vector to make that pass then fails HERE until the mirror gains the
    // field. The loop cannot be exited by touching one language.
    let fields = vector_file("connector-fields.json");
    let expect = |model: &str| -> BTreeSet<String> {
        fields["models"][model]
            .as_array()
            .unwrap_or_else(|| panic!("{model} is listed in the vector"))
            .iter()
            .map(|v| v.as_str().expect("a field name").to_string())
            .collect()
    };

    assert_eq!(connector_build::spec_field_names(), expect("ConnectorSpec"));
    assert_eq!(
        connector_build::build_field_names(),
        expect("ConnectorBuild")
    );
    assert_eq!(
        connector_build::lock_file_field_names(),
        expect("ConnectorLockFile")
    );
    assert_eq!(
        connector_build::lock_entry_field_names(),
        expect("ConnectorLockEntry")
    );
}

// ─── object_name / service_dns: the Docker network alias ─────────────────────

#[test]
fn the_connector_dns_derivation_matches_the_frozen_vector() {
    // This string is the Docker network alias the connector container is
    // started under at the skill and local tiers, and the runner derives the
    // URL it dials from the PYTHON copy of this derivation. A mismatch leaves
    // the runner dialing a name no container owns: a bare connection timeout
    // with nothing logged at either end. The over-63-character truncation
    // branch is in the corpus because that is the half a hand port gets wrong.
    for vector in vectors("connector-service-dns.json") {
        let release = vector["release"].as_str().expect("release");
        let agent = vector["agent"].as_str().expect("agent");
        let connector = vector["connector"].as_str().expect("connector");
        let namespace = vector["namespace"].as_str().expect("namespace");
        assert_eq!(
            connector_build::object_name(release, agent, connector),
            vector["object_name"].as_str().expect("object_name"),
            "{}: object_name",
            name_of(&vector)
        );
        assert_eq!(
            connector_build::service_dns(release, agent, connector, namespace),
            vector["service_dns"].as_str().expect("service_dns"),
            "{}: service_dns",
            name_of(&vector)
        );
    }
}

// ─── source_digest ───────────────────────────────────────────────────────────

#[test]
fn the_source_digest_port_agrees_with_python_on_every_tree() {
    // "A changed source produces a distinct digest" is only a real proof if
    // both lanes compute the same value: the CLI writes the digest into the
    // lock and the platform validator recomputes it from the extracted bundle
    // to refuse a stale one. Two algorithms means every build looks stale on
    // one side of the seam. The corpus covers a nested tree, a `.dockerignore`d
    // file, a bundle-root context carrying `connectors.lock.yaml` and a
    // subdirectory one that hashes its own, a file that differs only in its
    // owner execute bit, and two edits that touch only the declaration.
    let tmp = scratch();
    let root = tmp.path();
    for (i, vector) in vectors("connector-source-digest.json").iter().enumerate() {
        let context = root.join(format!("ctx{i}"));
        std::fs::create_dir_all(&context).expect("mkdir ctx");
        materialize(&context, &vector["tree"]);
        let build: connector_build::ConnectorBuildDecl =
            serde_json::from_value(vector["build"].clone()).expect("parse the build block");
        assert_eq!(
            connector_build::source_digest_of(&context, &build).expect("hash the context"),
            vector["source_digest"].as_str().expect("source_digest"),
            "{}: source_digest",
            name_of(vector)
        );
    }
}

#[test]
fn the_source_digest_relations_hold() {
    // Where the exclusion rules are actually falsified. A port that ignores
    // `.dockerignore` still passes every single-vector assertion by recording
    // whatever it computes; it cannot satisfy both halves of a relation, since
    // one pair must come out equal and another must not.
    let doc = vector_file("connector-source-digest.json");
    let by_name: std::collections::BTreeMap<String, Value> = doc["vectors"]
        .as_array()
        .expect("vectors")
        .iter()
        .map(|v| (name_of(v).to_string(), v.clone()))
        .collect();
    let tmp = scratch();
    let root = tmp.path();

    for (i, relation) in doc["relations"]
        .as_array()
        .expect("relations")
        .iter()
        .enumerate()
    {
        let (names, same) = match relation.get("same") {
            Some(v) => (v, true),
            None => (&relation["distinct"], false),
        };
        let mut digests = Vec::new();
        for (j, name) in names.as_array().expect("a pair").iter().enumerate() {
            let vector = &by_name[name.as_str().expect("a name")];
            let context = root.join(format!("r{i}-{j}"));
            std::fs::create_dir_all(&context).expect("mkdir ctx");
            materialize(&context, &vector["tree"]);
            let build: connector_build::ConnectorBuildDecl =
                serde_json::from_value(vector["build"].clone()).expect("parse the build block");
            digests.push(
                connector_build::source_digest_of(&context, &build).expect("hash the context"),
            );
        }
        let why = relation["why"].as_str().unwrap_or("");
        if same {
            assert_eq!(digests[0], digests[1], "{why}");
        } else {
            assert_ne!(digests[0], digests[1], "{why}");
        }
    }
}

/// The one digest rule the JSON corpus cannot state, since a vector's `tree` is
/// a path-to-content map and cannot express a link.
///
/// Its Python twin is
/// `test_a_symlink_inside_the_context_is_neither_followed_nor_hashed`. Both must
/// hold or the digest stops being one cross-language identity: a bundle with a
/// linked file in its context would report its lock stale on one side of the
/// seam and current on the other. Asserted as an equality against the same tree
/// WITHOUT the links rather than as a frozen literal, so it survives any future
/// change to the stream layout.
#[test]
#[cfg(unix)]
fn a_symlink_inside_the_context_is_neither_followed_nor_hashed() {
    let tmp = scratch();
    let root = tmp.path();
    let outside = root.join("outside");
    std::fs::create_dir_all(outside.join("lib")).expect("mkdir -p");
    std::fs::write(outside.join("secret.env"), "TOKEN=abc\n").expect("write");
    std::fs::write(outside.join("lib/vendored.py"), "VENDORED = 1\n").expect("write");

    let plain = root.join("plain");
    let linked = root.join("linked");
    for context in [&plain, &linked] {
        std::fs::create_dir_all(context).expect("mkdir");
        std::fs::write(context.join("Dockerfile"), "FROM scratch\n").expect("write");
    }
    std::os::unix::fs::symlink(outside.join("secret.env"), linked.join("secret.env"))
        .expect("symlink a file");
    std::os::unix::fs::symlink(outside.join("lib"), linked.join("lib"))
        .expect("symlink a directory");

    let build: connector_build::ConnectorBuildDecl = serde_json::from_value(serde_json::json!({
        "context": "connectors/tempo",
        "dockerfile": "Dockerfile",
        "platforms": ["linux/amd64"],
    }))
    .expect("parse the build block");

    assert_eq!(
        connector_build::source_digest_of(&linked, &build).expect("hash the linked context"),
        connector_build::source_digest_of(&plain, &build).expect("hash the plain context"),
        "a symlink is never followed and never hashed, on both sides of the seam"
    );
}
