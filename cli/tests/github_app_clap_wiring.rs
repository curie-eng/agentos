//! Integration: the clap-to-`GithubAppOpts` wiring of `cluster github-app`'s
//! BYO Secret flags (issue #1255).
//!
//! Every other test for this verb lives in `cli/src/github_app.rs`'s unit
//! module and builds a `GithubAppOpts` by hand, so it proves what the command
//! builders do with a field but never that the FLAG reaches that field. The
//! match arm in `main.rs` destructures the clap variant and then constructs
//! `GithubAppOpts` by name; Rust makes the destructuring half exhaustive, but
//! nothing makes the construction half exhaustive. A field pulled out of the
//! pattern and never placed into the struct compiles perfectly clean, and the
//! flag is silently dropped: `--existing-secret my-github-app` would parse,
//! validate, print "GitHub App configured", and configure nothing.
//!
//! These tests close that gap by driving the built binary and asserting on the
//! `--dry-run --json` plan, which carries the exact helm argv that would run.
//! Assertions are on WHOLE whitespace-separated tokens of the plan, never a
//! substring of the joined line: `contains("api.githubAppExistingSecret=")` is
//! also satisfied by the disconnect clear and by any other Secret name, so it
//! tests for a prefix rather than for the value that was set (#1263).
//!
//! No cluster and no network. `--dry-run` returns before any helm or kubectl
//! process is spawned, and an explicit `--existing-secret` is the one connect
//! path that makes no `helm get values` read (see `needs_byo_conflict_check`),
//! so the values-read step this verb gained cannot reach out either. `--chart`
//! is passed explicitly because chart resolution otherwise looks for
//! `charts/curie` relative to the test process's cwd, which is the `cli`
//! package root.

use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

/// Run the binary with `argv` and return every whitespace-separated token of
/// its `--dry-run --json` plan, flattened across plan lines.
fn dry_run_plan_tokens(argv: &[&str]) -> Vec<String> {
    let output = Command::new(bin())
        .args(argv)
        .output()
        .unwrap_or_else(|e| panic!("run curie {}: {e}", argv.join(" ")));
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        output.status.success(),
        "curie {} must exit 0; stdout: {stdout}; stderr: {stderr}",
        argv.join(" ")
    );
    let value: serde_json::Value = serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("stdout must be one JSON object: {e}; stdout: {stdout}"));
    let lines = value
        .get("plan")
        .and_then(|p| p.as_array())
        .unwrap_or_else(|| panic!("dry-run output must carry a plan: {value}"));
    lines
        .iter()
        .filter_map(|l| l.as_str())
        .flat_map(|l| l.split_whitespace())
        .map(str::to_string)
        .collect()
}

/// The plan of a BYO connect that names a NON-default data key. Non-default on
/// purpose: `--existing-secret-key privateKey` would also pass if the flag were
/// dropped on the floor, because `privateKey` is the value the default supplies
/// (#1263).
fn byo_plan_tokens() -> Vec<String> {
    dry_run_plan_tokens(&[
        "cluster",
        "github-app",
        "--app-id",
        "1",
        "--existing-secret",
        "my-github-app",
        "--existing-secret-key",
        "app-pem",
        "--chart",
        "charts/curie",
        "--dry-run",
        "--json",
    ])
}

/// The whole plan token immediately preceding `value`.
fn token_before(tokens: &[String], value: &str) -> String {
    let at = tokens
        .iter()
        .position(|t| t == value)
        .unwrap_or_else(|| panic!("no plan token equal to `{value}`: {tokens:?}"));
    assert!(at > 0, "`{value}` has no preceding token: {tokens:?}");
    tokens[at - 1].clone()
}

#[test]
fn the_existing_secret_flags_reach_the_helm_plan_not_just_the_parser() {
    // The silent-drop failure mode: both flags parse, the verb reports
    // success, and the release is never pointed at the operator's Secret. The
    // only place that is visible is the argv the verb would actually run.
    let tokens = byo_plan_tokens();
    assert!(
        tokens
            .iter()
            .any(|t| t == "api.githubAppExistingSecret=my-github-app"),
        "--existing-secret never reached the helm plan: {tokens:?}"
    );
    assert!(
        tokens
            .iter()
            .any(|t| t == "api.githubAppExistingSecretKey=app-pem"),
        "--existing-secret-key never reached the helm plan: {tokens:?}"
    );
}

#[test]
fn the_wired_byo_flags_are_set_as_strings() {
    // An all-digit Secret name or data key parsed by `--set` round-trips
    // through --reuse-values as a float64 and renders in scientific notation
    // -- #1236's App-ID bug in a new field. The clap layer is where a
    // hand-written `--set` would land, so it is asserted here too.
    let tokens = byo_plan_tokens();
    assert_eq!(
        token_before(&tokens, "api.githubAppExistingSecret=my-github-app"),
        "--set-string",
        "the Secret name must not be helm-typed: {tokens:?}"
    );
    assert_eq!(
        token_before(&tokens, "api.githubAppExistingSecretKey=app-pem"),
        "--set-string",
        "the data key must not be helm-typed: {tokens:?}"
    );
}

#[test]
fn the_wired_byo_connect_never_asks_helm_to_read_a_pem() {
    // The security property, asserted at the real entry point: on the BYO path
    // no file path is handed to helm, so no PEM can be copied into release
    // history where `helm get values` prints it back (#1236).
    let tokens = byo_plan_tokens();
    assert!(
        !tokens.iter().any(|t| t == "--set-file"),
        "the wired BYO plan makes helm read a file off disk: {tokens:?}"
    );
    assert!(
        tokens.iter().any(|t| t == "api.githubAppPrivateKey="),
        "the wired BYO plan must clear the inline key: {tokens:?}"
    );
}

#[test]
fn the_wired_byo_connect_still_rolls_the_api() {
    // A secretKeyRef env var is resolved once at pod start, so the helm
    // upgrade alone leaves the running API on the old credential while the CLI
    // reports success. AC1 says the BYO path includes the rollout.
    let tokens = byo_plan_tokens();
    assert!(
        tokens.iter().any(|t| t == "restart"),
        "the BYO plan never restarts the api deployment: {tokens:?}"
    );
    assert!(
        tokens.iter().any(|t| t == "status"),
        "the BYO plan never waits for the rollout: {tokens:?}"
    );
    assert!(
        tokens.iter().any(|t| t == "deployment/curie-api"),
        "the BYO plan rolls something other than the api: {tokens:?}"
    );
}
