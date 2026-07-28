//! Issue #1040: `curie <tier> info` -- the resolved bundle plus discovery
//! diagnostics.
//!
//! Written test-first. The public seam these drive (`curie::info::{BundleView,
//! BundleOrigin, discover, InfoReport, InfoOutput, ...}`) does not exist yet, so
//! this binary fails to compile until the Implementer adds `cli/src/info.rs`.
//! That RED state is the contract handoff, exactly as `cli/tests/skill_check.rs`
//! and `cli/tests/exit_codes.rs` were handed off.
//!
//! Two properties carry most of the weight here, and both are stated as
//! assertions that FAIL against a plausible-looking wrong implementation:
//!
//! 1. **A sentinel, never an omission or an empty collection.** An absent
//!    concept is `{available:false, reason, where}` and an unresolvable one is
//!    `{resolved:false, reason}`. `null`, a missing key, or `[]` where a
//!    rejection belongs is the exact lie ADR-0041 exists to kill, so every
//!    negative test asserts the sentinel's SHAPE rather than mere presence.
//! 2. **One discovery pass.** The same bundle read from disk and read as a
//!    deployed file list must yield the same diagnostics and the same inventory
//!    blocks (test 13). That turns "one shared pass" from a claim into a checked
//!    property.
//!
//! The committed `examples/` bundles are READ-ONLY for this whole suite. Every
//! mutation runs against a tempdir copy taken by `fixture()`; `committed_examples_are_unmutated`
//! is the tripwire for a future test that forgets.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use curie::info::{discover, BundleOrigin, BundleView};

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn examples_root() -> PathBuf {
    PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/../examples"))
}

fn output_text(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned() + &String::from_utf8_lossy(&output.stderr)
}

/// Env names whose ambient host value would silently change what `info`
/// resolves. Cleared on every child so a developer's shell cannot green (or red)
/// a test that is about the bundle, not about their exports.
const AMBIENT_TO_CLEAR: &[&str] = &[
    "CURIE_API_URL",
    "CURIE_API_KEY",
    "CURIE_MODEL",
    "CURIE_CREDENTIALS",
    "CURIE_MODEL_CREDENTIALS",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
];

/// Run the real binary. `cwd` is always a tempdir so the repo-root `.env`
/// dotenvy load cannot leak a credential into the child.
fn run_curie(args: &[&str], cwd: &Path, env: &[(&str, &str)]) -> Output {
    let mut cmd = Command::new(bin());
    cmd.args(args).current_dir(cwd);
    for name in AMBIENT_TO_CLEAR {
        cmd.env_remove(name);
    }
    for (name, value) in env {
        cmd.env(name, value);
    }
    cmd.output().expect("run curie")
}

/// `curie --json skill info --plugin-dir <dir>` against a fixture copy.
fn run_skill_info(dir: &Path, extra: &[&str], env: &[(&str, &str)]) -> Output {
    let dir_s = dir.to_str().expect("fixture path is utf-8").to_string();
    let mut args: Vec<&str> = vec!["--json", "skill", "info", "--plugin-dir", &dir_s];
    args.extend_from_slice(extra);
    run_curie(&args, dir, env)
}

fn json_stdout(output: &Output) -> serde_json::Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|e| {
        panic!(
            "stdout must be exactly one JSON object (issue #456: never empty stdout under --json): {e}\n{}",
            output_text(output)
        )
    })
}

fn assert_exit(output: &Output, code: i32, what: &str) {
    assert_eq!(
        output.status.code(),
        Some(code),
        "{what} must exit {code}\n{}",
        output_text(output)
    );
}

/// Copy an `examples/` bundle into a fresh tempdir so a test can mutate it.
/// The committed example is read-only for the whole suite.
fn fixture(example: &str) -> tempfile::TempDir {
    let src = examples_root().join(example);
    assert!(
        src.is_dir(),
        "committed example {} must exist",
        src.display()
    );
    let dir = tempfile::tempdir().expect("tempdir");
    copy_dir_all(&src, dir.path());
    dir
}

fn copy_dir_all(src: &Path, dst: &Path) {
    std::fs::create_dir_all(dst).expect("create fixture dir");
    for entry in std::fs::read_dir(src).expect("read example dir") {
        let entry = entry.expect("dir entry");
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if entry.file_type().expect("file type").is_dir() {
            copy_dir_all(&from, &to);
        } else {
            std::fs::copy(&from, &to).expect("copy fixture file");
        }
    }
}

/// A scratch cwd for the tier tests, which target no bundle directory.
fn scratch() -> tempfile::TempDir {
    tempfile::tempdir().expect("tempdir")
}

// ---------------------------------------------------------------------------
// Payload assertions: the SHAPE of a sentinel, never mere presence
// ---------------------------------------------------------------------------

/// `{available: false, reason, where}` -- the concept does not exist at this
/// tier. Fails against `null`, against an omitted key, and against the OTHER
/// sentinel, which is the whole point of asserting shape rather than presence.
fn assert_unavailable(value: &serde_json::Value, what: &str) {
    let obj = value
        .as_object()
        .unwrap_or_else(|| panic!("{what} must be the `unavailable` object, got: {value}"));
    assert_eq!(
        obj.get("available"),
        Some(&serde_json::Value::Bool(false)),
        "{what} must carry `available: false` -- a null or an omitted key reads as \
         \"not applicable\" to an agent, which is the omission ADR-0041 forbids: {value}"
    );
    let reason = obj
        .get("reason")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_else(|| panic!("{what} must carry a string `reason`: {value}"));
    assert!(
        !reason.trim().is_empty(),
        "{what}'s `reason` must say WHY the concept has no meaning here, not be empty: {value}"
    );
    let where_it_lives = obj
        .get("where")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_else(|| panic!("{what} must carry a string `where`: {value}"));
    assert!(
        !where_it_lives.trim().is_empty(),
        "{what}'s `where` must name the tier or command that HAS the concept: {value}"
    );
    assert!(
        !obj.contains_key("resolved"),
        "the two sentinels are distinct facts; `unavailable` must never carry `resolved`: {value}"
    );
}

/// `{resolved: false, reason}` -- the concept exists here, but this bundle's
/// state blocked resolving it.
fn assert_unresolved(value: &serde_json::Value, what: &str) {
    let obj = value
        .as_object()
        .unwrap_or_else(|| panic!("{what} must be the `unresolved` object, got: {value}"));
    assert_eq!(
        obj.get("resolved"),
        Some(&serde_json::Value::Bool(false)),
        "{what} must carry `resolved: false`: {value}"
    );
    let reason = obj
        .get("reason")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_else(|| panic!("{what} must carry a string `reason`: {value}"));
    assert!(
        !reason.trim().is_empty(),
        "{what}'s `reason` must name what blocked resolution: {value}"
    );
    assert!(
        !obj.contains_key("available"),
        "the two sentinels are distinct facts; `unresolved` must never carry `available`: {value}"
    );
}

fn is_unresolved(value: &serde_json::Value) -> bool {
    value.get("resolved") == Some(&serde_json::Value::Bool(false))
}

fn diagnostics(report: &serde_json::Value) -> &Vec<serde_json::Value> {
    report["diagnostics"]
        .as_array()
        .unwrap_or_else(|| panic!("`diagnostics` must always be an array: {report}"))
}

fn codes(report: &serde_json::Value) -> Vec<String> {
    diagnostics(report)
        .iter()
        .filter_map(|d| d["code"].as_str().map(str::to_string))
        .collect()
}

/// The one diagnostic carrying `code`. Panics (with the whole payload) when the
/// implementation emitted none, which is the failure these negative tests exist
/// to produce.
fn diagnostic<'a>(report: &'a serde_json::Value, code: &str) -> &'a serde_json::Value {
    diagnostics(report)
        .iter()
        .find(|d| d["code"] == serde_json::json!(code))
        .unwrap_or_else(|| {
            panic!(
                "expected a diagnostic with code {code:?}; got codes {:?} in {report}",
                codes(report)
            )
        })
}

fn looked_in(diag: &serde_json::Value) -> Vec<String> {
    diag["looked_in"]
        .as_array()
        .unwrap_or_else(|| panic!("`looked_in` must be an array of probed paths: {diag}"))
        .iter()
        .filter_map(|v| v.as_str().map(str::to_string))
        .collect()
}

/// Every diagnostic carries all seven keys, its `kind` is one of the ten closed
/// values, and its `code` is prefixed by its `kind` (the coarse-closed /
/// fine-open split, ADR contract). Run on every payload a test parses so a
/// malformed diagnostic cannot slip through a test that only looked at one code.
const DIAGNOSTIC_KINDS: &[&str] = &[
    "manifest",
    "skill",
    "mcp",
    "evals",
    "secret",
    "boot_env",
    "approval_gate",
    "artifact",
    "state",
    "deployed",
];

fn assert_diagnostics_well_formed(report: &serde_json::Value) {
    for diag in diagnostics(report) {
        for key in [
            "code",
            "kind",
            "candidate",
            "looked_for",
            "looked_in",
            "reason",
            "fix",
        ] {
            assert!(
                diag.get(key).is_some(),
                "every diagnostic carries all seven keys; {key:?} is missing from {diag}"
            );
        }
        let kind = diag["kind"]
            .as_str()
            .unwrap_or_else(|| panic!("`kind` must be a string: {diag}"));
        assert!(
            DIAGNOSTIC_KINDS.contains(&kind),
            "`kind` is a CLOSED enum a consumer switches on; {kind:?} is not one of {DIAGNOSTIC_KINDS:?}: {diag}"
        );
        let code = diag["code"]
            .as_str()
            .unwrap_or_else(|| panic!("`code` must be a string: {diag}"));
        // The prefix rule is what lets a consumer branch on `kind` alone and
        // still recover the family of an unknown `code`.
        let prefix = code.split('.').next().unwrap_or("");
        assert!(
            prefix == kind || (kind == "skill" && prefix == "skills"),
            "a diagnostic's `code` prefix must equal its `kind`; got code {code:?} under kind {kind:?}"
        );
        let reason = diag["reason"].as_str().unwrap_or("");
        assert!(
            !reason.trim().is_empty(),
            "a rejection with no reason is not a diagnosis: {diag}"
        );
    }
}

// ---------------------------------------------------------------------------
// Building a deployed view from a real bundle, with no network
// ---------------------------------------------------------------------------

/// Walk a bundle directory and produce the `Vec<(path, content)>` shape
/// `ApiClient::bundle_files` returns: root-relative, forward-slashed paths with
/// file contents. Real data through the real pass -- no `ApiClient`, no HTTP, and
/// no mock of an internal module.
fn deployed_files(root: &Path) -> Vec<(String, String)> {
    let mut out = Vec::new();
    collect_files(root, root, &mut out);
    out.sort();
    out
}

fn collect_files(root: &Path, dir: &Path, out: &mut Vec<(String, String)>) {
    for entry in std::fs::read_dir(dir).expect("read bundle dir") {
        let entry = entry.expect("dir entry");
        let path = entry.path();
        let name = entry.file_name();
        // `bundle::pack_tar_gz` skips these, so a stored bundle never carries
        // them; a deployed view that included them would not be the same data.
        if matches!(
            name.to_string_lossy().as_ref(),
            ".git" | ".curie" | ".venv" | "node_modules" | "__pycache__"
        ) {
            continue;
        }
        if entry.file_type().expect("file type").is_dir() {
            collect_files(root, &path, out);
        } else if let Ok(content) = std::fs::read_to_string(&path) {
            let rel = path
                .strip_prefix(root)
                .expect("path under root")
                .to_string_lossy()
                .replace('\\', "/");
            out.push((rel, content));
        }
    }
}

fn deployed_origin() -> BundleOrigin {
    BundleOrigin::Deployed {
        agent: "demo".to_string(),
        agent_id: "a_1".to_string(),
        version_id: "ver_1".to_string(),
        version_label: "v1".to_string(),
        environment: "prod".to_string(),
        bundle_sha256: Some("abc123".to_string()),
        channel: "C0DEMO".to_string(),
    }
}

fn deployed_report(root: &Path) -> serde_json::Value {
    let view = BundleView::from_files(deployed_origin(), deployed_files(root));
    serde_json::to_value(discover(&view)).expect("InfoReport serializes")
}

fn disk_report(root: &Path) -> serde_json::Value {
    let view = BundleView::from_disk(root).expect("from_disk over a real bundle");
    serde_json::to_value(discover(&view)).expect("InfoReport serializes")
}

/// A minimal in-memory bundle: a valid manifest plus whatever the test adds.
fn synthetic(files: &[(&str, &str)]) -> serde_json::Value {
    let mut all: Vec<(String, String)> = vec![(
        ".claude-plugin/plugin.json".to_string(),
        r#"{"name": "synthetic", "version": "0.1.0"}"#.to_string(),
    )];
    for (path, content) in files {
        all.push(((*path).to_string(), (*content).to_string()));
    }
    let view = BundleView::from_files(deployed_origin(), all);
    serde_json::to_value(discover(&view)).expect("InfoReport serializes")
}

const VALID_FRONTMATTER: &str = "---\nname: sample\ndescription: A sample skill for the discovery pass.\nallowed-tools:\n  - WebSearch\n---\n\n# Sample\n";

// ===========================================================================
// 1-14: AC-driving integration tests against the real binary
// ===========================================================================

/// AC1. The happy path: a clean bundle resolves its skill and its eval suite,
/// and exits 0.
#[test]
fn info_json_exits_zero_and_reports_the_weather_skill_and_eval_count() {
    let fx = fixture("weather");
    let out = run_skill_info(fx.path(), &[], &[]);
    assert_exit(&out, 0, "skill info against a clean bundle");

    let v = json_stdout(&out);
    assert_diagnostics_well_formed(&v);
    assert_eq!(v["tier"], "skill", "{v}");

    let skills = v["skills"]
        .as_array()
        .unwrap_or_else(|| panic!("a resolvable skills tree is an array, got: {v}"));
    assert_eq!(skills.len(), 1, "weather declares exactly one skill: {v}");
    assert_eq!(skills[0]["name"], "weather", "{v}");
    let path = skills[0]["path"].as_str().unwrap_or_else(|| panic!("{v}"));
    assert!(
        path.ends_with("skills/weather/SKILL.md"),
        "the skill's path must name the SKILL.md that registered it, got {path:?}"
    );

    // AC1's second half: the eval case count, read from the bundle's own
    // evals/cases.json (never a different bundle's suite).
    assert_eq!(
        v["evals"]["case_count"], 1,
        "examples/weather ships exactly one eval case: {v}"
    );
}

/// AC1's MCP half against the bundle that actually declares a server.
#[test]
fn info_json_lists_a_declared_mcp_server() {
    let fx = fixture("github-issues");
    let out = run_skill_info(fx.path(), &[], &[]);
    assert_exit(&out, 0, "skill info against github-issues");

    let v = json_stdout(&out);
    assert_diagnostics_well_formed(&v);
    let servers = v["mcp_servers"]
        .as_array()
        .unwrap_or_else(|| panic!("a readable .mcp.json resolves to an array, got: {v}"));
    let github = servers
        .iter()
        .find(|s| s["name"] == "github")
        .unwrap_or_else(|| panic!("the declared `github` server must be listed: {v}"));

    assert_eq!(github["source"], ".mcp.json", "{v}");
    assert_eq!(
        github["authed"], true,
        "the github server carries an env block, so it is authed: {v}"
    );
    // #337's rule: a server that was never probed must SAY so, never imply
    // "loads" by omission.
    assert_eq!(
        github["load"], "not_probed",
        "static info never fabricates a load verdict: {v}"
    );
    assert!(
        codes(&v).iter().any(|c| c == "mcp.not_probed"),
        "the not-probed state must also name the way to find out: {v}"
    );

    // The security boundary, asserted structurally: an MCP row carries EXACTLY
    // the reviewed `DeclaredServer` field set plus `load`. A new key here (env,
    // headers, args, url, command) is a value-leak vector, so the assertion is
    // an exact key-set equality rather than an absence spot-check.
    let keys: std::collections::BTreeSet<&str> = github
        .as_object()
        .expect("an mcp row is an object")
        .keys()
        .map(String::as_str)
        .collect();
    let expected: std::collections::BTreeSet<&str> = ["name", "source", "form", "authed", "load"]
        .into_iter()
        .collect();
    assert_eq!(
        keys, expected,
        "an mcp_servers row must carry exactly the reviewed field set; \
         env/headers/args/url/command can each carry a literal token: {v}"
    );
}

/// Negative: an empty array must never be readable as "no declaration found".
/// This is the ticket's own stated premise, and the reason the two codes exist.
#[test]
fn weather_declares_zero_mcp_servers_and_says_so() {
    let fx = fixture("weather");
    let out = run_skill_info(fx.path(), &[], &[]);
    assert_exit(&out, 0, "skill info against weather");

    let v = json_stdout(&out);
    assert_diagnostics_well_formed(&v);
    assert_eq!(
        v["mcp_servers"],
        serde_json::json!([]),
        "weather's .mcp.json declares {{}}, so the resolved list is empty: {v}"
    );

    // The load-bearing half: without this, `mcp_servers: []` is ambiguous
    // between "declared none" and "no declaration was found at all".
    let diag = diagnostic(&v, "mcp.declared_none");
    assert!(
        looked_in(diag).iter().any(|p| p.contains(".mcp.json")),
        "the declared-none diagnostic must name WHERE the empty declaration was \
         read from: {diag}"
    );
    assert!(
        !codes(&v).iter().any(|c| c == "mcp.no_declaration"),
        "a present-but-empty .mcp.json is `declared_none`, not `no_declaration`: {v}"
    );
}

/// AC2. An incomplete bundle is a diagnosis, not a crash.
#[test]
fn deleting_evals_cases_json_is_a_diagnostic_and_still_exits_zero() {
    let fx = fixture("weather");
    std::fs::remove_file(fx.path().join("evals/cases.json")).expect("remove the eval suite");

    let out = run_skill_info(fx.path(), &[], &[]);
    assert_exit(
        &out,
        0,
        "a missing eval suite is a diagnosis, not a failure",
    );

    let v = json_stdout(&out);
    assert_diagnostics_well_formed(&v);
    // NOT `{"case_count": 0}`, which reads as "the suite exists and is empty".
    assert_unresolved(&v["evals"], "`evals` with no cases.json");
    assert!(
        v["evals"].get("case_count").is_none(),
        "an unresolved eval block must not also claim a count: {v}"
    );

    let diag = diagnostic(&v, "evals.file_absent");
    let probed = looked_in(diag);
    assert!(
        probed
            .first()
            .is_some_and(|p| p.ends_with("evals/cases.json")),
        "the diagnostic must name the missing file BY PATH, got {probed:?}"
    );
}

/// AC3. A rejected candidate must be reported as looked-at-and-rejected. It must
/// not simply vanish from the inventory, which is the defect the ticket names.
#[test]
fn renaming_skill_md_reports_the_directory_as_looked_at_and_rejected() {
    let fx = fixture("weather");
    let dir = fx.path().join("skills/weather");
    std::fs::rename(dir.join("SKILL.md"), dir.join("SKILLS.md")).expect("rename the skill file");

    let out = run_skill_info(fx.path(), &[], &[]);
    assert_exit(&out, 0, "a non-conforming skill file is a diagnosis");

    let v = json_stdout(&out);
    assert_diagnostics_well_formed(&v);
    assert_eq!(
        v["skills"],
        serde_json::json!([]),
        "nothing registered, so the inventory is empty: {v}"
    );

    let diag = diagnostic(&v, "skill.no_skill_md");
    let candidate = diag["candidate"]
        .as_str()
        .unwrap_or_else(|| panic!("{diag}"));
    assert!(
        candidate.contains("skills/weather"),
        "the candidate must name the directory that was looked at, got {candidate:?}"
    );
    assert!(
        looked_in(diag)
            .iter()
            .any(|p| p.ends_with("skills/weather/SKILL.md")),
        "the diagnostic must name the exact filename it looked for: {diag}"
    );
    assert!(
        !diag["reason"].as_str().unwrap_or("").trim().is_empty(),
        "the rejection must carry the reason: {diag}"
    );
    // The renamed file is the near-miss; naming it is what makes the diagnosis
    // actionable rather than a restatement of the code.
    assert!(
        diag["fix"].is_string() || diag["fix"].is_null(),
        "`fix` is a string or an explicit null, never absent: {diag}"
    );
}

/// AC5, parity: the verb is IMPLEMENTED at every tier, and its `--dry-run`
/// declines the network while still naming the calls it would make.
#[test]
fn local_and_cluster_info_dry_run_emit_a_plan() {
    let cwd = scratch();

    let local = run_curie(
        &["--json", "local", "info", "demo", "--dry-run"],
        cwd.path(),
        &[],
    );
    assert_exit(&local, 0, "local info --dry-run");

    let cluster = run_curie(
        &[
            "--json",
            "cluster",
            "info",
            "demo",
            "--api-url",
            "http://127.0.0.1:9",
            "--api-key",
            "k",
            "--dry-run",
        ],
        cwd.path(),
        &[],
    );
    assert_exit(&cluster, 0, "cluster info --dry-run");

    for (tier, out) in [("local", &local), ("cluster", &cluster)] {
        let v = json_stdout(out);
        assert_eq!(
            v["dry_run"], true,
            "{tier} info --dry-run emits the uniform plan payload: {v}"
        );
        let plan = v["plan"]
            .as_array()
            .unwrap_or_else(|| panic!("{tier}: `plan` must be an array of lines: {v}"));
        assert!(
            !plan.is_empty(),
            "{tier}: an empty plan tells an agent nothing: {v}"
        );
        let joined = plan
            .iter()
            .filter_map(|l| l.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        // The plan must name the deployed-bundle read path, which is the whole
        // reason this verb is answerable at these tiers.
        assert!(
            joined.contains("/deployments"),
            "{tier}: the plan must name the deployments GET, got:\n{joined}"
        );
        assert!(
            joined.contains("/files"),
            "{tier}: the plan must name the bundle-files GET, got:\n{joined}"
        );
    }
}

/// AC5, negative: the ONE genuinely by-construction absence. The flag is
/// DECLARED at these tiers so it can be declined with a reason (exit 4) rather
/// than rejected as an unknown-flag typo, and the decline fires before any
/// network or kubectl call.
#[test]
fn local_and_cluster_info_decline_check_mcp_with_exit_4() {
    let cwd = scratch();

    let local = run_curie(
        &["--json", "local", "info", "demo", "--check-mcp"],
        cwd.path(),
        &[],
    );
    let cluster = run_curie(
        &["--json", "cluster", "info", "demo", "--check-mcp"],
        cwd.path(),
        &[],
    );

    for (tier, out) in [("local", &local), ("cluster", &cluster)] {
        assert_exit(out, 4, &format!("{tier} info --check-mcp"));
        let v = json_stdout(out);
        let keys: std::collections::BTreeSet<&str> = v
            .as_object()
            .unwrap_or_else(|| panic!("{tier}: the error payload is an object: {v}"))
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            ["error", "fix"].into_iter().collect(),
            "{tier}: the ADR-0021 error payload is exactly {{error, fix}}: {v}"
        );
        let fix = v["fix"].as_str().unwrap_or_else(|| panic!("{tier}: {v}"));
        assert!(
            fix.contains("curie skill info"),
            "{tier}: the decline must name the tier that CAN answer, got {fix:?}"
        );
        assert!(
            fix.contains("--check-mcp"),
            "{tier}: the alternative must name the flag, got {fix:?}"
        );
        let error = v["error"].as_str().unwrap_or_else(|| panic!("{tier}: {v}"));
        assert!(
            !error.trim().is_empty(),
            "{tier}: the decline must state WHY, got {error:?}"
        );
    }
}

/// The security boundary. `info` sits adjacent to declared secret names, MCP
/// `env` blocks, and model credentials; a value must never reach stdout OR
/// stderr, while the NAME is still reported.
#[test]
fn info_never_emits_a_secret_value() {
    // Hoisted to constants so no test-only credential literal sits inline.
    const PAT_NAME: &str = "GITHUB_PERSONAL_ACCESS_TOKEN";
    const PAT_SENTINEL: &str = "sentinel-do-not-print";
    const MODEL_CRED_NAME: &str = "ANTHROPIC_API_KEY";
    const MODEL_CRED_SENTINEL: &str = "sk-ant-sentinel-do-not-print";

    let fx = fixture("github-issues");
    let out = run_skill_info(
        fx.path(),
        &[],
        &[
            (PAT_NAME, PAT_SENTINEL),
            (MODEL_CRED_NAME, MODEL_CRED_SENTINEL),
        ],
    );
    assert_exit(&out, 0, "skill info with credentials in the environment");

    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
    for stream in [("stdout", &stdout), ("stderr", &stderr)] {
        for sentinel in [PAT_SENTINEL, MODEL_CRED_SENTINEL] {
            assert!(
                !stream.1.contains(sentinel),
                "a secret VALUE reached {}: the bundle's .mcp.json env block and the \
                 model credential must collapse to a name and a boolean\n{}",
                stream.0,
                stream.1
            );
        }
    }

    // ...while the NAME is still reported, satisfied. Absence of the sentinel is
    // only meaningful if the verb actually resolved the secret.
    let v = json_stdout(&out);
    let declared = v["secrets"]["declared"]
        .as_array()
        .unwrap_or_else(|| panic!("the manifest's declared secrets resolve to an array: {v}"));
    let pat = declared
        .iter()
        .find(|s| s["name"] == PAT_NAME)
        .unwrap_or_else(|| panic!("the declared secret NAME must be reported: {v}"));
    assert_eq!(
        pat["satisfied"], true,
        "the exported value satisfies it, and that fact is reportable without the value: {v}"
    );
    assert_eq!(pat["source"], "shell_env", "{v}");
    assert!(
        pat.get("value").is_none(),
        "there is no value field anywhere in this contract: {v}"
    );
}

/// Negative: a directory that is not a bundle is a USAGE error (exit 2), not an
/// incomplete bundle. Matches `commands::check`'s existing treatment.
#[test]
fn a_directory_with_no_manifest_is_a_usage_error() {
    let empty = scratch();
    let out = run_skill_info(empty.path(), &[], &[]);
    assert_exit(&out, 2, "a directory with no plugin manifest");

    let v = json_stdout(&out);
    let keys: std::collections::BTreeSet<&str> = v
        .as_object()
        .unwrap_or_else(|| panic!("{v}"))
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(keys, ["error", "fix"].into_iter().collect(), "{v}");
    let error = v["error"].as_str().unwrap_or_else(|| panic!("{v}"));
    assert!(
        error.contains("no plugin manifest under"),
        "the shared no-manifest message must travel, got {error:?}"
    );
    assert!(
        error.contains(".claude-plugin/plugin.json"),
        "it names both accepted manifest locations, got {error:?}"
    );
    assert!(
        v["fix"].as_str().is_some_and(|f| f.contains("curie init")),
        "the fix must name the way forward: {v}"
    );
}

/// Negative: a manifest present but unparseable. Every downstream fact the
/// manifest is the sole source of must go UNRESOLVED, never to an empty
/// collection that reads as "this bundle declares none".
#[test]
fn an_unparseable_manifest_is_a_diagnostic_not_a_crash() {
    let fx = fixture("weather");
    std::fs::write(fx.path().join(".claude-plugin/plugin.json"), "{")
        .expect("write a truncated manifest");

    let out = run_skill_info(fx.path(), &[], &[]);
    assert_exit(&out, 0, "an unparseable manifest is a diagnosis");

    let v = json_stdout(&out);
    assert_diagnostics_well_formed(&v);
    let diag = diagnostic(&v, "manifest.invalid_json");
    assert!(
        looked_in(diag)
            .iter()
            .any(|p| p.ends_with(".claude-plugin/plugin.json")),
        "the diagnostic must name the file it could not parse: {diag}"
    );

    // The gates come only from the manifest, so an empty list here would be the
    // ADR-0041 lie.
    assert_unresolved(
        &v["approval_gates"],
        "`approval_gates` under an unparseable manifest",
    );
    // Declared secret NAMES have exactly one source too. The schema may hang the
    // sentinel on `secrets` or on `secrets.declared`; either is honest, an empty
    // resolved list is not.
    assert!(
        is_unresolved(&v["secrets"]) || is_unresolved(&v["secrets"]["declared"]),
        "declared secrets come only from the manifest; `declared: []` reads as \
         \"this bundle declares no secrets\", which is the lie: {v}"
    );
}

/// Negative, the ADR-0041 lie rule in its sharpest form. `parse_manifest_gates`
/// returns Err on an invalid `approvalPolicy`; `info` catches it and reports
/// UNRESOLVED. Reporting `[]` would tell an operator no gate is armed for a
/// manifest the runner itself rejects.
#[test]
fn an_invalid_approval_policy_is_a_diagnostic_not_an_empty_gate_list() {
    let fx = fixture("weather");
    let manifest_path = fx.path().join(".claude-plugin/plugin.json");
    let raw = std::fs::read_to_string(&manifest_path).expect("read the manifest");
    let mut manifest: serde_json::Value = serde_json::from_str(&raw).expect("manifest parses");
    manifest["approvalPolicy"] = serde_json::json!({ "gates": [{ "gate": "Bash" }] });
    std::fs::write(
        &manifest_path,
        serde_json::to_string_pretty(&manifest).expect("serialize"),
    )
    .expect("write the gate-less-route manifest");

    let out = run_skill_info(fx.path(), &[], &[]);
    assert_exit(&out, 0, "an invalid approvalPolicy is a diagnosis");

    let v = json_stdout(&out);
    assert_diagnostics_well_formed(&v);
    assert!(
        !v["approval_gates"].is_array(),
        "asserting an empty [] here is the defect: an unparseable policy must be \
         UNRESOLVED, never an empty gate list: {v}"
    );
    assert_unresolved(
        &v["approval_gates"],
        "`approval_gates` under an invalid approvalPolicy",
    );
    let reason = v["approval_gates"]["reason"].as_str().unwrap_or("");
    assert!(
        reason.contains("route") || reason.contains("gate"),
        "the reason must quote the upstream parse failure, got {reason:?}"
    );
    diagnostic(&v, "approval_gate.manifest_invalid");
}

/// Negative, both directions of the tier inversion. Asserting the `available:
/// false` SHAPE (not mere presence) is what makes this fail against a `null` or
/// an omitted key.
#[test]
fn meaningless_at_this_tier_fields_are_present_with_a_reason() {
    // --- Skill tier: the deployed-only facts are absent. ---
    let fx = fixture("weather");
    let out = run_skill_info(fx.path(), &[], &[]);
    assert_exit(&out, 0, "skill info");
    let v = json_stdout(&out);

    assert_unavailable(&v["channel"], "`channel` at the skill tier");
    assert_unavailable(&v["comms"], "`comms` at the skill tier");
    assert_unavailable(
        &v["bundle"]["deployed"],
        "`bundle.deployed` at the skill tier",
    );
    // `model` itself is REAL at the skill tier; its recorded-runner half is not,
    // because the fixture has never been `skill up`ed.
    assert_unavailable(
        &v["model"]["recorded_runner"],
        "`model.recorded_runner` with no .curie/runner.json",
    );
    assert!(
        codes(&v).iter().any(|c| c == "state.runner_absent"),
        "the absent runner state must also be a diagnostic: {v}"
    );
    // The skill tier DOES have a filesystem root; it must be a real value.
    assert!(
        v["bundle"]["root"].is_string(),
        "`bundle.root` is real at the skill tier: {v}"
    );

    // --- Deployed origin: the inversion. ---
    let d = deployed_report(fx.path());
    assert_diagnostics_well_formed(&d);
    assert_unavailable(&d["bundle"]["root"], "`bundle.root` at a deployed tier");
    assert_unavailable(&d["model"], "`model` at a deployed tier");
    assert!(
        d["bundle"]["deployed"]["version_label"] == serde_json::json!("v1"),
        "`bundle.deployed` carries the real in-force version at a deployed tier: {d}"
    );
    assert_eq!(
        d["channel"]["id"],
        serde_json::json!("C0DEMO"),
        "`channel` is a real fact at a deployed tier: {d}"
    );
    // Satisfaction is a fact about THIS shell, not the deployed sandbox.
    if let Some(rows) = d["secrets"]["declared"].as_array() {
        for row in rows {
            assert_unavailable(
                &row["satisfied"],
                "`secrets.declared[].satisfied` at a deployed tier",
            );
        }
    }
}

/// The highest-value test in the suite: "one discovery pass" as a CHECKED
/// property. The same bundle read from disk and read as a deployed file list
/// must produce the same diagnostics and the same inventory, so a `diagnostics`
/// entry means exactly the same thing at every tier.
#[test]
fn the_same_bundle_yields_identical_diagnostics_from_disk_and_from_a_deployed_file_list() {
    // Happy path.
    let clean = fixture("weather");
    assert_cross_source_parity(clean.path(), "the clean weather bundle");

    // And the REJECTION path, which is where two implementations would actually
    // diverge. Proving only the happy path equal would be the weak version.
    let mutated = fixture("weather");
    let dir = mutated.path().join("skills/weather");
    std::fs::rename(dir.join("SKILL.md"), dir.join("SKILLS.md")).expect("rename the skill file");
    let mutated_report = assert_cross_source_parity(mutated.path(), "the AC3-mutated bundle");
    assert!(
        codes(&mutated_report)
            .iter()
            .any(|c| c == "skill.no_skill_md"),
        "the mutated fixture must actually produce the rejection whose parity is \
         under test, otherwise the equality is vacuous: {mutated_report}"
    );
}

/// Returns the disk report so the caller can assert the comparison was not
/// vacuous.
fn assert_cross_source_parity(root: &Path, what: &str) -> serde_json::Value {
    let disk = disk_report(root);
    let deployed = deployed_report(root);
    assert_diagnostics_well_formed(&disk);
    assert_diagnostics_well_formed(&deployed);

    // The ONE documented by-construction difference: a stored bundle carries no
    // empty directories and no symlinks, so the deployed pass may add a
    // `deployed.*` note the disk pass has no reason to emit. Everything else --
    // every manifest, skill, mcp, evals, secret, boot_env, approval_gate,
    // artifact and state rejection -- must be identical.
    let disk_diags = without_deployed_kind(&disk);
    let deployed_diags = without_deployed_kind(&deployed);
    assert!(
        !disk_diags.is_empty(),
        "{what}: an empty-equals-empty comparison proves nothing: {disk}"
    );
    assert_eq!(
        disk_diags, deployed_diags,
        "{what}: the two populators must feed ONE discovery pass; a difference here \
         means a tier has its own rejection vocabulary"
    );
    assert!(
        diagnostics(&disk)
            .iter()
            .all(|d| d["kind"] != serde_json::json!("deployed")),
        "{what}: the disk pass has no deployed tier to report on: {disk}"
    );

    // The inventory blocks, excluding the documented tier-absent fields BY NAME.
    for block in ["skills", "mcp_servers", "approval_gates", "evals"] {
        assert_eq!(
            disk[block], deployed[block],
            "{what}: `{block}` must resolve identically from disk and from a stored \
             file list"
        );
    }
    disk
}

fn without_deployed_kind(report: &serde_json::Value) -> Vec<serde_json::Value> {
    diagnostics(report)
        .iter()
        .filter(|d| d["kind"] != serde_json::json!("deployed"))
        .cloned()
        .collect()
}

/// Tripwire: a future test that mutates a bundle without going through
/// `fixture()` must fail loudly here rather than silently corrupting the
/// committed examples every later run reads.
#[test]
fn committed_examples_are_unmutated() {
    for rel in [
        "weather/evals/cases.json",
        "weather/skills/weather/SKILL.md",
        "weather/.claude-plugin/plugin.json",
        "weather/.mcp.json",
        "github-issues/.mcp.json",
        "github-issues/skills/github-issues/SKILL.md",
    ] {
        let path = examples_root().join(rel);
        assert!(
            path.is_file(),
            "examples/{rel} was mutated or deleted by the suite; every mutation must \
             run against a fixture() tempdir copy"
        );
    }
    // Content, not just presence: a truncated-manifest test that forgot fixture()
    // would leave the file in place but destroyed.
    let manifest = examples_root().join("weather/.claude-plugin/plugin.json");
    let raw = std::fs::read_to_string(&manifest).expect("read the committed manifest");
    let value: serde_json::Value =
        serde_json::from_str(&raw).expect("the committed manifest is still valid JSON");
    assert_eq!(value["name"], "weather", "{raw}");
}

// ===========================================================================
// 19-25: the discovery rules, driven through the public `discover` seam
// ===========================================================================

/// 19. The skill-directory rule, mirroring `plugin_format._validate_skills`'
/// `rglob("SKILL.md")`: a directory with a SKILL.md registers (at any depth),
/// one without is rejected, and an INTERMEDIATE directory in a nested tree
/// produces no rejection (that noise would bury the real finding).
#[test]
fn a_skills_dir_registers_only_directories_carrying_a_skill_md() {
    let report = synthetic(&[
        ("skills/flat/SKILL.md", VALID_FRONTMATTER),
        ("skills/nested/deep/SKILL.md", VALID_FRONTMATTER),
        ("skills/broken/notes.md", "# just some notes\n"),
    ]);
    assert_diagnostics_well_formed(&report);

    let paths: std::collections::BTreeSet<String> = report["skills"]
        .as_array()
        .unwrap_or_else(|| panic!("{report}"))
        .iter()
        .filter_map(|s| s["path"].as_str().map(str::to_string))
        .collect();
    assert_eq!(
        paths,
        ["skills/flat/SKILL.md", "skills/nested/deep/SKILL.md"]
            .into_iter()
            .map(str::to_string)
            .collect(),
        "a nested SKILL.md registers exactly as the Python validator's rglob does: {report}"
    );

    let rejected: Vec<&str> = diagnostics(&report)
        .iter()
        .filter(|d| d["code"] == serde_json::json!("skill.no_skill_md"))
        .filter_map(|d| d["candidate"].as_str())
        .collect();
    assert_eq!(
        rejected.len(),
        1,
        "exactly one directory was looked at and rejected, got {rejected:?}: {report}"
    );
    assert!(
        rejected[0].contains("skills/broken"),
        "got {rejected:?}: {report}"
    );
    assert!(
        !rejected.iter().any(|c| c.ends_with("skills/nested")),
        "an intermediate directory of a nested skill is not a rejection; emitting one \
         buries the real finding in noise: {report}"
    );
}

/// 20. The frontmatter rules. `skill.tools_confusable` mirrors
/// `_CONFUSABLE_TOOLS_KEYS`: `tools:` looks right and is silently ignored, which
/// is exactly the bundle-looks-fine-behaves-wrong class this verb exists for.
#[test]
fn frontmatter_rules_reject_a_missing_block_and_flag_the_confusable_tools_key() {
    let no_block = synthetic(&[("skills/plain/SKILL.md", "# Plain\n\nNo frontmatter here.\n")]);
    assert_diagnostics_well_formed(&no_block);
    let diag = diagnostic(&no_block, "skill.frontmatter_missing");
    assert!(
        diag["candidate"]
            .as_str()
            .is_some_and(|c| c.contains("skills/plain")),
        "{diag}"
    );

    let confusable = synthetic(&[(
        "skills/confusable/SKILL.md",
        "---\nname: confusable\ndescription: Uses the wrong key.\ntools:\n  - WebSearch\n---\n\n# Body\n",
    )]);
    assert_diagnostics_well_formed(&confusable);
    let diag = diagnostic(&confusable, "skill.tools_confusable");
    assert!(
        diag["reason"]
            .as_str()
            .is_some_and(|r| r.contains("allowed-tools")),
        "the reason must name the key that actually works: {diag}"
    );
    // The skill still registers -- a confusable key is a warning about its tool
    // grant, not a reason to drop the skill from the inventory.
    let registered: Vec<&str> = confusable["skills"]
        .as_array()
        .unwrap_or_else(|| panic!("{confusable}"))
        .iter()
        .filter_map(|s| s["path"].as_str())
        .collect();
    assert_eq!(
        registered,
        vec!["skills/confusable/SKILL.md"],
        "a confusable tools key does not un-register the skill: {confusable}"
    );
}

/// 21. Secret satisfaction at the skill tier is a fact about THIS shell.
/// Driven through the binary so the env is per-child rather than a
/// process-global mutation racing every other test in this binary.
#[test]
fn secret_satisfaction_reads_this_shell_and_treats_an_empty_value_as_absent() {
    const PAT_NAME: &str = "GITHUB_PERSONAL_ACCESS_TOKEN";
    let fx = fixture("github-issues");

    let find = |v: &serde_json::Value| -> serde_json::Value {
        v["secrets"]["declared"]
            .as_array()
            .unwrap_or_else(|| panic!("declared secrets resolve to an array: {v}"))
            .iter()
            .find(|s| s["name"] == PAT_NAME)
            .unwrap_or_else(|| panic!("the declared name must always be reported: {v}"))
            .clone()
    };

    // Present in the shell.
    let out = run_skill_info(fx.path(), &[], &[(PAT_NAME, "a-real-looking-value")]);
    assert_exit(&out, 0, "skill info with the secret exported");
    let v = json_stdout(&out);
    let row = find(&v);
    assert_eq!(row["satisfied"], true, "{v}");
    assert_eq!(row["source"], "shell_env", "{v}");
    assert!(
        !codes(&v).iter().any(|c| c == "secret.unsatisfied"),
        "a satisfied secret must not also be diagnosed as unsatisfied: {v}"
    );

    // Absent entirely.
    let out = run_skill_info(fx.path(), &[], &[]);
    assert_exit(&out, 0, "skill info with the secret unset");
    let v = json_stdout(&out);
    let row = find(&v);
    assert_eq!(row["satisfied"], false, "{v}");
    assert_eq!(row["source"], "none", "{v}");
    let diag = diagnostic(&v, "secret.unsatisfied");
    assert!(
        diag["candidate"]
            .as_str()
            .is_some_and(|c| c.contains(PAT_NAME)),
        "the diagnostic must name which declared secret is unsatisfied: {diag}"
    );

    // Exported but EMPTY. `env_credential_present`'s frozen rule: an empty
    // string is absent, not present. Getting this wrong reports a bundle as
    // ready when the sandbox would get nothing.
    let out = run_skill_info(fx.path(), &[], &[(PAT_NAME, "")]);
    assert_exit(&out, 0, "skill info with an empty secret");
    let v = json_stdout(&out);
    let row = find(&v);
    assert_eq!(
        row["satisfied"], false,
        "an empty-string export is ABSENT, not satisfied: {v}"
    );
    assert_eq!(row["source"], "none", "{v}");
}

/// 22. The two sentinels are two distinct facts and must serialize distinctly.
/// A single merged shape would let a consumer read "does not exist here" as
/// "exists but is broken", which sends an operator down the wrong path.
#[test]
fn the_two_sentinels_serialize_distinctly() {
    // A deployed-origin bundle with no eval suite gives one of each in a single
    // payload: `bundle.root` is unavailable (no directory on this machine) and
    // `evals` is unresolved (the concept exists here, the file does not).
    let report = synthetic(&[("skills/flat/SKILL.md", VALID_FRONTMATTER)]);
    assert_diagnostics_well_formed(&report);

    assert_unavailable(
        &report["bundle"]["root"],
        "`bundle.root` at a deployed tier",
    );
    assert_unresolved(&report["evals"], "`evals` with no cases.json");

    // Exact key sets: neither sentinel may grow the other's discriminator, and
    // neither may carry an extra field.
    let unavailable_keys: std::collections::BTreeSet<&str> = report["bundle"]["root"]
        .as_object()
        .expect("object")
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        unavailable_keys,
        ["available", "reason", "where"].into_iter().collect(),
        "the unavailable sentinel is exactly three keys: {report}"
    );
    let unresolved_keys: std::collections::BTreeSet<&str> = report["evals"]
        .as_object()
        .expect("object")
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        unresolved_keys,
        ["resolved", "reason"].into_iter().collect(),
        "the unresolved sentinel is exactly two keys: {report}"
    );
}

/// 23. An unreadable deployed bundle is a DIAGNOSIS at exit 0, and specifically
/// not the skill tier's exit-2 no-manifest path, which is a filesystem-only
/// outcome and would misreport a platform-side gap as a bad `--plugin-dir`.
///
/// The plan's other half of this test (prod outranks dev in
/// `select_in_force_deployment`) is deliberately NOT duplicated here: that
/// function already carries its own unit test, it is `pub(crate)` so an
/// integration test cannot reach it, and a test that calls it directly would
/// survive deleting all of `info.rs` -- it would be a `commands` test wearing an
/// `info` test's name. What `info` owns is the mapping from "nothing readable"
/// to a sentinel plus a `deployed.*` code, which is what is asserted.
#[test]
fn an_unreadable_deployed_bundle_is_a_diagnostic_not_a_no_manifest_usage_error() {
    let empty = BundleView::from_files(deployed_origin(), Vec::new());
    let report = serde_json::to_value(discover(&empty)).expect("InfoReport serializes");
    assert_diagnostics_well_formed(&report);

    assert_unresolved(
        &report["bundle"],
        "`bundle` from an empty deployed file set",
    );
    let diag = diagnostic(&report, "deployed.bundle_unreadable");
    assert_eq!(diag["kind"], "deployed", "{diag}");
    assert!(
        !codes(&report).iter().any(|c| c.starts_with("manifest.")),
        "an empty stored file set is a platform-side gap, not a manifest defect: {report}"
    );
}

/// 24. Diagnostics sort deterministically by `(kind, candidate, code)` so the
/// payload diffs cleanly and a caller can assert an exact array.
#[test]
fn diagnostics_are_sorted_by_kind_then_candidate_then_code() {
    // Several kinds, with candidates deliberately out of insertion order.
    let report = synthetic(&[
        ("skills/zeta/notes.md", "# no skill file\n"),
        ("skills/alpha/notes.md", "# no skill file\n"),
        (".mcp.json", r#"{"mcpServers": {}}"#),
    ]);
    assert_diagnostics_well_formed(&report);

    let actual: Vec<(String, String, String)> = diagnostics(&report)
        .iter()
        .map(|d| {
            (
                d["kind"].as_str().unwrap_or_default().to_string(),
                d["candidate"].as_str().unwrap_or_default().to_string(),
                d["code"].as_str().unwrap_or_default().to_string(),
            )
        })
        .collect();
    assert!(
        actual.len() >= 3,
        "this fixture must produce several diagnostics across kinds for the ordering \
         to mean anything, got {actual:?}"
    );
    let mut sorted = actual.clone();
    sorted.sort();
    assert_eq!(
        actual, sorted,
        "diagnostics must be emitted in (kind, candidate, code) order"
    );

    // Determinism: the same view discovered twice is byte-identical, so a
    // HashMap iteration order can never leak into the payload.
    let again = synthetic(&[
        ("skills/zeta/notes.md", "# no skill file\n"),
        ("skills/alpha/notes.md", "# no skill file\n"),
        (".mcp.json", r#"{"mcpServers": {}}"#),
    ]);
    assert_eq!(
        report["diagnostics"], again["diagnostics"],
        "the pass must be deterministic across runs"
    );
}

/// 25. Path normalization. A stored bundle can name a file `./skills/x/SKILL.md`
/// while a disk walk yields `skills/x/SKILL.md`. Both must land on the same view
/// key, or the cross-source parity property degenerates into a path-format test.
#[test]
fn a_leading_dot_slash_in_a_deployed_file_list_normalizes_to_the_same_view_key() {
    let manifest = (
        ".claude-plugin/plugin.json".to_string(),
        r#"{"name": "synthetic", "version": "0.1.0"}"#.to_string(),
    );
    let skill = (
        "skills/x/SKILL.md".to_string(),
        VALID_FRONTMATTER.to_string(),
    );

    let plain = BundleView::from_files(deployed_origin(), vec![manifest.clone(), skill.clone()]);
    let dotted = BundleView::from_files(
        deployed_origin(),
        vec![
            (format!("./{}", manifest.0), manifest.1.clone()),
            (format!("./{}", skill.0), skill.1.clone()),
        ],
    );

    let plain_report = serde_json::to_value(discover(&plain)).expect("serializes");
    let dotted_report = serde_json::to_value(discover(&dotted)).expect("serializes");

    // Not vacuous: the skill must actually have registered.
    assert_eq!(
        plain_report["skills"]
            .as_array()
            .map(std::vec::Vec::len)
            .unwrap_or(0),
        1,
        "the plain form must resolve the skill: {plain_report}"
    );
    assert_eq!(
        plain_report, dotted_report,
        "a leading ./ is a path-format artifact of the stored bundle, not a different \
         file; normalizing it is what makes the cross-source parity test a parity test"
    );
}
