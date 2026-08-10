//! Inventory comparator for the versioned-result-schema gate (issue #634).
//!
//! Pure, side-effect-free core of the anti-drift gate, in the same shape as the
//! field-parity (`support/field_parity.rs`) and emit-parity
//! (`support/emit_parity.rs`) comparators: given the text of every `cli/src`
//! file, the parsed `cli/schema/index.json` inventory, and the state of the
//! committed schema files, return every way the inventory has drifted from the
//! source. The gate test (`cli/tests/schema_inventory.rs`) asserts on the
//! returned `Violation` variants; this module never prints or panics on drift.
//!
//! ## What gives it teeth
//!
//! Every `impl CliOutput for T` in the source is an agent-facing `--json` result
//! family. That set is a *syntactic* property a `syn` walk enumerates
//! exhaustively (exactly as `emit_parity`'s `OutputCollector` does), so a new
//! result family that lands without an `index.json` entry is caught as
//! `UndeclaredResult` -- the "a new result family with no schema fails CI"
//! requirement (AC2).
//!
//! ## Deliberately narrower half (mirrored from emit_parity's scope note)
//!
//! A result emitted directly through `Ui::emit_json` (rather than a `CliOutput`)
//! is NOT a syntactic property the walk can attribute to a schema. There are two
//! such sites today (the centralized error emit, and the eval sweep), plus the
//! `emit_json` primitive itself. Those are hand-declared as `builder` entries,
//! and the `raw_emit_sites` allowlist pins the exact per-file count of
//! `.emit_json(` call sites so a NEW direct-emit result trips `UnexpectedRawEmitter`
//! until it is declared. That is the tractable half; discovering which schema a
//! brand-new raw emitter should map to still needs a human, the same honest cost
//! emit_parity documents for its hop.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::Value;
use syn::visit::Visit;

/// A single way the schema inventory has drifted from the source. The gate test
/// matches on these variants and their payload, never on message strings.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Violation {
    /// An `impl CliOutput for T` exists in the source but no `index.json` entry
    /// declares `T`. THE anti-drift teeth: a new result family with no schema.
    UndeclaredResult { result: String },
    /// An `index.json` `CliOutput` entry names a result with no `impl CliOutput`
    /// found anywhere in the scanned sources (the fail-closed twin of the above).
    ResultNotFound { result: String },
    /// An entry's schema file is absent from `cli/schema/`.
    SchemaFileMissing { result: String, schema: String },
    /// An entry's schema file does not compile to a JSON Schema validator.
    SchemaInvalid { result: String, schema: String },
    /// An entry has no usable `version` (a positive integer, or `"N.M"`).
    MissingVersion { result: String },
    /// An entry's `version` disagrees with the version segment of the `$id`.
    VersionMismatch {
        result: String,
        schema: String,
        entry_version: SchemaVersion,
        id_version: SchemaVersion,
    },
    /// An entry is missing a required key (`result`/`kind`/`schema`), so it would
    /// silently escape comparison.
    MalformedManifestEntry { detail: String },
    /// A file's `.emit_json(` call-site count is not what `raw_emit_sites` pins,
    /// so a new direct-emit result would otherwise escape the gate.
    UnexpectedRawEmitter {
        file: String,
        expected: u64,
        found: u64,
    },
}

/// The committed state of one schema file, as the caller observed it on disk.
/// A file missing from the map passed to [`violations`] is treated as absent.
#[derive(Debug, Clone)]
pub struct SchemaState {
    /// `jsonschema::validator_for` accepted it.
    pub compiles: bool,
    /// The version parsed from the `$id`'s version segment, if any.
    pub id_version: Option<SchemaVersion>,
}

/// Collects every `impl (path::)?CliOutput for T` self-type name, including impls
/// nested in module/fn bodies. Mirrors `emit_parity::OutputCollector`.
#[derive(Default)]
struct CliOutputImplCollector {
    names: BTreeSet<String>,
}

impl<'ast> Visit<'ast> for CliOutputImplCollector {
    fn visit_item_impl(&mut self, node: &'ast syn::ItemImpl) {
        if let Some((path, _)) = &node.trait_ {
            let is_cli_output = path
                .segments
                .last()
                .map(|s| s.ident == "CliOutput")
                .unwrap_or(false);
            if is_cli_output {
                if let syn::Type::Path(tp) = node.self_ty.as_ref() {
                    if let Some(seg) = tp.path.segments.last() {
                        self.names.insert(seg.ident.to_string());
                    }
                }
            }
        }
        syn::visit::visit_item_impl(self, node);
    }
}

/// Every `impl CliOutput for T` self-type name found across the given sources.
pub fn cli_output_impls(cli_srcs: &[(&str, &str)]) -> BTreeSet<String> {
    let mut collector = CliOutputImplCollector::default();
    for (_, src) in cli_srcs {
        // A file that fails to parse contributes nothing; a declared entry whose
        // impl only lived there then fails closed via ResultNotFound, the same
        // fail-closed shape the sibling gates rely on.
        if let Ok(file) = syn::parse_file(src) {
            collector.visit_file(&file);
        }
    }
    collector.names
}

/// Parse the `/vN` version segment from a schema `$id` string, e.g.
/// `https://.../cli/kill/v1.json` -> `Some(1)`. The version identity AC1 asks
/// for: it must be present and must agree with the inventory entry's `version`.
/// A schema's two-part version (ADR-0101).
///
/// `v1` and `v1.0` are the SAME version: a bare major is read as minor zero, so
/// no schema had to move when the minor half was introduced. Ordering is
/// derived (major first, then minor) but nothing compares them yet -- the gate
/// checks identity between the `$id` and the index, not precedence.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct SchemaVersion {
    pub major: u64,
    pub minor: u64,
}

impl std::fmt::Display for SchemaVersion {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}.{}", self.major, self.minor)
    }
}

/// Parse `vN` or `vN.M` from the last segment of a schema `$id` (ADR-0101).
///
/// Args:
///   id: the schema's `$id`.
///
/// Returns:
///   The version, or `None` when the segment is not a version at all.
pub fn version_from_id(id: &str) -> Option<SchemaVersion> {
    let last = id.rsplit('/').next()?;
    let stem = last.strip_suffix(".json").unwrap_or(last);
    let digits = stem.strip_prefix('v')?;
    let (major, minor) = match digits.split_once('.') {
        Some((maj, min)) => (maj, min),
        None => (digits, "0"),
    };
    let numeric = |s: &str| -> Option<u64> {
        if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        s.parse().ok()
    };
    Some(SchemaVersion {
        major: numeric(major)?,
        minor: numeric(minor)?,
    })
}

/// Parse an `index.json` `version` value, which ADR-0101 widened from an integer
/// to a `"major.minor"` string. Both spellings are accepted and mean the same
/// thing, so an entry that never changed does not have to be rewritten.
///
/// Args:
///   value: the raw `version` field.
///
/// Returns:
///   The version, or `None` when it is neither a positive integer nor a
///   well-formed version string.
pub fn version_from_entry(value: Option<&Value>) -> Option<SchemaVersion> {
    match value {
        Some(Value::Number(n)) => {
            let major = n.as_u64()?;
            (major > 0).then_some(SchemaVersion { major, minor: 0 })
        }
        Some(Value::String(s)) => {
            let v = version_from_id(&format!("/v{s}.json"))?;
            (v.major > 0).then_some(v)
        }
        _ => None,
    }
}

fn entry_str<'a>(e: &'a Value, key: &str) -> Option<&'a str> {
    e.get(key).and_then(|v| v.as_str())
}

/// Compare the `index.json` inventory against the `impl CliOutput` set found in
/// `cli_srcs` and the committed schema files described by `schemas` (filename ->
/// state; absent from the map == missing on disk). Pure: same inputs, same
/// output, so the fixtures drive it identically to the real tree.
pub fn violations(
    cli_srcs: &[(&str, &str)],
    index: &Value,
    schemas: &BTreeMap<String, SchemaState>,
) -> Vec<Violation> {
    let mut out = Vec::new();

    let impls = cli_output_impls(cli_srcs);

    let empty: Vec<Value> = Vec::new();
    let results: &[Value] = index
        .get("results")
        .and_then(|v| v.as_array())
        .map(Vec::as_slice)
        .unwrap_or(&empty);

    // Declared CliOutput result names (for the two-way impl cross-check).
    let mut declared_cli_output: BTreeSet<String> = BTreeSet::new();

    for entry in results {
        let result = entry_str(entry, "result");
        let kind = entry_str(entry, "kind");
        let schema = entry_str(entry, "schema");
        let (Some(result), Some(kind), Some(schema)) = (result, kind, schema) else {
            out.push(Violation::MalformedManifestEntry {
                detail: format!("results entry missing result/kind/schema: {entry}"),
            });
            continue;
        };

        if kind == "CliOutput" {
            declared_cli_output.insert(result.to_string());
        }

        // Version identity: a usable version that agrees with the schema `$id`.
        let entry_version = version_from_entry(entry.get("version"));
        if entry_version.is_none() {
            out.push(Violation::MissingVersion {
                result: result.to_string(),
            });
        }

        match schemas.get(schema) {
            None => out.push(Violation::SchemaFileMissing {
                result: result.to_string(),
                schema: schema.to_string(),
            }),
            Some(state) => {
                if !state.compiles {
                    out.push(Violation::SchemaInvalid {
                        result: result.to_string(),
                        schema: schema.to_string(),
                    });
                }
                if let (Some(ev), Some(iv)) = (entry_version, state.id_version) {
                    if ev != iv {
                        out.push(Violation::VersionMismatch {
                            result: result.to_string(),
                            schema: schema.to_string(),
                            entry_version: ev,
                            id_version: iv,
                        });
                    }
                }
            }
        }
    }

    // UndeclaredResult: an impl the inventory never declares (THE teeth).
    for name in &impls {
        if !declared_cli_output.contains(name) {
            out.push(Violation::UndeclaredResult {
                result: name.clone(),
            });
        }
    }
    // ResultNotFound: a declared CliOutput entry with no impl in the sources.
    for name in &declared_cli_output {
        if !impls.contains(name) {
            out.push(Violation::ResultNotFound {
                result: name.clone(),
            });
        }
    }

    // Raw-emitter allowlist: pin the exact per-file `.emit_json(` count so a new
    // direct-emit result cannot slip past the CliOutput-only discovery above.
    let expected: BTreeMap<&str, u64> = index
        .get("raw_emit_sites")
        .and_then(|v| v.as_object())
        .map(|o| {
            o.iter()
                .filter_map(|(k, v)| v.as_u64().map(|n| (k.as_str(), n)))
                .collect()
        })
        .unwrap_or_default();
    for (path, src) in cli_srcs {
        // Count call sites, not prose: a full-line comment that merely mentions
        // `.emit_json(` (this module's own doc comments do) must not inflate the
        // count into a false violation.
        let found = src
            .lines()
            .filter(|line| !line.trim_start().starts_with("//"))
            .map(|line| line.matches(".emit_json(").count() as u64)
            .sum();
        let exp = expected.get(path).copied().unwrap_or(0);
        if found != exp {
            out.push(Violation::UnexpectedRawEmitter {
                file: (*path).to_string(),
                expected: exp,
                found,
            });
        }
    }
    // A pinned site that no longer exists (or a path typo) is also drift.
    for (path, exp) in &expected {
        if !cli_srcs.iter().any(|(p, _)| p == path) {
            out.push(Violation::UnexpectedRawEmitter {
                file: (*path).to_string(),
                expected: *exp,
                found: 0,
            });
        }
    }

    out
}
