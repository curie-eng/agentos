//! `curie dev soak` skips cleanly, and says WHY (#2056).
//!
//! The scenario behind the verb (`tests/soak/test_soak_resilience.py`) needs a
//! standing cluster, so the interesting property for everyone without one is the
//! skip: exit 0, one machine-readable result object, and a reason naming the
//! concrete context, namespace or warm pool that was absent. A skip that exits 0
//! with a generic "not available" is indistinguishable from a soak that ran and
//! found nothing, which is exactly the vacuity that makes a nightly rung
//! unreadable.
//!
//! These tests therefore prove the skip BY EXECUTION rather than by unit-testing
//! a predicate: a fake `kubectl` goes on the child's `PATH` (the stubbing shape
//! `cli/src/doctor.rs` already uses for its cluster reads) and fails at one
//! preflight stage per case, so the real verb walks its real preflight order.
//! `PATH` is set on the child `Command` only, never on this process, so the
//! stubs cannot leak into the rest of the suite and no environment lock is
//! needed. A scratch directory carrying a `runner/Dockerfile` sentinel stands in
//! for the source checkout `find_repo_root` looks for.
//!
//! The skip cases alone would leave the verb's whole POINT untested: the
//! scenario is `CURIE_SOAK=1`-gated, so a regression that set `CURIE_SOAK=0`,
//! dropped the variable, or retargeted the pytest file would make pytest skip
//! or collect the wrong thing and still report a confident green -- every
//! preflight test above returns before the child is ever spawned. So the same
//! `PATH` trick carries a second stub: a fake `uv` that records its argv and
//! the soak-relevant environment into a file the test names via an env var,
//! prints chatter to its own stdout, and exits with a code the test picks. With
//! both stubs the verb walks its entire happy path -- preflights, child
//! invocation, exit-code classification, result emit -- with no cluster and no
//! pytest.
//!
//! A failed probe is two different findings, and only kubectl's own stderr
//! separates them: `NotFound` licenses saying the object is ABSENT, while
//! anything else (RBAC, TLS, a refused connection, silence) supports only
//! saying the probe could not be READ. The stubs therefore write realistic
//! apiserver stderr, so both branches are walked by execution rather than
//! asserted about a predicate. A last stub-free case gives the child a `PATH`
//! holding only the stub dir, so `uv` resolves to nothing and the launch
//! failure path runs too.
//!
//! Nothing here needs, or reaches, a cluster.

use std::path::Path;
use std::process::Command;

use curie::commands::{soak_outcome_for_exit_code, SoakStatus};

/// `kubectl` that cannot report a current context: the "no cluster configured
/// at all" case.
const KUBECTL_NO_CONTEXT: &str = r#"#!/bin/sh
case "$*" in
  "config current-context") exit 1 ;;
  *) printf 'unexpected kubectl invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#;

/// `kubectl` pointed at a cluster that has no such namespace.
const KUBECTL_NO_NAMESPACE: &str = r#"#!/bin/sh
case "$*" in
  "config current-context") printf '%s\n' 'soak-stub-context' ;;
  "get namespace "*) exit 1 ;;
  *) printf 'unexpected kubectl invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#;

/// `kubectl` pointed at a cluster with the namespace but no warm pool -- the
/// deepest preflight, and the one the scenario's sandbox claims depend on.
/// The failing branch also prints a line of its own to stdout before exiting
/// 1 -- real `kubectl` failures do this (e.g. a server error body), and the
/// stub needs that chatter so the leak assertion below has something real to
/// catch: a stub that prints nothing on stdout makes that assertion vacuous.
const KUBECTL_NO_POOL: &str = r#"#!/bin/sh
case "$*" in
  "config current-context") printf '%s\n' 'soak-stub-context' ;;
  "get namespace "*) exit 0 ;;
  "get sandboxwarmpool "*) printf 'soak-stub-chatter-on-stdout\n'; exit 1 ;;
  *) printf 'unexpected kubectl invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#;

/// `kubectl` pointed at a cluster that satisfies every preflight, so the verb
/// proceeds to actually spawn the scenario.
const KUBECTL_PREFLIGHT_OK: &str = r#"#!/bin/sh
case "$*" in
  "config current-context") printf '%s\n' 'soak-stub-context' ;;
  "get namespace "*) exit 0 ;;
  "get sandboxwarmpool "*) exit 0 ;;
  *) printf 'unexpected kubectl invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#;

/// A fake `uv` standing in for `uv run pytest`. It records what the verb asked
/// it to do -- every argv element, and the four environment variables the
/// scenario is steered by -- into the file named by `CURIE_SOAK_TEST_RECORD`,
/// then prints a distinctive marker on its OWN stdout (so the leak assertion
/// has something real to catch) and exits with `CURIE_SOAK_TEST_EXIT`. Both
/// variables are set on the `curie` child, which passes its environment through
/// to the scenario process. `UNSET` is recorded for a variable that is absent,
/// so "dropped entirely" is distinguishable from "set to the empty string".
const UV_RECORDER: &str = r#"#!/bin/sh
: > "$CURIE_SOAK_TEST_RECORD"
for arg in "$@"; do
  printf 'argv\t%s\n' "$arg" >> "$CURIE_SOAK_TEST_RECORD"
done
for name in CURIE_SOAK CURIE_SOAK_RUNS CURIE_SANDBOX_E2E_NAMESPACE CURIE_SANDBOX_E2E_POOL; do
  eval "value=\${$name-UNSET}"
  printf 'env\t%s=%s\n' "$name" "$value" >> "$CURIE_SOAK_TEST_RECORD"
done
printf 'soak-stub-uv-chatter-on-stdout\n'
exit "${CURIE_SOAK_TEST_EXIT:-0}"
"#;

/// The marker `UV_RECORDER` prints on its own stdout.
const UV_STDOUT_MARKER: &str = "soak-stub-uv-chatter-on-stdout";

/// A scratch source checkout: only the `runner/Dockerfile` sentinel
/// `find_repo_root` keys on, so the verb believes it is in a checkout without
/// this test touching the real one.
fn scratch_checkout() -> tempfile::TempDir {
    let scratch = tempfile::tempdir().expect("scratch repo");
    std::fs::create_dir_all(scratch.path().join("runner")).expect("create repo sentinel dir");
    std::fs::write(scratch.path().join("runner/Dockerfile"), "").expect("write repo sentinel");
    scratch
}

/// A directory holding executable fakes named `(program, script)`, to be
/// prepended to the child's `PATH`.
fn stub_tools(tools: &[(&str, &str)]) -> tempfile::TempDir {
    let dir = tempfile::tempdir().expect("stub tool dir");
    for (program, body) in tools {
        let path = dir.path().join(program);
        std::fs::write(&path, body).unwrap_or_else(|e| panic!("write fake {program}: {e}"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755))
                .unwrap_or_else(|e| panic!("make fake {program} runnable: {e}"));
        }
    }
    dir
}

/// Prepend `dir` to the inherited `PATH`, for the child `Command` only.
fn path_with(dir: &Path) -> String {
    let inherited = std::env::var("PATH").unwrap_or_default();
    format!("{}:{inherited}", dir.display())
}

/// Parse the `--json` stream, which must be exactly one object however the run
/// ended. Shared by the skip, pass and fail paths so none of them can quietly
/// settle for a looser contract than the others.
fn parse_single_result_object(stdout: &str, stderr: &str) -> serde_json::Value {
    serde_json::from_str(stdout).unwrap_or_else(|error| {
        panic!(
            "--json stdout must be exactly one parseable result object: {error}; \
             stdout: {stdout:?}; stderr: {stderr:?}"
        )
    })
}

/// Run `curie --json dev soak` in a scratch checkout with the stub `kubectl`
/// winning on `PATH`, and return the parsed result object plus the raw streams.
fn run_soak(
    kubectl_body: &str,
    namespace: &str,
    pool: &str,
) -> (serde_json::Value, String, std::process::Output) {
    let checkout = scratch_checkout();
    let tools = stub_tools(&[("kubectl", kubectl_body)]);
    let path = path_with(tools.path());

    let output = Command::new(env!("CARGO_BIN_EXE_curie"))
        .args([
            "--json",
            "dev",
            "soak",
            "--namespace",
            namespace,
            "--pool",
            pool,
        ])
        .current_dir(checkout.path())
        .env("PATH", path)
        .output()
        .expect("run dev soak against a scratch checkout");

    let stdout = String::from_utf8(output.stdout.clone()).expect("stdout is UTF-8");
    let stderr = String::from_utf8(output.stderr.clone()).expect("stderr is UTF-8");
    let payload = parse_single_result_object(&stdout, &stderr);
    (payload, stderr, output)
}

/// Every skip is exit 0 with one object naming a reason. The three reasons must
/// also DIFFER: a single catch-all string would satisfy "non-empty reason" at
/// every stage while telling an operator nothing about which prerequisite is
/// missing.
#[test]
fn each_missing_prerequisite_skips_with_its_own_named_reason() {
    let namespace = "soak-scratch-ns";
    let pool = "soak-scratch-pool";

    let (no_context, stderr, output) = run_soak(KUBECTL_NO_CONTEXT, namespace, pool);
    assert!(
        output.status.success(),
        "an unreachable cluster is a skip, not a failure; stderr: {stderr}"
    );
    assert_eq!(no_context["status"], "skipped", "{no_context}");
    assert_eq!(
        no_context["context"],
        serde_json::Value::Null,
        "{no_context}"
    );
    assert_eq!(
        no_context["exit_code"],
        serde_json::Value::Null,
        "pytest never ran, so there is no exit code: {no_context}"
    );
    let context_reason = no_context["reason"]
        .as_str()
        .unwrap_or_else(|| panic!("a skip must carry a reason string: {no_context}"))
        .to_string();
    assert!(
        !context_reason.is_empty(),
        "the skip reason must not be empty: {no_context}"
    );

    let (no_namespace, stderr, output) = run_soak(KUBECTL_NO_NAMESPACE, namespace, pool);
    assert!(
        output.status.success(),
        "a missing namespace is a skip, not a failure; stderr: {stderr}"
    );
    assert_eq!(no_namespace["status"], "skipped", "{no_namespace}");
    let namespace_reason = no_namespace["reason"]
        .as_str()
        .unwrap_or_else(|| panic!("a skip must carry a reason string: {no_namespace}"))
        .to_string();
    assert!(
        namespace_reason.contains(namespace),
        "the reason must name the namespace that was asked for: {namespace_reason:?}"
    );

    let (no_pool, stderr, output) = run_soak(KUBECTL_NO_POOL, namespace, pool);
    assert!(
        output.status.success(),
        "a missing warm pool is a skip, not a failure; stderr: {stderr}"
    );
    assert_eq!(no_pool["status"], "skipped", "{no_pool}");
    let pool_reason = no_pool["reason"]
        .as_str()
        .unwrap_or_else(|| panic!("a skip must carry a reason string: {no_pool}"))
        .to_string();
    assert!(
        pool_reason.contains(pool),
        "the reason must name the warm pool that was asked for: {pool_reason:?}"
    );

    let mut distinct = vec![
        context_reason.as_str(),
        namespace_reason.as_str(),
        pool_reason.as_str(),
    ];
    distinct.sort_unstable();
    distinct.dedup();
    assert_eq!(
        distinct.len(),
        3,
        "each preflight must skip with its own reason, not one catch-all: \
         {context_reason:?} / {namespace_reason:?} / {pool_reason:?}"
    );
}

/// Under `--json`, stdout is exactly one result object: the preflight's child
/// output belongs on stderr, and a second object (or a stray line) would break
/// every consumer that reads one line and parses it.
#[test]
fn soak_json_keeps_preflight_chatter_off_stdout() {
    let (payload, stderr, output) =
        run_soak(KUBECTL_NO_POOL, "soak-scratch-ns", "soak-scratch-pool");
    assert!(
        output.status.success(),
        "the skip path must exit 0; stderr: {stderr}"
    );
    assert!(
        payload.is_object(),
        "--json result must be an object: {payload}"
    );
    let stdout = String::from_utf8(output.stdout).expect("stdout is UTF-8");
    assert_eq!(
        stdout.trim().lines().count(),
        1,
        "--json stdout must be exactly one line: {stdout:?}"
    );
    assert!(
        !stdout.contains("soak-stub-chatter-on-stdout"),
        "the stub kubectl's own stdout must not reach the result stream: {stdout:?}"
    );
}

/// Pytest's exit codes mean different things, and only 0 is a pass. Code 5 in
/// particular is the silent-vacuity case: pytest collected nothing, so the
/// scenario never ran even though it "did not fail".
#[test]
fn only_exit_code_zero_passes_and_five_names_the_empty_collection() {
    let (status, reason) = soak_outcome_for_exit_code(0);
    assert_eq!(status, SoakStatus::Passed);
    assert!(reason.is_none(), "a pass carries no reason: {reason:?}");

    let mut reasons = Vec::new();
    for code in [1, 2, 3, 4, 5] {
        let (status, reason) = soak_outcome_for_exit_code(code);
        assert_eq!(
            status,
            SoakStatus::Failed,
            "pytest exit {code} must not be reported as a pass"
        );
        let reason = reason.unwrap_or_else(|| panic!("pytest exit {code} must carry a reason"));
        assert!(
            !reason.is_empty(),
            "pytest exit {code} must carry a non-empty reason"
        );
        reasons.push(reason);
    }

    let collected_nothing = &reasons[4];
    assert!(
        collected_nothing.contains("collected no tests"),
        "pytest exit 5 must read as collecting no tests, the silent-vacuity case: \
         {collected_nothing:?}"
    );

    let mut distinct: Vec<&str> = reasons.iter().map(String::as_str).collect();
    distinct.sort_unstable();
    distinct.dedup();
    assert_eq!(
        distinct.len(),
        5,
        "each failing pytest exit code needs its own reason: {reasons:?}"
    );
}

/// The env-default wiring is the whole reason the nightly rung needs no flags.
/// Dropping `env = ...` from a flag would still compile, still pass every test
/// above (they pass the flags explicitly), and silently make an exported
/// harness environment stop being honored -- so pin it on the help text.
#[test]
fn the_flags_declare_the_harness_environment_variables() {
    let output = Command::new(env!("CARGO_BIN_EXE_curie"))
        .args(["dev", "soak", "--help"])
        .output()
        .expect("run dev soak --help");
    assert!(
        output.status.success(),
        "`dev soak --help` must succeed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let help = String::from_utf8(output.stdout).expect("help is UTF-8");
    for variable in [
        "CURIE_SANDBOX_E2E_NAMESPACE",
        "CURIE_SANDBOX_E2E_POOL",
        "CURIE_SOAK_RUNS",
    ] {
        assert!(
            help.contains(variable),
            "`dev soak --help` must name {variable} so the env default cannot be dropped \
             silently: {help}"
        );
    }
}

/// The scenario the verb runs must actually be there. A rename on the pytest
/// side would otherwise turn every real invocation into a pytest exit 4, which
/// reads as a usage error rather than as the broken wiring it is.
#[test]
fn the_named_scenario_file_exists() {
    let scenario =
        Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/..")).join(curie::commands::SOAK_SCENARIO);
    assert!(
        scenario.is_file(),
        "`curie dev soak` runs {}, which must exist in the checkout",
        scenario.display()
    );
}

/// The Rust and Python defaults for the namespace and pool must stay the same
/// two strings.
///
/// `--namespace` and `--pool` carry clap `default_value`s, and clap ALWAYS
/// exports a value for a defaulted arg -- so `dev_soak` always passes
/// `CURIE_SANDBOX_E2E_NAMESPACE` and `CURIE_SANDBOX_E2E_POOL` to the child, and
/// `SoakConfig.from_env`'s own `os.environ.get` fallbacks in
/// `tests/soak/harness.py` are dead code under the verb. The Rust side is
/// therefore authoritative, and the drift is silent in one direction: change the
/// harness default and `curie dev soak` keeps targeting the old cluster while a
/// bare `uv run pytest` targets the new one, with nothing failing. Pin the two
/// literals on the harness so that edit has to come here.
#[test]
fn the_harness_defaults_match_the_flag_defaults() {
    let harness =
        Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/..")).join("tests/soak/harness.py");
    let source = std::fs::read_to_string(&harness)
        .unwrap_or_else(|error| panic!("read {}: {error}", harness.display()));
    // The whole `os.environ.get` call, not the bare default: "curie-g1" is a
    // prefix of "curie-g1-runner-pool", so asserting the bare literals would
    // let a namespace default vanish and still pass on the pool's line.
    for fallback in [
        r#"os.environ.get("CURIE_SANDBOX_E2E_NAMESPACE", "curie-g1")"#,
        r#"os.environ.get("CURIE_SANDBOX_E2E_POOL", "curie-g1-runner-pool")"#,
    ] {
        assert!(
            source.contains(fallback),
            "{} must still read `{fallback}`: `curie dev soak`'s clap defaults restate it, and \
             clap always exports a defaulted arg, so a harness-only change would silently point \
             the verb and a bare `uv run pytest` at different clusters",
            harness.display()
        );
    }
}

/// What the fake `uv` was asked to do: its argv, and the soak-relevant slice of
/// the environment it was handed.
struct Recorded {
    argv: Vec<String>,
    env: std::collections::BTreeMap<String, String>,
}

impl Recorded {
    /// The recorded value of `name`, or a panic naming the whole record --
    /// `UNSET` is a recorded value, so a missing key here means the stub itself
    /// never ran or never wrote its line.
    fn env(&self, name: &str) -> &str {
        self.env
            .get(name)
            .unwrap_or_else(|| panic!("the stub uv recorded no {name} line: {:?}", self.env))
    }
}

/// Read back what `UV_RECORDER` wrote. The file's absence is itself a finding:
/// it means the verb never reached the child at all.
fn read_record(path: &Path, stderr: &str) -> Recorded {
    let raw = std::fs::read_to_string(path).unwrap_or_else(|error| {
        panic!(
            "the soak verb never invoked its child (no record at {}): {error}; stderr: {stderr:?}",
            path.display()
        )
    });
    let mut argv = Vec::new();
    let mut env = std::collections::BTreeMap::new();
    for line in raw.lines() {
        if let Some(arg) = line.strip_prefix("argv\t") {
            argv.push(arg.to_string());
        } else if let Some(rest) = line.strip_prefix("env\t") {
            let (name, value) = rest
                .split_once('=')
                .unwrap_or_else(|| panic!("malformed recorded env line: {line:?}"));
            env.insert(name.to_string(), value.to_string());
        } else {
            panic!("unexpected line in the uv record: {line:?}");
        }
    }
    Recorded { argv, env }
}

/// The outcome of a run that got all the way past preflight and spawned the
/// child: the parsed result object, the raw streams, the process status, and
/// what the fake `uv` recorded.
struct CompletedSoak {
    payload: serde_json::Value,
    stdout: String,
    stderr: String,
    status: std::process::ExitStatus,
    recorded: Recorded,
}

/// Run `curie --json dev soak` with every preflight satisfied, so the verb
/// spawns the fake `uv` -- which exits `uv_exit` after recording its call. Same
/// scratch checkout and child-only `PATH` as `run_soak`; the two stubs simply
/// share one directory.
fn run_soak_to_completion(uv_exit: &str, namespace: &str, pool: &str, runs: &str) -> CompletedSoak {
    let checkout = scratch_checkout();
    let tools = stub_tools(&[("kubectl", KUBECTL_PREFLIGHT_OK), ("uv", UV_RECORDER)]);
    let records = tempfile::tempdir().expect("record dir");
    let record = records.path().join("uv-invocation");

    let output = Command::new(env!("CARGO_BIN_EXE_curie"))
        .args([
            "--json",
            "dev",
            "soak",
            "--namespace",
            namespace,
            "--pool",
            pool,
            "--runs",
            runs,
        ])
        .current_dir(checkout.path())
        .env("PATH", path_with(tools.path()))
        .env("CURIE_SOAK_TEST_RECORD", &record)
        .env("CURIE_SOAK_TEST_EXIT", uv_exit)
        // Decoys, so every environment assertion below proves the HANDLER set
        // the value. The child inherits this process's environment, so a
        // contributor who happens to export CURIE_SOAK=1 (the scenario's own
        // gate) would otherwise make that assertion pass with the handler's
        // `.env` deleted. Each decoy is a value the assertion rejects; for the
        // three flag-backed variables it doubles as proof the explicit flag
        // wins over clap's `env = ...` default.
        .env("CURIE_SOAK", "0")
        .env("CURIE_SOAK_RUNS", "999")
        .env("CURIE_SANDBOX_E2E_NAMESPACE", "inherited-decoy-namespace")
        .env("CURIE_SANDBOX_E2E_POOL", "inherited-decoy-pool")
        .output()
        .expect("run dev soak against a scratch checkout");

    let stdout = String::from_utf8(output.stdout.clone()).expect("stdout is UTF-8");
    let stderr = String::from_utf8(output.stderr.clone()).expect("stderr is UTF-8");
    let payload = parse_single_result_object(&stdout, &stderr);
    let recorded = read_record(&record, &stderr);
    CompletedSoak {
        payload,
        stdout,
        stderr,
        status: output.status,
        recorded,
    }
}

/// `CURIE_SOAK=1` is the entire reason this verb exists (#2056: nothing ever set
/// it), and it is invisible from the outside -- flipping it to `0`, or dropping
/// it, makes pytest skip the gated scenario, exit 0, and let the verb report a
/// confident `passed`. So assert it BY EXECUTION on the real child, alongside
/// the pytest target: an accidental retarget fails here, while a deliberate
/// rename moves `SOAK_SCENARIO` and stays honest.
#[test]
fn the_child_is_uv_run_pytest_on_the_gated_scenario_with_curie_soak_set() {
    let run = run_soak_to_completion("0", "soak-scratch-ns", "soak-scratch-pool", "1");

    assert_eq!(
        run.recorded.env("CURIE_SOAK"),
        "1",
        "the scenario is CURIE_SOAK=1-gated; any other value (or none) makes pytest skip it \
         and the verb report a green that proves nothing. The child inherited CURIE_SOAK=0, so \
         a 0 here means the handler stopped setting it. stderr: {}",
        run.stderr
    );
    assert_eq!(
        run.recorded.argv,
        vec![
            "run".to_string(),
            "pytest".to_string(),
            curie::commands::SOAK_SCENARIO.to_string(),
            "-q".to_string(),
        ],
        "the verb must invoke `uv run pytest {} -q`",
        curie::commands::SOAK_SCENARIO
    );
}

/// The flags are the verb's only steering, and they only steer anything if they
/// reach the child as environment. Use values that match no default, so a
/// handler that dropped the plumbing and let the scenario fall back could not
/// pass by coincidence.
#[test]
fn the_flags_reach_the_child_as_environment() {
    let namespace = "soak-flag-ns";
    let pool = "soak-flag-pool";
    let run = run_soak_to_completion("0", namespace, pool, "7");

    assert_eq!(
        run.recorded.env("CURIE_SANDBOX_E2E_NAMESPACE"),
        namespace,
        "--namespace must reach the scenario"
    );
    assert_eq!(
        run.recorded.env("CURIE_SANDBOX_E2E_POOL"),
        pool,
        "--pool must reach the scenario"
    );
    assert_eq!(
        run.recorded.env("CURIE_SOAK_RUNS"),
        "7",
        "--runs must reach the scenario, which owns the repetition"
    );
    assert_eq!(run.payload["runs"], 7, "{}", run.payload);
}

/// A child that exits 0 is the pass, and the pass must stay quiet: the
/// scenario's own stdout is chatter, and letting it through would put a second
/// line (or a non-JSON one) on the `--json` result stream that every consumer
/// parses as a single object.
#[test]
fn a_passing_child_reports_passed_and_keeps_its_stdout_off_the_result_stream() {
    let run = run_soak_to_completion("0", "soak-scratch-ns", "soak-scratch-pool", "1");

    assert!(
        run.status.success(),
        "a passing scenario must exit 0; stderr: {}",
        run.stderr
    );
    assert_eq!(run.payload["status"], "passed", "{}", run.payload);
    assert_eq!(
        run.payload["exit_code"], 0,
        "the pytest exit code must be reported: {}",
        run.payload
    );
    assert_eq!(
        run.payload["reason"],
        serde_json::Value::Null,
        "a pass carries no reason: {}",
        run.payload
    );
    assert_eq!(
        run.stdout.trim().lines().count(),
        1,
        "--json stdout must be exactly one line: {:?}",
        run.stdout
    );
    assert!(
        !run.stdout.contains(UV_STDOUT_MARKER),
        "the scenario's own stdout must not reach the result stream: {:?}",
        run.stdout
    );
    assert!(
        run.stderr.contains(UV_STDOUT_MARKER),
        "the scenario's stdout must still be visible, on stderr: {:?}",
        run.stderr
    );
}

/// A real soak failure exits non-zero, but it must NOT degrade into the generic
/// `{error, fix}` payload: an agent reading the result needs the same
/// `status`/`reason`/`exit_code` family the pass and skip emit, which is exactly
/// what `with_json_payload` is for. Still exactly one object on stdout, and
/// still no child chatter in it.
#[test]
fn a_failing_child_reports_failed_on_the_same_result_family() {
    let run = run_soak_to_completion("1", "soak-fail-ns", "soak-fail-pool", "1");

    assert!(
        !run.status.success(),
        "a failing scenario must exit non-zero; stdout: {:?}",
        run.stdout
    );
    assert_eq!(run.payload["status"], "failed", "{}", run.payload);
    assert_eq!(
        run.payload["exit_code"], 1,
        "the pytest exit code must be reported: {}",
        run.payload
    );
    assert_eq!(
        run.payload["namespace"], "soak-fail-ns",
        "the failure must carry the soak result family, not the generic error payload: {}",
        run.payload
    );
    assert!(
        run.payload.get("fix").is_none() && run.payload.get("error").is_none(),
        "a soak failure must not degrade into the generic error payload: {}",
        run.payload
    );
    let reason = run.payload["reason"]
        .as_str()
        .unwrap_or_else(|| panic!("a failure must carry a reason string: {}", run.payload));
    assert!(
        reason.contains("pytest exit 1"),
        "the reason must name what pytest reported: {reason:?}"
    );
    assert_eq!(
        run.stdout.trim().lines().count(),
        1,
        "--json stdout must be exactly one line even on the failure path: {:?}",
        run.stdout
    );
    assert!(
        !run.stdout.contains(UV_STDOUT_MARKER),
        "the scenario's own stdout must not reach the result stream: {:?}",
        run.stdout
    );
}

/// `kubectl` on a reachable cluster that genuinely has no such namespace: it
/// says so in its own words, on STDERR, in the shape a real apiserver uses.
/// The `KUBECTL_NO_NAMESPACE` stub above exits 1 saying nothing, which is the
/// ambiguous case; this one is the only case that licenses "absent".
const KUBECTL_NAMESPACE_NOT_FOUND: &str = r#"#!/bin/sh
case "$*" in
  "config current-context") printf '%s\n' 'soak-stub-context' ;;
  "get namespace "*)
    printf 'Error from server (NotFound): namespaces "%s" not found\n' "$3" >&2
    exit 1 ;;
  *) printf 'unexpected kubectl invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#;

/// The same NotFound shape one probe deeper: the namespace is there, the warm
/// pool the scenario claims from is not.
const KUBECTL_POOL_NOT_FOUND: &str = r#"#!/bin/sh
case "$*" in
  "config current-context") printf '%s\n' 'soak-stub-context' ;;
  "get namespace "*) exit 0 ;;
  "get sandboxwarmpool "*)
    printf 'Error from server (NotFound): sandboxwarmpools.curie.dev "%s" not found\n' "$3" >&2
    exit 1 ;;
  *) printf 'unexpected kubectl invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#;

/// `kubectl` that cannot ASK the cluster about the namespace: RBAC refuses the
/// read. The object may well exist; nothing here supports a claim either way,
/// so this is the case a wrong "absent" would misreport as a verdict about the
/// cluster.
const KUBECTL_NAMESPACE_FORBIDDEN: &str = r#"#!/bin/sh
case "$*" in
  "config current-context") printf '%s\n' 'soak-stub-context' ;;
  "get namespace "*)
    printf 'Error from server (Forbidden): namespaces "%s" is forbidden: User "soak-stub-user" cannot get resource "namespaces" in API group "" at the cluster scope\n' "$3" >&2
    exit 1 ;;
  *) printf 'unexpected kubectl invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#;

/// The same unreadable-probe case one stage deeper, on the warm pool.
const KUBECTL_POOL_FORBIDDEN: &str = r#"#!/bin/sh
case "$*" in
  "config current-context") printf '%s\n' 'soak-stub-context' ;;
  "get namespace "*) exit 0 ;;
  "get sandboxwarmpool "*)
    printf 'Error from server (Forbidden): sandboxwarmpools.curie.dev "%s" is forbidden: User "soak-stub-user" cannot get resource "sandboxwarmpools" in API group "curie.dev" in the namespace "%s"\n' "$3" "$5" >&2
    exit 1 ;;
  *) printf 'unexpected kubectl invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#;

/// The clause the namespace reason uses ONLY when kubectl itself said NotFound:
/// a verdict about the cluster ("there is nothing to soak").
const NAMESPACE_ABSENT_WORDING: &str = "does not exist on kube context";

/// The same clause for the warm pool.
const POOL_ABSENT_WORDING: &str = "is absent from namespace";

/// The clause both operational reasons use instead: a verdict about the PROBE
/// ("I could not find out"), which is all an unreadable failure supports.
const UNCHECKABLE_WORDING: &str = "could not be checked";

/// A distinctive fragment of the two RBAC stubs' stderr. The reason has to
/// quote kubectl's own words, or the operator is left with "something went
/// wrong" and no way to tell an expired credential from a broken kubeconfig.
const KUBECTL_ERROR_FRAGMENT: &str = "Forbidden";

/// Run one preflight-failure case and return its reason, having first pinned
/// the parts every skip shares: exit 0, and `status: "skipped"`. Shared by the
/// NotFound and operational cases below so neither can drift into asserting on
/// the reason wording while quietly tolerating a non-zero exit.
fn skip_reason(kubectl_body: &str, namespace: &str, pool: &str) -> String {
    let (payload, stderr, output) = run_soak(kubectl_body, namespace, pool);
    assert!(
        output.status.success(),
        "a failed preflight probe is a skip, not a failure; stderr: {stderr}"
    );
    assert_eq!(payload["status"], "skipped", "{payload}");
    assert_eq!(
        payload["exit_code"],
        serde_json::Value::Null,
        "pytest never ran, so there is no exit code: {payload}"
    );
    payload["reason"]
        .as_str()
        .unwrap_or_else(|| panic!("a skip must carry a reason string: {payload}"))
        .to_string()
}

/// kubectl's own `NotFound` is the only thing that licenses the ABSENT wording,
/// so it needs a case that actually produces it. Every other stub in this file
/// fails with empty stderr, which is the ambiguous case -- without this test the
/// absent branch is unreached, and a mutation deleting it (or inverting the
/// predicate) would leave the whole suite green.
#[test]
fn a_notfound_namespace_probe_reports_the_namespace_absent() {
    let namespace = "soak-notfound-ns";
    let reason = skip_reason(KUBECTL_NAMESPACE_NOT_FOUND, namespace, "soak-notfound-pool");

    assert!(
        reason.contains(namespace),
        "the reason must name the namespace that was asked for: {reason:?}"
    );
    assert!(
        reason.contains(NAMESPACE_ABSENT_WORDING),
        "kubectl said NotFound, so the reason may state the namespace is absent: {reason:?}"
    );
    assert!(
        !reason.contains(UNCHECKABLE_WORDING),
        "a positive NotFound is not an uncheckable probe; hedging here would make the two \
         branches indistinguishable: {reason:?}"
    );
}

/// The same licensing rule one probe deeper. The pool branch is a separate
/// `if`, so it can regress on its own.
#[test]
fn a_notfound_warm_pool_probe_reports_the_pool_absent() {
    let pool = "soak-notfound-pool";
    let reason = skip_reason(KUBECTL_POOL_NOT_FOUND, "soak-notfound-ns", pool);

    assert!(
        reason.contains(pool),
        "the reason must name the warm pool that was asked for: {reason:?}"
    );
    assert!(
        reason.contains(POOL_ABSENT_WORDING),
        "kubectl said NotFound, so the reason may state the pool is absent: {reason:?}"
    );
    assert!(
        !reason.contains(UNCHECKABLE_WORDING),
        "a positive NotFound is not an uncheckable probe: {reason:?}"
    );
}

/// An RBAC refusal says nothing about whether the namespace exists. Reporting
/// it as absent is a false statement about the CLUSTER, and it is the exact
/// statement that would send someone creating a namespace that is already
/// there. Two-sided on purpose: the claim must be dropped AND kubectl's own
/// words must survive into the reason, or the operator gets a hedge with no
/// diagnostic in it.
#[test]
fn an_unreadable_namespace_probe_says_uncheckable_and_quotes_kubectl() {
    let namespace = "soak-forbidden-ns";
    let reason = skip_reason(
        KUBECTL_NAMESPACE_FORBIDDEN,
        namespace,
        "soak-forbidden-pool",
    );

    assert!(
        !reason.contains(NAMESPACE_ABSENT_WORDING),
        "an access failure does not license claiming the namespace is absent: {reason:?}"
    );
    assert!(
        reason.contains(UNCHECKABLE_WORDING),
        "the reason must say the probe could not be read: {reason:?}"
    );
    assert!(
        reason.contains(KUBECTL_ERROR_FRAGMENT),
        "the reason must quote kubectl's own error, or the skip is undiagnosable: {reason:?}"
    );
    assert!(
        reason.contains(namespace),
        "the reason must still name the namespace that was asked for: {reason:?}"
    );
}

/// Same two-sided property on the warm-pool probe, which has its own copy of
/// the branch.
#[test]
fn an_unreadable_warm_pool_probe_says_uncheckable_and_quotes_kubectl() {
    let pool = "soak-forbidden-pool";
    let reason = skip_reason(KUBECTL_POOL_FORBIDDEN, "soak-forbidden-ns", pool);

    assert!(
        !reason.contains(POOL_ABSENT_WORDING),
        "an access failure does not license claiming the warm pool is absent: {reason:?}"
    );
    assert!(
        reason.contains(UNCHECKABLE_WORDING),
        "the reason must say the probe could not be read: {reason:?}"
    );
    assert!(
        reason.contains(KUBECTL_ERROR_FRAGMENT),
        "the reason must quote kubectl's own error, or the skip is undiagnosable: {reason:?}"
    );
    assert!(
        reason.contains(pool),
        "the reason must still name the warm pool that was asked for: {reason:?}"
    );
}

/// The wording assertions above are all substring checks, so a refactor that
/// collapsed both branches into one sentence carrying every phrase would keep
/// them green. Pin the distinction itself: for the SAME probe, "absent" and
/// "could not be checked" must be different sentences, because the split is the
/// entire point of reading kubectl's stderr at all.
#[test]
fn the_absent_and_uncheckable_reasons_are_different_sentences() {
    let namespace = "soak-split-ns";
    let pool = "soak-split-pool";

    let namespace_absent = skip_reason(KUBECTL_NAMESPACE_NOT_FOUND, namespace, pool);
    let namespace_uncheckable = skip_reason(KUBECTL_NAMESPACE_FORBIDDEN, namespace, pool);
    assert_ne!(
        namespace_absent, namespace_uncheckable,
        "a missing namespace and an unqueryable cluster must not read the same"
    );

    let pool_absent = skip_reason(KUBECTL_POOL_NOT_FOUND, namespace, pool);
    let pool_uncheckable = skip_reason(KUBECTL_POOL_FORBIDDEN, namespace, pool);
    assert_ne!(
        pool_absent, pool_uncheckable,
        "a missing warm pool and an unqueryable cluster must not read the same"
    );
}

/// A `PATH` holding ONLY `dir`, for the child `Command` only. `path_with`
/// prepends to the inherited `PATH`, which is right for every stub that must
/// WIN a lookup; proving a lookup FAILS needs the inherited entries gone, or a
/// contributor with `uv` installed silently tests the happy path instead.
fn path_only(dir: &Path) -> String {
    dir.display().to_string()
}

/// A child that could not be SPAWNED is still a soak result. The natural
/// implementation -- returning a bare error -- would put the generic
/// `{error, fix}` payload on stdout, so an agent reading a non-zero `dev soak`
/// would find no `status`, no `exit_code`, and no way to tell "uv is missing"
/// from "the cluster broke". Driven by execution with every preflight passing
/// and no `uv` anywhere on the child's `PATH`.
#[test]
fn a_child_that_never_launched_reports_failed_on_the_same_result_family() {
    let checkout = scratch_checkout();
    let tools = stub_tools(&[("kubectl", KUBECTL_PREFLIGHT_OK)]);

    let output = Command::new(env!("CARGO_BIN_EXE_curie"))
        .args([
            "--json",
            "dev",
            "soak",
            "--namespace",
            "soak-launch-ns",
            "--pool",
            "soak-launch-pool",
        ])
        .current_dir(checkout.path())
        // Only the stub dir: the point of the case is that `uv` resolves to
        // nothing at all.
        .env("PATH", path_only(tools.path()))
        .output()
        .expect("run dev soak against a scratch checkout");

    let stdout = String::from_utf8(output.stdout.clone()).expect("stdout is UTF-8");
    let stderr = String::from_utf8(output.stderr.clone()).expect("stderr is UTF-8");
    assert!(
        !output.status.success(),
        "a scenario that never launched must exit non-zero; stdout: {stdout:?}"
    );
    assert_eq!(
        stdout.trim().lines().count(),
        1,
        "--json stdout must be exactly one line even when the child never started: {stdout:?}"
    );

    let payload = parse_single_result_object(&stdout, &stderr);
    assert_eq!(payload["status"], "failed", "{payload}");
    assert_eq!(
        payload["exit_code"],
        serde_json::Value::Null,
        "there is no child, so there is no exit code to report: {payload}"
    );
    assert!(
        payload.get("fix").is_none() && payload.get("error").is_none(),
        "a launch failure must not degrade into the generic error payload: {payload}"
    );
    let reason = payload["reason"]
        .as_str()
        .unwrap_or_else(|| panic!("a failure must carry a reason string: {payload}"));
    assert!(
        reason.contains("uv"),
        "the reason must name what could not be launched: {reason:?}"
    );
}
