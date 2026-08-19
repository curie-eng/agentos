//! The CLI's hand mirrors of the connector declaration and lock (ADR 0113).
//!
//! `packages/plugin-format` owns `connectors.yaml` and `connectors.lock.yaml`,
//! but the CLI reads both before the platform ever sees the bundle: `curie
//! build --plugin-dir` builds what the declaration names and writes the lock,
//! and `curie cluster deploy` preflights that lock. Rust cannot import the
//! Pydantic models, so the shapes are mirrored here and the seam is frozen in
//! `tests/vectors/connector-build-decl.json`, `connector-lock.json`,
//! `connector-fields.json`, `connector-service-dns.json` and
//! `connector-source-digest.json` -- every one of them read by both this
//! crate's tests and the Python suite, so a change made in one language and not
//! the other fails that language.
//!
//! Every struct here carries `#[serde(deny_unknown_fields)]`. A tolerant reader
//! would silently drop a key the bundle author wrote, which is how an operator
//! ends up believing they declared something they did not, and it would leave
//! `curie dev field-parity` green while the two languages had already diverged.
//!
//! `plugin_format.validate_bundle` remains the full gate: it enforces every
//! other rule in `validate_connectors` (names, ports, secret paths,
//! placeholders) and the lock's freshness against the extracted tree. What this
//! module mirrors is what the CLI itself must decide locally -- which form a
//! connector declares, whether its build inputs are inside the bundle, and what
//! the source hashes to.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

use anyhow::{anyhow, bail, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// The declaration this module reads, named once (its Python twin is
/// `plugin_format.connectors.CONNECTORS_FILE`).
pub const CONNECTORS_FILE: &str = "connectors.yaml";

/// The lock `curie build` writes, named once (its Python twin is
/// `plugin_format.connector_lock.CONNECTOR_LOCK_FILE`).
pub const CONNECTOR_LOCK_FILE: &str = "connectors.lock.yaml";

/// The only lock shape this build understands.
pub const LOCK_VERSION: u32 = 1;

/// Kubernetes DNS label ceiling and the digest suffix a truncated name keeps.
/// Mirrors `connector_render._DNS_LABEL_MAX` / `_DIGEST_LEN`.
const DNS_LABEL_MAX: usize = 63;
const DIGEST_LEN: usize = 8;

// ─── The declaration ─────────────────────────────────────────────────────────

/// `connectors.yaml` itself.
#[derive(Debug, Default, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectorsFileDecl {
    #[serde(default)]
    pub connectors: BTreeMap<String, ConnectorSpecDecl>,
}

/// One declared connector, a full mirror of `plugin_format.ConnectorSpec`.
#[derive(Debug, Default, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectorSpecDecl {
    // -- hosted form --
    pub image: Option<String>,
    pub build: Option<ConnectorBuildDecl>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    #[serde(default = "default_port")]
    pub port: u32,
    pub unhosted_url: Option<String>,

    // -- remote form --
    pub url: Option<String>,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,

    // -- both --
    #[serde(default)]
    pub secrets: Vec<SecretDecl>,
    #[serde(default)]
    pub sealed_secrets: BTreeMap<String, String>,
    #[serde(default)]
    pub secret_files: BTreeMap<String, String>,
}

/// A declared secret: a bare env var name, or a reference to a Secret someone
/// else provisioned. Both forms exist on the Python side and the CLI only ever
/// carries them through, so this reads either without choosing between them.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(untagged)]
pub enum SecretDecl {
    Name(String),
    Ref {
        name: String,
        from_secret: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        key: Option<String>,
    },
}

/// Where a connector's image comes from, when the bundle carries its source.
#[derive(Debug, Default, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectorBuildDecl {
    pub context: String,
    #[serde(default = "default_dockerfile")]
    pub dockerfile: String,
    #[serde(default)]
    pub platforms: Vec<String>,
}

fn default_port() -> u32 {
    8000
}

fn default_dockerfile() -> String {
    "Dockerfile".to_string()
}

// ─── The lock ────────────────────────────────────────────────────────────────

/// `connectors.lock.yaml` itself.
#[derive(Debug, Default, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectorLockFileDecl {
    pub version: u32,
    #[serde(default)]
    pub connectors: BTreeMap<String, ConnectorLockEntryDecl>,
}

/// The resolved identity of one built connector.
#[derive(Debug, Default, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ConnectorLockEntryDecl {
    pub image: String,
    pub delivery: Delivery,
    #[serde(default)]
    pub platforms: Vec<String>,
    pub source_digest: String,
}

/// How the built image reaches the tier that runs it. Control bearing: it is
/// what the cluster deploy preflight refuses on, so an unmodelled value is
/// rejected loudly rather than degraded to a default (ADR-0036's rule).
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Delivery {
    #[default]
    Registry,
    LocalDaemon,
}

// ─── Field names: the Python/Rust parity corpus ──────────────────────────────

/// The wire field names of one mirror struct.
///
/// Read off a serialized default rather than a hand-typed list, so a field
/// added to the struct shows up here without anyone remembering to update a
/// second copy -- the whole point of `tests/vectors/connector-fields.json` is
/// that neither language can drift alone.
fn field_names<T: Serialize + Default>() -> BTreeSet<String> {
    match serde_json::to_value(T::default()) {
        Ok(serde_json::Value::Object(map)) => map.keys().cloned().collect(),
        _ => BTreeSet::new(),
    }
}

pub fn spec_field_names() -> BTreeSet<String> {
    field_names::<ConnectorSpecDecl>()
}

pub fn build_field_names() -> BTreeSet<String> {
    field_names::<ConnectorBuildDecl>()
}

pub fn lock_file_field_names() -> BTreeSet<String> {
    field_names::<ConnectorLockFileDecl>()
}

pub fn lock_entry_field_names() -> BTreeSet<String> {
    field_names::<ConnectorLockEntryDecl>()
}

// ─── Parsing ─────────────────────────────────────────────────────────────────

/// Parse and check a `connectors.yaml` document.
///
/// Accept/reject parity with the Python validator is the property that matters:
/// a document the CLI accepts and the platform rejects means an operator builds
/// and pushes an image for a bundle that can never deploy, and the reverse
/// means a bundle that deploys through git flow but cannot be built locally.
pub fn parse_connectors(document: &str) -> Result<ConnectorsFileDecl> {
    let file: ConnectorsFileDecl =
        serde_norway::from_str(document).with_context(|| format!("parse {CONNECTORS_FILE}"))?;
    for (name, spec) in &file.connectors {
        check_spec(name, spec)?;
    }
    Ok(file)
}

/// Parse and check a `connectors.lock.yaml` document.
///
/// The version is checked here because it is the only thing that lets a future
/// shape change be refused by an older reader instead of silently misread.
pub fn parse_lock(document: &str) -> Result<ConnectorLockFileDecl> {
    let lock: ConnectorLockFileDecl =
        serde_norway::from_str(document).with_context(|| format!("parse {CONNECTOR_LOCK_FILE}"))?;
    if lock.version != LOCK_VERSION {
        bail!(
            "{CONNECTOR_LOCK_FILE}: version {} is not a shape this build understands \
             (expected {LOCK_VERSION})",
            lock.version
        );
    }
    Ok(lock)
}

/// Read a bundle's `connectors.yaml`, or an empty declaration when it has none.
pub fn load(bundle_dir: &Path) -> Result<ConnectorsFileDecl> {
    let path = bundle_dir.join(CONNECTORS_FILE);
    if !path.is_file() {
        return Ok(ConnectorsFileDecl::default());
    }
    let body =
        std::fs::read_to_string(&path).with_context(|| format!("read {}", path.display()))?;
    parse_connectors(&body)
}

/// Read a bundle's `connectors.lock.yaml`, or `None` when it has none.
///
/// `None` is the common case and not an error: an ordinary `image:` bundle
/// carries no lock and never will.
pub fn load_lock(bundle_dir: &Path) -> Result<Option<ConnectorLockFileDecl>> {
    let path = bundle_dir.join(CONNECTOR_LOCK_FILE);
    if !path.is_file() {
        return Ok(None);
    }
    let body =
        std::fs::read_to_string(&path).with_context(|| format!("read {}", path.display()))?;
    parse_lock(&body).map(Some)
}

fn check_spec(name: &str, spec: &ConnectorSpecDecl) -> Result<()> {
    let forms = [
        spec.image.is_some(),
        spec.build.is_some(),
        spec.url.is_some(),
    ];
    let declared = forms.iter().filter(|set| **set).count();
    if declared > 1 {
        bail!(
            "connectors.{name}: set exactly one of `image`, `build` or `url` -- otherwise it \
             is unclear who owns the process"
        );
    }
    if declared == 0 {
        bail!(
            "connectors.{name}: set `image` for Curie to run it, `build` for Curie to build it \
             from source in this bundle, or `url` to point at something already running"
        );
    }
    // Keyed to hosted-ness, not to `image`: a built connector is equally hosted,
    // so a remote-only field must not ride the build form.
    if (spec.image.is_some() || spec.build.is_some()) && !spec.headers.is_empty() {
        bail!(
            "connectors.{name}: `headers` apply to a remote endpoint; configure a hosted \
             connector with `env` and `args` instead"
        );
    }
    if let Some(build) = &spec.build {
        check_build(name, build)?;
    }
    Ok(())
}

fn check_build(name: &str, build: &ConnectorBuildDecl) -> Result<()> {
    if escapes(&build.context) {
        bail!(
            "connectors.{name}: `build.context` is {:?}; it must be a path inside the bundle",
            build.context
        );
    }
    if escapes(&build.dockerfile) {
        bail!(
            "connectors.{name}: `build.dockerfile` is {:?}; it must be a path inside the build \
             context",
            build.dockerfile
        );
    }
    if build.platforms.is_empty() {
        bail!(
            "connectors.{name}: `build.platforms` is empty. A silently single-arch build fails \
             after apply as `no matching manifest`, so the target set is stated, never guessed"
        );
    }
    for platform in &build.platforms {
        if !is_platform(platform) {
            bail!(
                "connectors.{name}: `{platform}` is not an OCI platform. Use `os/arch` or \
                 `os/arch/variant`, such as `linux/amd64` or `linux/arm/v7`"
            );
        }
    }
    Ok(())
}

/// `os/arch`, optionally `os/arch/variant`. Mirrors `connectors._PLATFORM_RE`.
fn is_platform(platform: &str) -> bool {
    let parts: Vec<&str> = platform.split('/').collect();
    let alnum = |s: &&str| {
        !s.is_empty()
            && s.chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
    };
    match parts.as_slice() {
        [os, arch] => alnum(os) && alnum(arch),
        [os, arch, variant] => {
            alnum(os)
                && alnum(arch)
                && variant.starts_with('v')
                && variant.len() > 1
                && variant[1..].chars().all(|c| c.is_ascii_digit())
        }
        _ => false,
    }
}

/// Is this bundle-relative path empty, absolute, or outside its root?
///
/// Textual, the same check the Python validator performs on a declaration it
/// may be reading without a tree. The resolvers below add what only a
/// filesystem can answer.
fn escapes(path: &str) -> bool {
    if path.trim().is_empty() {
        return true;
    }
    let mut depth: i32 = 0;
    for component in Path::new(path).components() {
        match component {
            Component::RootDir | Component::Prefix(_) => return true,
            Component::ParentDir => {
                depth -= 1;
                if depth < 0 {
                    return true;
                }
            }
            Component::CurDir => {}
            Component::Normal(_) => depth += 1,
        }
    }
    false
}

// ─── Path resolution ─────────────────────────────────────────────────────────

/// The canonical build context, required to sit inside the bundle.
///
/// ADR 0113 requires a build context constrained to the extracted bundle
/// "without accepting arbitrary host paths". Canonicalization is what decides,
/// so a symlinked directory cannot walk out of the bundle either -- and a
/// symlink is refused rather than dereferenced, the same rule
/// `cli/src/bundle.rs`'s packer applies for the same reason.
pub fn resolve_context(bundle_root: &Path, context: &str) -> Result<PathBuf> {
    if escapes(context) {
        bail!("build context {context:?} must be a path inside the bundle");
    }
    let root = bundle_root
        .canonicalize()
        .with_context(|| format!("resolve bundle root {}", bundle_root.display()))?;
    let candidate = root.join(context);
    refuse_symlink(&candidate)?;
    let resolved = candidate
        .canonicalize()
        .with_context(|| format!("resolve build context {}", candidate.display()))?;
    if !resolved.starts_with(&root) {
        bail!(
            "build context {context:?} resolves to {} which is outside the bundle",
            resolved.display()
        );
    }
    Ok(resolved)
}

/// The canonical Dockerfile, required to sit inside the canonical context.
///
/// The textual check alone is not enough here: a bundle can ship
/// `connectors/x/Dockerfile` as a symlink to a host file, and `curie build`
/// reads that Dockerfile BEFORE the bundle is packed, so the packer's symlink
/// refusal never runs.
pub fn resolve_dockerfile(context_canonical: &Path, dockerfile: &str) -> Result<PathBuf> {
    if escapes(dockerfile) {
        bail!("dockerfile {dockerfile:?} must be a path inside the build context");
    }
    let candidate = context_canonical.join(dockerfile);
    refuse_symlink(&candidate)?;
    let resolved = candidate
        .canonicalize()
        .with_context(|| format!("resolve dockerfile {}", candidate.display()))?;
    if !resolved.starts_with(context_canonical) {
        bail!(
            "dockerfile {dockerfile:?} resolves to {} which is outside the build context",
            resolved.display()
        );
    }
    Ok(resolved)
}

fn refuse_symlink(path: &Path) -> Result<()> {
    if let Ok(meta) = std::fs::symlink_metadata(path) {
        if meta.file_type().is_symlink() {
            bail!(
                "{} is a symlink; Curie refuses to follow one out of the bundle rather than \
                 build whatever it points at",
                path.display()
            );
        }
    }
    Ok(())
}

// ─── object_name / service_dns: the Docker network alias ─────────────────────

/// The Kubernetes object name for a connector, and the Docker network alias the
/// same connector is started under at the skill and local tiers.
///
/// A hand port of `connector_render.object_name`, frozen against
/// `tests/vectors/connector-service-dns.json`, because the runner derives the
/// URL it dials from the PYTHON copy: a mismatch leaves it dialing a name no
/// container owns, which surfaces as a bare connection timeout.
pub fn object_name(release: &str, agent: &str, connector: &str) -> String {
    let base = format!("{release}-{agent}-mcp-{connector}");
    if base.len() <= DNS_LABEL_MAX {
        return base;
    }
    // Truncate WITH a digest of the full name: clipping alone maps two long
    // names sharing a prefix onto one object.
    let digest = hex(Sha256::digest(base.as_bytes()).as_slice());
    let keep = DNS_LABEL_MAX - DIGEST_LEN - 1;
    let head = base[..keep].trim_end_matches('-');
    format!("{head}-{}", &digest[..DIGEST_LEN])
}

/// The in-cluster DNS name of a connector's Service.
pub fn service_dns(release: &str, agent: &str, connector: &str, namespace: &str) -> String {
    format!(
        "{}.{namespace}.svc.cluster.local",
        object_name(release, agent, connector)
    )
}

// ─── source_digest ───────────────────────────────────────────────────────────

/// The content-derived identity of a build input.
///
/// A port of `plugin_format.connector_lock.source_digest_of`, frozen against
/// `tests/vectors/connector-source-digest.json`, whose `comment` states the
/// algorithm in full and why each rule is there. The CLI writes this value into
/// the lock and the platform validator recomputes it from the extracted bundle
/// to refuse a stale one, so two algorithms would make every build look stale on
/// one side of the seam.
///
/// Content plus ONE bit of mode per file, the owner execute bit: the build
/// context tar carries each file's mode and BuildKit keys its cache on it, so an
/// exec-bit flip on an entrypoint is a changed build input.
pub fn source_digest_of(context_dir: &Path, build: &ConnectorBuildDecl) -> Result<String> {
    let rules = dockerignore_rules(context_dir, &build.dockerfile)?;
    let mut included: Vec<String> = Vec::new();
    collect_files(
        context_dir,
        context_dir,
        &rules,
        is_bundle_root_context(&build.context),
        &mut included,
    )?;
    included.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));

    let mut stream: Vec<u8> = Vec::new();
    for relative in &included {
        let path = context_dir.join(relative);
        let bytes = std::fs::read(&path).with_context(|| format!("read {relative}"))?;
        stream.extend_from_slice(relative.as_bytes());
        stream.push(0);
        stream.extend_from_slice(hex(Sha256::digest(&bytes).as_slice()).as_bytes());
        stream.push(0);
        stream.push(if is_owner_executable(&path)? {
            b'1'
        } else {
            b'0'
        });
        stream.push(b'\n');
    }
    // The declared build block, so editing `platforms` or `dockerfile` alone
    // still invalidates the lock. Keys sorted, no whitespace, platforms in
    // DECLARED order -- a lane that sorted them would split the two digests.
    let canonical = format!(
        "{{\"context\":{},\"dockerfile\":{},\"platforms\":[{}]}}",
        json_string(&build.context),
        json_string(&build.dockerfile),
        build
            .platforms
            .iter()
            .map(|p| json_string(p))
            .collect::<Vec<_>>()
            .join(",")
    );
    stream.extend_from_slice(b"build\0");
    stream.extend_from_slice(canonical.as_bytes());
    stream.push(b'\n');

    Ok(format!(
        "sha256:{}",
        hex(Sha256::digest(&stream).as_slice())
    ))
}

/// Does the declared `build.context` name the bundle root?
///
/// Decided from the DECLARATION, not the tree: `curie build` writes the
/// generated `connectors.lock.yaml` at the bundle root only, so a
/// `connectors.lock.yaml` is this context's own generated file exactly when the
/// declared context names that root. The Python twin normalizes identically --
/// strip a trailing `/`, then `.` and the empty string are the root.
fn is_bundle_root_context(context: &str) -> bool {
    matches!(context.trim_end_matches('/'), "." | "")
}

/// Is this file executable by its owner? The one bit of mode the digest carries.
///
/// Docker's build context tar carries each file's mode and BuildKit keys its
/// cache on it, so flipping the exec bit on an entrypoint changes the build
/// input while the bytes stay put.
#[cfg(unix)]
fn is_owner_executable(path: &Path) -> Result<bool> {
    use std::os::unix::fs::PermissionsExt;

    let mode = std::fs::metadata(path)
        .with_context(|| format!("stat {}", path.display()))?
        .permissions()
        .mode();
    Ok(mode & 0o100 != 0)
}

/// Off-unix there is no owner execute bit to read, so every file hashes as
/// non-executable -- the same normalization docker itself applies on Windows,
/// where the context tar is written with a fixed mode.
#[cfg(not(unix))]
fn is_owner_executable(_path: &Path) -> Result<bool> {
    Ok(false)
}

fn json_string(value: &str) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| String::from("\"\""))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// One `.dockerignore` line: the pattern, and whether it re-includes.
struct IgnoreRule {
    negated: bool,
    pattern: String,
}

/// The context's own `.dockerignore` exclusion set, in file order, or an empty
/// one.
///
/// Docker's file, so honoring it means the digest covers exactly what the
/// daemon receives. `.curieignore` governs bundle packing, a different
/// question, and is not consulted.
///
/// Order is load-bearing: `*` followed by `!Dockerfile` and `!Dockerfile`
/// followed by `*` are different exclusion sets, and the last matching rule is
/// the one that decides. The two trailing rules mirror the docker CLI's own
/// `TrimBuildFilesFromExcludes`, which puts the Dockerfile and `.dockerignore`
/// back into the context however the patterns read.
fn dockerignore_rules(context_dir: &Path, dockerfile: &str) -> Result<Vec<IgnoreRule>> {
    let path = context_dir.join(".dockerignore");
    if !path.is_file() {
        return Ok(Vec::new());
    }
    let body =
        std::fs::read_to_string(&path).with_context(|| format!("read {}", path.display()))?;
    let mut rules: Vec<IgnoreRule> = Vec::new();
    for line in body.lines().map(str::trim) {
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        match line.strip_prefix('!') {
            // The `!` is removed and the remainder used verbatim, as Docker
            // does; a bare `!` names nothing and is skipped rather than being
            // read as a re-include of everything.
            Some("") => continue,
            Some(remainder) => rules.push(IgnoreRule {
                negated: true,
                pattern: remainder.to_string(),
            }),
            None => rules.push(IgnoreRule {
                negated: false,
                pattern: line.to_string(),
            }),
        }
    }
    for build_file in [".dockerignore", dockerfile] {
        if is_excluded(&rules, build_file) {
            rules.push(IgnoreRule {
                negated: true,
                pattern: build_file.to_string(),
            });
        }
    }
    Ok(rules)
}

fn collect_files(
    root: &Path,
    dir: &Path,
    rules: &[IgnoreRule],
    exclude_lock: bool,
    out: &mut Vec<String>,
) -> Result<()> {
    for entry in std::fs::read_dir(dir).with_context(|| format!("read {}", dir.display()))? {
        let entry = entry?;
        let path = entry.path();
        let kind = entry.file_type()?;
        // A symlink is never followed and never hashed; the context resolver
        // refuses one before hashing begins.
        if kind.is_symlink() {
            continue;
        }
        let relative = path
            .strip_prefix(root)
            .map_err(|_| anyhow!("{} is outside {}", path.display(), root.display()))?
            .to_string_lossy()
            .replace('\\', "/");
        if kind.is_dir() {
            // Descended even when the directory itself matches an exclusion: a
            // later `!` can re-include a file beneath it, which is exactly what
            // Docker does once the ignore set carries any exclusion. The
            // per-file check below evaluates the same ancestor paths, so
            // pruning here would only ever LOSE a re-included file.
            collect_files(root, &path, rules, exclude_lock, out)?;
            continue;
        }
        // Only the context's OWN generated lock: `curie build` writes it at the
        // BUNDLE root, so it is skipped only when the declared context IS that
        // root. Under a subdirectory context a `connectors.lock.yaml` at the top
        // is authored input the daemon receives, and so is one nested deeper
        // under any context.
        if exclude_lock && relative == CONNECTOR_LOCK_FILE {
            continue;
        }
        if is_excluded(rules, &relative) {
            continue;
        }
        out.push(relative);
    }
    Ok(())
}

/// Is this path excluded once every rule has had its say, in file order?
///
/// Docker's rule, not "the first exclusion wins": every rule is evaluated and
/// the last one that matches decides, so a later `!` re-includes what an earlier
/// pattern excluded. Each rule is matched segment by segment against the path
/// and each of its ancestor directory paths, so `*` and `?` never cross a `/`
/// and a pattern naming a directory excludes everything beneath it.
fn is_excluded(rules: &[IgnoreRule], relative: &str) -> bool {
    let segments: Vec<&str> = relative.split('/').collect();
    let candidates: Vec<String> = (0..segments.len())
        .map(|i| segments[..=i].join("/"))
        .collect();
    let mut excluded = false;
    for rule in rules {
        if matches_any(&rule.pattern, &candidates) {
            excluded = !rule.negated;
        }
    }
    excluded
}

fn matches_any(pattern: &str, candidates: &[String]) -> bool {
    let parts: Vec<&str> = pattern.split('/').collect();
    for candidate in candidates {
        let against: Vec<&str> = candidate.split('/').collect();
        if parts.len() != against.len() {
            continue;
        }
        if parts
            .iter()
            .zip(against.iter())
            .all(|(p, a)| matches_segment(p, a))
        {
            return true;
        }
    }
    false
}

/// One path segment against one glob segment: `*`, `?` and `[...]` classes,
/// matching Python's `fnmatch.fnmatchcase` on the other side of the seam.
fn matches_segment(pattern: &str, name: &str) -> bool {
    let p: Vec<char> = pattern.chars().collect();
    let n: Vec<char> = name.chars().collect();
    glob_match(&p, &n)
}

fn glob_match(pattern: &[char], name: &[char]) -> bool {
    if pattern.is_empty() {
        return name.is_empty();
    }
    match pattern[0] {
        '*' => {
            for split in 0..=name.len() {
                if glob_match(&pattern[1..], &name[split..]) {
                    return true;
                }
            }
            false
        }
        '?' => !name.is_empty() && glob_match(&pattern[1..], &name[1..]),
        '[' => {
            let Some(close) = pattern.iter().position(|c| *c == ']').filter(|i| *i > 1) else {
                // An unterminated class is a literal `[`, as fnmatch reads it.
                return !name.is_empty() && name[0] == '[' && glob_match(&pattern[1..], &name[1..]);
            };
            if name.is_empty() {
                return false;
            }
            let mut class = &pattern[1..close];
            let negated = matches!(class.first(), Some('!'));
            if negated {
                class = &class[1..];
            }
            let hit = class_matches(class, name[0]);
            (hit != negated) && glob_match(&pattern[close + 1..], &name[1..])
        }
        literal => !name.is_empty() && name[0] == literal && glob_match(&pattern[1..], &name[1..]),
    }
}

fn class_matches(class: &[char], candidate: char) -> bool {
    let mut i = 0;
    while i < class.len() {
        if i + 2 < class.len() && class[i + 1] == '-' {
            if class[i] <= candidate && candidate <= class[i + 2] {
                return true;
            }
            i += 3;
            continue;
        }
        if class[i] == candidate {
            return true;
        }
        i += 1;
    }
    false
}
