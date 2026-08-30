//! Integration: `cluster github-app` refuses a malformed `--app-id` or
//! `--private-key` at the real entry point (issue #1260).
//!
//! The unit module in `cli/src/github_app.rs` calls `require_connect_inputs`
//! directly, so it proves the RULES but never that the rules sit on the path a
//! command line actually takes. Between clap and that function lie a
//! `default_value` on every one of these flags and a hand-written struct
//! construction in `main.rs`; a validated value that then reached
//! `connect_commands` un-normalised, or a refusal that landed after the plan was
//! already printed, would leave the unit suite entirely green. The defect this
//! ticket exists to remove -- exit 0, "GitHub App configured", an App that is
//! not in use -- is only visible from outside the process.
//!
//! So these tests drive the built binary and read its `--dry-run --json` plan,
//! which carries the exact helm argv that would run. Assertions are on WHOLE
//! whitespace-separated plan tokens, never a substring of the joined line:
//! `contains("api.githubAppId=1234567")` is also satisfied by
//! `api.githubAppId= 1234567 `, which is the untrimmed bug this ticket fixes
//! (#1263).
//!
//! No cluster and no network. `--dry-run` returns before any helm or kubectl
//! process is spawned and makes no `helm get values` read (`cli/CLAUDE.md`:
//! `--dry-run` never touches the network), and every refusal here lands in
//! `require_connect_inputs`, which runs before all of that. `--chart` is passed
//! explicitly because chart resolution otherwise looks for `charts/curie`
//! relative to the test process's cwd, which is the `cli` package root.

use std::process::{Command, Output};

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

/// A real PEM-shaped fixture on disk, so an `--app-id` case is judged by the
/// `--app-id` rules and never refused on the way there for want of a key.
///
/// PEM-SHAPED, not credential-shaped: the body is the marker lines and a
/// placeholder, because `require_connect_inputs` checks the shape and nothing
/// here ever reads the bytes. A real key body would be a secret-scanner
/// tripwire with no test value.
fn pem_fixture(dir: &tempfile::TempDir) -> String {
    let path = dir.path().join("app.pem");
    std::fs::write(
        &path,
        format!(
            "{}\nplaceholder; the shape is what is checked\n{}\n",
            pem_marker("BEGIN"),
            pem_marker("END"),
        ),
    )
    .expect("write pem fixture");
    path.to_string_lossy().into_owned()
}

/// One PEM marker line, composed from its boundary word rather than written
/// out whole. This fixture carries no key material -- nothing here is ever
/// parsed as a key -- but spelling the markers out contiguously reads as a
/// pasted credential to a secret scanner, a false positive that would
/// otherwise have to be allowlisted in every repo that vendors this test.
fn pem_marker(boundary: &str) -> String {
    format!("-----{boundary} RSA PRIVATE KEY-----")
}

fn run(argv: &[&str]) -> Output {
    Command::new(bin())
        .args(argv)
        .output()
        .unwrap_or_else(|e| panic!("run curie {}: {e}", argv.join(" ")))
}

fn combined(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

fn stdout_json(output: &Output) -> serde_json::Value {
    let stdout = String::from_utf8_lossy(&output.stdout);
    serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("stdout must be one JSON object: {e}; stdout: {stdout}"))
}

/// Every whitespace-separated token of a successful `--dry-run --json` plan,
/// flattened across plan lines.
fn plan_tokens(output: &Output, argv: &[&str]) -> Vec<String> {
    assert!(
        output.status.success(),
        "curie {} must exit 0; output: {}",
        argv.join(" "),
        combined(output)
    );
    let value = stdout_json(output);
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

/// A chart-held `--dry-run --json` connect with the given App id.
fn connect(app_id: &str, key_path: &str) -> (Vec<String>, Output) {
    let argv = vec![
        "cluster",
        "github-app",
        "--app-id",
        app_id,
        "--private-key",
        key_path,
        "--chart",
        "charts/curie",
        "--dry-run",
        "--json",
    ];
    let output = run(&argv);
    (argv.iter().map(|a| a.to_string()).collect(), output)
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
fn a_padded_app_id_reaches_the_helm_plan_trimmed() {
    // AC1, through clap. ` 1234567 ` is what a paste out of the App's settings
    // page produces; helm stores the whitespace verbatim, the api pod signs a
    // JWT whose `iss` claim is " 1234567 ", and every GitHub call answers 401
    // while `helm get values` prints something that reads as correct. Asserted
    // as a whole token, which is the only assertion the untrimmed value fails.
    let dir = tempfile::tempdir().expect("tempdir");
    let key = pem_fixture(&dir);
    let (argv, output) = connect(" 1234567 ", &key);
    let argv: Vec<&str> = argv.iter().map(String::as_str).collect();
    let tokens = plan_tokens(&output, &argv);
    assert!(
        tokens.iter().any(|t| t == "api.githubAppId=1234567"),
        "the padded id reached the plan unnormalised: {tokens:?}"
    );
    assert_eq!(
        token_before(&tokens, "api.githubAppId=1234567"),
        "--set-string",
        "the App id must not be helm-typed: {tokens:?}"
    );
}

#[test]
fn an_app_id_above_two_to_the_fifty_third_round_trips_through_the_binary() {
    // AC4, and the red-on-revert guard for #1236's fix on the CLI path. 2^53+1
    // survives only if nothing in the chain -- validator or emitter -- takes it
    // through an f64; a hop that did would render 9007199254740992 here and the
    // JWT's `iss` would name an App that does not exist.
    let dir = tempfile::tempdir().expect("tempdir");
    let key = pem_fixture(&dir);
    let (argv, output) = connect("9007199254740993", &key);
    let argv: Vec<&str> = argv.iter().map(String::as_str).collect();
    let tokens = plan_tokens(&output, &argv);
    assert!(
        tokens
            .iter()
            .any(|t| t == "api.githubAppId=9007199254740993"),
        "an id above 2^53 did not round-trip exactly: {tokens:?}"
    );
    assert_eq!(
        token_before(&tokens, "api.githubAppId=9007199254740993"),
        "--set-string",
        "the App id must not be helm-typed: {tokens:?}"
    );
}

#[test]
fn a_comma_injected_app_id_is_refused_with_an_actionable_fix_and_no_plan() {
    // AC2, and the case that makes the validator a security boundary rather
    // than a tidiness check. `--set-string` stops helm TYPING a value but helm
    // still splits the expression on commas STRUCTURALLY, so this single argv
    // entry is read as TWO assignments -- the second silently re-pointing every
    // clone at a host the operator never named. Before #1260 this exited 0 and
    // printed "GitHub App configured".
    //
    // Exit code 2 is asserted as the exact ADR-0021 Usage class, not merely
    // "non-zero": a `bail!` here would exit 1 with a null fix, which tells the
    // agent driving the CLI to retry a command that can never succeed (#1261).
    let dir = tempfile::tempdir().expect("tempdir");
    let key = pem_fixture(&dir);
    let (_argv, output) = connect("1,api.githubCloneBase=https://evil.example.com", &key);
    assert_eq!(
        output.status.code(),
        Some(2),
        "an injected App id must exit 2 (Usage); output: {}",
        combined(&output)
    );
    let value = stdout_json(&output);
    assert!(
        value.get("fix").is_some_and(|f| !f.is_null()),
        "the refusal must carry a non-null fix: {value}"
    );
    // The precise failure being prevented is a PLAN existing at all: a refusal
    // that arrived after the plan was built and printed would still exit 2.
    assert!(
        value.get("plan").is_none(),
        "the refused run emitted a helm plan: {value}"
    );
    // The injected text appearing inside `error` is REQUIRED, not forbidden:
    // `describe_rejected_value` quotes the rejected value back in the refusal,
    // and that echo is what makes AC2's "readable message" readable at all. A
    // future reader must not "fix" this back into a blanket substring ban --
    // what must never appear is a rendered helm assignment, i.e. the injected
    // text reaching argv construction rather than staying quoted prose.
    assert!(
        value
            .get("error")
            .and_then(|e| e.as_str())
            .is_some_and(|e| e.contains("1,api.githubCloneBase=https://evil.example.com")),
        "the refusal must name the rejected value: {value}"
    );
    let text = combined(&output);
    assert!(
        !text
            .split_whitespace()
            .any(|tok| tok == "--set-string" || tok == "--set" || tok == "helm"),
        "the refused run rendered a helm invocation: {text}"
    );
}

#[test]
fn a_non_numeric_app_id_is_refused() {
    // AC2's plainest case, and the one an operator hits by pasting the App slug
    // or the client id instead of the numeric App ID. It rendered into the
    // release and 401d on every call, long after this command reported success.
    let dir = tempfile::tempdir().expect("tempdir");
    let key = pem_fixture(&dir);
    let (_argv, output) = connect("abc", &key);
    assert_eq!(
        output.status.code(),
        Some(2),
        "a non-numeric App id must exit 2 (Usage); output: {}",
        combined(&output)
    );
}

#[test]
fn a_zero_byte_private_key_is_refused_rather_than_reported_as_configured() {
    // AC3, and the failure this ticket is named for. A 0-byte file passes
    // `is_file`, so before #1260 the run exited 0 and printed "GitHub App
    // configured" -- while helm rendered `githubAppPrivateKey: ""`, the
    // platform's `is_configured` answered False, and the platform silently fell
    // back to `api.githubToken`. Nothing downstream ever surfaced it. Exit 2,
    // not merely non-zero, for the #1261 reason above.
    //
    // No assertion reads the file's contents, here or anywhere in this file.
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("empty.pem");
    std::fs::write(&path, "").expect("write empty fixture");
    let (_argv, output) = connect("1234567", &path.to_string_lossy());
    assert_eq!(
        output.status.code(),
        Some(2),
        "a 0-byte private key must exit 2 (Usage), not report success; output: {}",
        combined(&output)
    );
    let value = stdout_json(&output);
    assert!(
        value.get("github_app_configured").is_none(),
        "the refused run reported the App as configured: {value}"
    );
    assert!(
        value.get("fix").is_some_and(|f| !f.is_null()),
        "the refusal must carry a non-null fix: {value}"
    );
}
