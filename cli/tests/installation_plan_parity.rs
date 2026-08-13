use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::{json, Value};

const IPV4: &str = "1.1.1.1";
const IPV6: &str = "2606:4700:4700::1111";
const MODEL_VALUE: &str = "model-value-for-plan";
const GITHUB_VALUE: &str = "github-value-for-plan";

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("cli has a repository parent")
        .to_path_buf()
}

/// The answer a real `kubectl get statefulset -n <ns> -o json` gives when the
/// namespace holds none, and (identically) when the namespace does not exist at
/// all: an empty List, exit 0. Observed behaviour, not an assumption about the
/// implementation: recorded against a real apiserver with kubectl v1.36.2 and
/// written up in the plan for #1351. The whole guard rests on it, so the stub
/// reproduces the shape verbatim.
const KUBECTL_EMPTY_LIST: &str =
    r#"{"apiVersion":"v1","items":[],"kind":"List","metadata":{"resourceVersion":""}}"#;

/// The stderr a real kubectl writes when it cannot reach an apiserver, observed
/// in the same recording (`KUBECONFIG=/dev/null kubectl get statefulset ...`,
/// exit 1). This is the shape a CI runner or a laptop with no cluster produces.
const KUBECTL_UNREACHABLE: &str =
    "The connection to the server localhost:8080 was refused - did you specify the right host or port?";

/// The stderr a real kubectl writes when the caller's identity is denied by
/// RBAC rather than the cluster being unreachable, for a namespaced `get
/// statefulset` list. Verified against a publicly reported operator RBAC
/// denial of this exact shape (a service account listing statefulsets
/// without the role), which read: `statefulsets.apps is forbidden: User
/// "system:serviceaccount:extension-system:keda-operator" cannot list
/// resource "statefulsets" in API group "apps" at the cluster scope`. This
/// string swaps in the namespaced tail (`in the namespace "<ns>"`) that a
/// `-n` scoped list produces instead of "at the cluster scope". Deliberately
/// carries none of `is_connectivity_failure`'s markers (no "connection",
/// "refused", "timeout", "unreachable", "dial tcp", etc.): this is the
/// non-connectivity failure shape the guard must still fail closed on.
const KUBECTL_FORBIDDEN: &str = "Error from server (Forbidden): statefulsets.apps is forbidden: User \"system:serviceaccount:curie:deployer\" cannot list resource \"statefulsets\" in API group \"apps\" in the namespace \"curie\"";

/// One staged object, as `<size> <key>`, the shape both halves of the
/// migration's verify compare: the staging pod's `find -printf` listing and the
/// new store's `aws s3 ls` listing. Identical on both sides means nothing was
/// lost, which is the successful migration the AC2 test asserts.
const STAGED_OBJECT: &str = "100 bundle.tar";

/// Writes `body` to `dir/name` and makes it executable (mode 0o755), the
/// dance both the helm and kubectl stubs need identically. Returns the path
/// written.
fn write_exec(dir: &Path, name: &str, body: &str) -> PathBuf {
    let path = dir.join(name);
    fs::write(&path, body).unwrap_or_else(|error| panic!("write {name} stub: {error}"));
    let mut permissions = fs::metadata(&path)
        .unwrap_or_else(|error| panic!("{name} metadata: {error}"))
        .permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&path, permissions)
        .unwrap_or_else(|error| panic!("make {name} stub executable: {error}"));
    path
}

struct HelmFixture {
    temp: tempfile::TempDir,
    file: PathBuf,
    live_values: Option<String>,
    log: PathBuf,
}

impl HelmFixture {
    fn new(config: &str, live_values: Option<Value>) -> Self {
        let temp = tempfile::tempdir().expect("tempdir");
        let file = temp.path().join("curie.yaml");
        fs::write(&file, config).expect("write curie.yaml");

        let bin_dir = temp.path().join("bin");
        fs::create_dir(&bin_dir).expect("create stub bin directory");
        write_exec(
            &bin_dir,
            "helm",
            r#"#!/bin/sh
if [ -n "${CURIE_TEST_CALL_LOG:-}" ]; then
    printf 'HELM_CALL: %s\n' "$*" >> "$CURIE_TEST_CALL_LOG"
fi
if [ "$1" = get ] && [ "$2" = values ]; then
    if [ "${CURIE_TEST_HELM_ABSENT:-}" = 1 ]; then
        printf '%s\n' 'release not found' >&2
        exit 1
    fi
    printf '%s\n' "$CURIE_TEST_HELM_VALUES"
    exit 0
fi
if [ "$1" = template ]; then
    if [ "${CURIE_TEST_HELM_MIXED_STATEFULSETS:-}" = 1 ]; then
        cat <<'YAML'
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: parity-rustfs
spec:
  selector:
    matchLabels:
      app.kubernetes.io/component: rustfs
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: parity-curie-postgres
spec:
  selector:
    matchLabels:
      app.kubernetes.io/component: postgres
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: parity-curie-valkey
spec:
  selector:
    matchLabels:
      app.kubernetes.io/component: valkey
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: parity-curie-clickhouse
spec:
  selector:
    matchLabels:
      app.kubernetes.io/component: clickhouse
YAML
        exit 0
    fi
    cat <<'YAML'
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: parity-rustfs
spec:
  selector:
    matchLabels:
      app.kubernetes.io/component: rustfs
YAML
    exit 0
fi
if [ "$1" = upgrade ]; then
    exit 0
fi
printf 'unexpected helm invocation: %s\n' "$*" >&2
exit 64
"#,
        );

        write_exec(
            &bin_dir,
            "kubectl",
            &format!(
                r#"#!/bin/sh
# Answers only the calls the paths under test actually make, and exits 64 on
# anything else, the same tripwire convention the helm stub uses. A stub that
# hands every question the same plausible blob cannot tell "the code asked the
# right question" from "the code asked a nonsense question and got a nonsense
# answer": the migration's Service lookup was being answered with the whole
# StatefulSet List, which then landed inside an endpoint URL (#1351).
if [ -n "${{CURIE_TEST_CALL_LOG:-}}" ]; then
    printf 'KUBECTL_CALL: %s\n' "$*" >> "$CURIE_TEST_CALL_LOG"
fi
all="$*"
verb=""
object=""
while [ $# -gt 0 ]; do
    case "$1" in
        --) break ;;
        -n|--namespace|-o|--output|-l|--selector|--image|--overrides) shift 2 ;;
        -*) shift ;;
        *)
            if [ -z "$verb" ]; then
                verb="$1"
            elif [ -z "$object" ]; then
                object="$1"
            fi
            shift
            ;;
    esac
done
unexpected() {{
    printf 'unexpected kubectl invocation: %s\n' "$all" >&2
    exit 64
}}
case "$verb $object" in
'get statefulset')
    if [ "${{CURIE_TEST_KUBECTL_FAIL:-}}" = 1 ]; then
        printf '%s\n' '{KUBECTL_UNREACHABLE}' >&2
        exit 1
    fi
    if [ "${{CURIE_TEST_KUBECTL_FORBIDDEN:-}}" = 1 ]; then
        printf '%s\n' '{KUBECTL_FORBIDDEN}' >&2
        exit 1
    fi
    if [ -n "${{CURIE_TEST_KUBECTL_STS:-}}" ]; then
        printf '%s\n' "$CURIE_TEST_KUBECTL_STS"
    else
        printf '%s\n' '{KUBECTL_EMPTY_LIST}'
    fi
    ;;
'get svc')
    # The jsonpath asks for a Service NAME, so answer with one, for whichever
    # store component the caller named and nothing else.
    case "$all" in
        *'=="minio"'*) printf '%s\n' 'parity-minio' ;;
        *'=="rustfs"'*) printf '%s\n' 'parity-rustfs' ;;
        *) unexpected ;;
    esac
    ;;
'get secret')
    printf '%s\n' 'parity-secrets'
    ;;
'run '*|'wait '*|'delete pod')
    # Staging-pod lifecycle: nothing to say, just succeed.
    : ;;
'exec '*)
    # One answer per in-pod script the migration runs, keyed on the script
    # itself: a single canned answer here would let the export and the verify
    # read each other's output.
    case "$all" in
        *'wc -l'*) printf '%s\n' '1' ;;
        *'-printf'*) printf '%s\n' '{STAGED_OBJECT}' ;;
        *'aws s3 ls'*) printf '%s\n' '{STAGED_OBJECT}' ;;
        *'aws s3 sync'*) printf '%s\n' 'synced' ;;
        *) unexpected ;;
    esac
    ;;
*)
    unexpected
    ;;
esac
exit 0
"#
            ),
        );

        let log = temp.path().join("calls.log");
        Self {
            temp,
            file,
            live_values: live_values.map(|values| values.to_string()),
            log,
        }
    }

    /// Every stubbed helm and kubectl invocation this fixture has seen, in
    /// order. Empty when nothing ran, which is the assertion a run that must
    /// touch no cluster turns on, so a missing file reads as "no calls" rather
    /// than panicking.
    fn calls(&self) -> String {
        fs::read_to_string(&self.log).unwrap_or_default()
    }

    fn run(&self, args: &[&str], env: &[(&str, &str)]) -> Output {
        let mut paths = vec![self.temp.path().join("bin")];
        if let Some(current) = std::env::var_os("PATH") {
            paths.extend(std::env::split_paths(&current));
        }
        let path = std::env::join_paths(paths).expect("join PATH");

        let mut command = Command::new(bin());
        command
            .current_dir(repo_root())
            .arg("--json")
            .args(args)
            .env("PATH", path)
            .env("CURIE_TEST_CALL_LOG", &self.log)
            .env_remove("CURIE_TEST_KUBECTL_STS")
            .env_remove("CURIE_TEST_KUBECTL_FAIL")
            .env_remove("CURIE_TEST_KUBECTL_FORBIDDEN")
            .env_remove("CURIE_TEST_HELM_MIXED_STATEFULSETS")
            .env_remove("CURIE_MODEL")
            .env_remove("CURIE_TEST_PROVIDER_EGRESS_JSON")
            .env_remove("CURIE_APPLY_TEST_MODEL_KEY")
            .env_remove("CURIE_APPLY_TEST_GITHUB_TOKEN");
        match &self.live_values {
            Some(values) => {
                command
                    .env_remove("CURIE_TEST_HELM_ABSENT")
                    .env("CURIE_TEST_HELM_VALUES", values);
            }
            None => {
                command
                    .env("CURIE_TEST_HELM_ABSENT", "1")
                    .env_remove("CURIE_TEST_HELM_VALUES");
            }
        }
        for (key, value) in env {
            command.env(key, value);
        }
        command.output().expect("run curie")
    }

    fn apply_dry_run(&self, env: &[(&str, &str)]) -> Output {
        self.run(
            &[
                "apply",
                "--dry-run",
                "--file",
                self.file.to_str().expect("UTF 8 path"),
            ],
            env,
        )
    }

    /// The REAL apply path, no `--dry-run`: the guard, the migration branch and
    /// the upgrade all run against the stubs.
    fn apply(&self, extra: &[&str], env: &[(&str, &str)]) -> Output {
        let mut args = vec!["apply", "--file", self.file.to_str().expect("UTF 8 path")];
        args.extend_from_slice(extra);
        self.run(&args, env)
    }

    fn diff(&self, env: &[(&str, &str)]) -> Output {
        self.run(
            &["diff", "--file", self.file.to_str().expect("UTF 8 path")],
            env,
        )
    }
}

fn provider_egress_fixture() -> String {
    json!({"api.anthropic.com": [IPV4, IPV6]}).to_string()
}

fn json_output(output: Output, verb: &str) -> Value {
    assert!(
        output.status.success(),
        "{verb} failed with stdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "{verb} did not emit JSON: {error}; stdout:\n{}",
            String::from_utf8_lossy(&output.stdout)
        )
    })
}

fn json_error(output: Output, verb: &str) -> Value {
    assert!(
        !output.status.success(),
        "{verb} unexpectedly succeeded; stdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        output.stderr.is_empty(),
        "{verb} wrote an error to stderr under --json:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let json: Value = serde_json::from_slice(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "{verb} did not emit structured JSON error: {error}; stdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    });
    assert!(json["error"].is_string(), "{verb} error payload: {json}");
    assert!(json.get("fix").is_some(), "{verb} error payload: {json}");
    json
}

fn plan(output: Output) -> String {
    let json = json_output(output, "apply --dry-run");
    json["plan"]
        .as_array()
        .expect("dry run plan array")
        .iter()
        .map(|line| line.as_str().expect("dry run line"))
        .collect::<Vec<_>>()
        .join("\n")
}

fn shell_tokens(line: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut token = String::new();
    let mut quoted = false;
    for character in line.chars() {
        match character {
            '\'' => quoted = !quoted,
            character if character.is_whitespace() && !quoted => {
                if !token.is_empty() {
                    tokens.push(std::mem::take(&mut token));
                }
            }
            character => token.push(character),
        }
    }
    assert!(!quoted, "unterminated quote in dry run command: {line}");
    if !token.is_empty() {
        tokens.push(token);
    }
    tokens
}

fn helm_values(plan: &str) -> BTreeMap<String, String> {
    let helm = plan
        .lines()
        .find(|line| line.starts_with("helm upgrade "))
        .unwrap_or_else(|| panic!("missing helm upgrade command: {plan}"));
    let tokens = shell_tokens(helm);
    let mut values = BTreeMap::new();
    let mut index = 0;
    while index < tokens.len() {
        if tokens[index] == "--set" || tokens[index] == "--set-string" {
            let setting = tokens.get(index + 1).unwrap_or_else(|| {
                panic!("{} has no value in dry run command: {helm}", tokens[index])
            });
            let (key, value) = setting
                .split_once('=')
                .unwrap_or_else(|| panic!("invalid Helm setting {setting}: {helm}"));
            values.insert(key.to_string(), value.to_string());
            index += 2;
            continue;
        }
        if tokens[index] == "-f" {
            let file = tokens
                .get(index + 1)
                .unwrap_or_else(|| panic!("-f has no value in dry run command: {helm}"));
            if let Some(secret_values) = file
                .strip_prefix("<secret values file: ")
                .and_then(|value| value.strip_suffix('>'))
            {
                for setting in secret_values.split(", ") {
                    let (key, value) = setting
                        .split_once('=')
                        .unwrap_or_else(|| panic!("invalid secret Helm setting {setting}: {helm}"));
                    if matches!(key, "agentSandbox.runner.credentials" | "api.githubToken") {
                        values.insert(key.to_string(), value.to_string());
                    }
                }
            }
            index += 2;
            continue;
        }
        index += 1;
    }
    values
}

fn entry<'a>(diff: &'a Value, key: &str) -> &'a Value {
    diff["entries"]
        .as_array()
        .expect("diff entries array")
        .iter()
        .find(|entry| entry["key"] == key)
        .unwrap_or_else(|| panic!("missing diff entry for {key}: {diff}"))
}

fn assert_added(diff: &Value, key: &str, value: &str) {
    let entry = entry(diff, key);
    assert_eq!(entry["kind"], "add", "{key}: {entry}");
    assert_eq!(entry["to"].as_str(), Some(value), "{key}: {entry}");
}

fn assert_diff_keys(diff: &Value, expected: &[&str]) {
    let actual = diff["entries"]
        .as_array()
        .expect("diff entries array")
        .iter()
        .map(|entry| entry["key"].as_str().expect("diff entry key"))
        .collect::<BTreeSet<_>>();
    let expected = expected.iter().copied().collect::<BTreeSet<_>>();
    assert_eq!(actual, expected, "diff effective values: {diff}");
}

/// The smallest installation the stateful-removal guard runs against: it names
/// only the namespace and release, so the run makes exactly the cluster calls
/// the guard and the upgrade need and nothing else.
fn installation_for_the_stateful_guard() -> &'static str {
    "version: 1\ninstall:\n  namespace: parity\n  release: parity\n"
}

/// A live release running a `minio` object store, in the shape
/// `kubectl get statefulset -o json` returns it. The component label is the
/// identity the guard reads; the resource name is what an operator sees.
fn live_minio_statefulset() -> String {
    json!({
        "apiVersion": "v1",
        "kind": "List",
        "items": [{
            "metadata": {"name": "parity-minio"},
            "spec": {"selector": {"matchLabels": {"app.kubernetes.io/component": "minio"}}}
        }]
    })
    .to_string()
}

fn live_mixed_store_statefulsets() -> String {
    json!({
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "metadata": {"name": "parity-minio"},
                "spec": {"selector": {"matchLabels": {"app.kubernetes.io/component": "minio"}}}
            },
            {
                "metadata": {"name": "parity-postgres"},
                "spec": {"selector": {"matchLabels": {"app.kubernetes.io/component": "postgres"}}}
            },
            {
                "metadata": {"name": "parity-valkey"},
                "spec": {"selector": {"matchLabels": {"app.kubernetes.io/component": "valkey"}}}
            },
            {
                "metadata": {"name": "parity-clickhouse"},
                "spec": {"selector": {"matchLabels": {"app.kubernetes.io/component": "clickhouse"}}}
            }
        ]
    })
    .to_string()
}

fn installation_with_effective_values() -> &'static str {
    "version: 1\ninstall:\n  namespace: parity\n  release: parity\ncredentials:\n  model: CURIE_APPLY_TEST_MODEL_KEY\n  github_token: CURIE_APPLY_TEST_GITHUB_TOKEN\nplatform:\n  ui: false\n  inference: true\nset:\n  dispatcher.deploy: \"false\"\n  worker.replicas: \"3\"\n"
}

#[test]
fn absent_release_diff_matches_apply_dry_run_for_effective_installation_values() {
    let fixture = HelmFixture::new(installation_with_effective_values(), None);
    let env = [
        ("CURIE_APPLY_TEST_MODEL_KEY", MODEL_VALUE),
        ("CURIE_APPLY_TEST_GITHUB_TOKEN", GITHUB_VALUE),
        ("CURIE_MODEL", "runner-model-for-plan"),
    ];

    let apply = plan(fixture.apply_dry_run(&env));
    let diff = json_output(fixture.diff(&env), "diff");

    assert_eq!(diff["release_exists"], false, "{diff}");
    assert!(
        diff["changes"].as_u64().is_some_and(|changes| changes > 0),
        "an absent release must have create changes: {diff}"
    );
    assert!(
        apply.contains("agentSandbox.runner.credentials=model-va***"),
        "apply must use and mask the resolved model credential: {apply}"
    );
    assert!(
        apply.contains("api.githubToken=github-v***"),
        "apply must use and mask the resolved GitHub credential: {apply}"
    );
    assert!(
        !apply.contains(MODEL_VALUE) && !apply.contains(GITHUB_VALUE),
        "apply must not leak credential values: {apply}"
    );
    assert_added(&diff, "agentSandbox.runner.credentials", "<secret>");
    assert_added(&diff, "api.githubToken", "<secret>");

    let apply_values = helm_values(&apply);
    for (key, apply_value) in &apply_values {
        let diff_entry = entry(&diff, key);
        assert_eq!(diff_entry["kind"], "add", "{key}: {diff_entry}");
        let diff_value = diff_entry["to"]
            .as_str()
            .unwrap_or_else(|| panic!("diff entry has no target value for {key}: {diff_entry}"));
        if diff_value == "<secret>" {
            assert!(
                apply_value.ends_with("***"),
                "apply must expose only a masked value for {key}: {apply}"
            );
        } else {
            assert_eq!(apply_value, diff_value, "{key}: {diff_entry}");
        }
    }

    let diff_keys = diff["entries"]
        .as_array()
        .expect("diff entries array")
        .iter()
        .filter(|entry| entry["kind"] != "preserved")
        .map(|entry| entry["key"].as_str().expect("diff entry key"))
        .collect::<BTreeSet<_>>();
    let apply_keys = apply_values
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    assert_eq!(diff_keys, apply_keys, "apply plan: {apply}; diff: {diff}");
}

#[test]
fn empty_github_token_clear_is_shared_by_apply_and_diff() {
    let fixture = HelmFixture::new(
        "version: 1\ninstall:\n  namespace: parity\n  release: parity\nset:\n  api.githubToken: \"\"\n",
        None,
    );

    let apply = plan(fixture.apply_dry_run(&[]));
    let diff = json_output(fixture.diff(&[]), "diff");

    let apply_values = helm_values(&apply);
    assert_eq!(
        apply_values.get("api.githubToken").map(String::as_str),
        Some(""),
        "apply must carry the exact empty GitHub token clear: {apply}"
    );
    assert_added(&diff, "api.githubToken", "<secret>");
    assert_diff_keys(
        &diff,
        &[
            "api.githubToken",
            "langfuse.web.service.type",
            "ui.service.type",
        ],
    );
}

#[test]
fn diff_marks_only_extra_live_egress_for_reset_and_preserves_live_github_token() {
    let fixture = HelmFixture::new(
        "version: 1\ninstall:\n  namespace: parity\n  release: parity\nplatform:\n  egress:\n    - host: anthropic\n",
        Some(json!({
            "api": {"githubToken": "ghp-live-token"},
            "security": {"networkPolicy": {"allowedEgress": [
                {"cidr": "1.1.1.1/32", "ports": [{"port": 443, "protocol": "TCP"}]},
                {"cidr": "2606:4700:4700::1111/128", "ports": [{"port": 443, "protocol": "TCP"}]},
                {"cidr": "9.9.9.9/32", "ports": [{"port": 443, "protocol": "TCP"}]}
            ]}}
        })),
    );
    let egress = provider_egress_fixture();
    let diff = json_output(
        fixture.diff(&[("CURIE_TEST_PROVIDER_EGRESS_JSON", egress.as_str())]),
        "diff",
    );

    for key in [
        "security.networkPolicy.allowedEgress[0].cidr",
        "security.networkPolicy.allowedEgress[1].cidr",
        "security.networkPolicy.allowedEgress[0].ports[0].port",
        "security.networkPolicy.allowedEgress[1].ports[0].port",
    ] {
        assert_eq!(entry(&diff, key)["kind"], "unchanged", "{key}: {diff}");
    }
    for key in [
        "security.networkPolicy.allowedEgress[2].cidr",
        "security.networkPolicy.allowedEgress[2].ports[0].port",
    ] {
        assert_eq!(
            entry(&diff, key)["kind"],
            "reset to chart default",
            "{key}: {diff}"
        );
    }
    let github = entry(&diff, "api.githubToken");
    assert_eq!(github["kind"], "preserved", "{github}");
    assert_eq!(github["from"], "<secret>", "{github}");
    assert!(
        !diff.to_string().contains("ghp-live-token"),
        "diff must not leak the preserved GitHub token: {diff}"
    );
}

#[test]
fn apply_and_diff_report_the_same_curie_model_conflict() {
    let fixture = HelmFixture::new(
        "version: 1\ninstall:\n  namespace: parity\n  release: parity\nset:\n  agentSandbox.runner.model: file-model\n",
        None,
    );
    let env = [("CURIE_MODEL", "shell-model")];
    let apply = fixture.apply_dry_run(&env);
    let diff = fixture.diff(&env);

    assert_eq!(apply.status.code(), diff.status.code());
    let apply_error = json_error(apply, "apply --dry-run");
    let diff_error = json_error(diff, "diff");
    let apply_message = apply_error["error"]
        .as_str()
        .expect("apply error message string");
    assert!(
        apply_message.contains("CURIE_MODEL")
            && apply_message.contains("agentSandbox.runner.model"),
        "apply returned the wrong conflict error: {apply_error}"
    );
    assert_eq!(
        apply_error, diff_error,
        "apply and diff must use the same effective-plan conflict validation"
    );
}

#[test]
fn apply_with_both_flags_makes_no_cluster_call() {
    // #1351: --migrate-store and --allow-stateful-removal state contradictory
    // intent, and apply resolved the contradiction by silently dropping the
    // migration and taking the data destroying path. The primary assertion is
    // the ABSENCE of the mutation, not the wording of the refusal.
    let fixture = HelmFixture::new(installation_for_the_stateful_guard(), None);

    let output = fixture.apply(&["--migrate-store", "--allow-stateful-removal"], &[]);

    let calls = fixture.calls();
    assert!(
        calls.is_empty(),
        "a contradictory flag pair must touch no cluster at all; recorded calls:\n{calls}"
    );
    assert!(
        !calls.contains("upgrade"),
        "no upgrade may run for a rejected apply; recorded calls:\n{calls}"
    );
    assert!(
        !output.status.success(),
        "a contradictory flag pair must not exit successfully; stdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    // The two flag names only, never clap's full sentence, and no pinned exit
    // code: an operator needs to be told which two flags collided, and a clap
    // patch bump must not fail this.
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("--migrate-store") && stderr.contains("--allow-stateful-removal"),
        "the refusal must name both colliding flags; stderr:\n{stderr}"
    );
}

#[test]
fn migrate_store_alone_still_migrates() {
    // AC2: the control run. A live minio store plus a chart that renders rustfs
    // is the rename the guard exists for, and --migrate-store alone must still
    // take the migration branch.
    let fixture = HelmFixture::new(installation_for_the_stateful_guard(), None);

    let output = fixture.apply(
        &["--migrate-store"],
        &[("CURIE_TEST_KUBECTL_STS", &live_minio_statefulset())],
    );

    let calls = fixture.calls();
    assert!(
        output.status.success(),
        "--migrate-store alone must carry the migration through; stdout:\n{}\nstderr:\n{}\ncalls:\n{calls}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let live_read = calls
        .find("KUBECTL_CALL: get statefulset")
        .unwrap_or_else(|| panic!("the guard must read the live StatefulSets:\n{calls}"));
    let render = calls
        .find("HELM_CALL: template")
        .unwrap_or_else(|| panic!("the guard must render the target chart:\n{calls}"));
    let store_lookup = calls
        .find("KUBECTL_CALL: get svc")
        .unwrap_or_else(|| panic!("the migration must look up the store Service:\n{calls}"));
    let upgrade = calls
        .find("HELM_CALL: upgrade")
        .unwrap_or_else(|| panic!("the migration must upgrade the release:\n{calls}"));
    assert!(
        live_read < render && render < store_lookup && store_lookup < upgrade,
        "the migration branch runs the live read, then the render, then the store lookup, then the upgrade:\n{calls}"
    );
    // The export must be COMPLETE before the upgrade deletes the old store, and
    // the import must run after it. Staging alone, then upgrading, is the
    // failure mode that empties the store.
    let staged = calls
        .find("find /stage -type f | wc -l")
        .unwrap_or_else(|| panic!("the export must count what it staged:\n{calls}"));
    let imported = calls
        .find("aws s3 sync /stage")
        .unwrap_or_else(|| panic!("the import must load the staged objects back:\n{calls}"));
    assert!(
        staged < upgrade && upgrade < imported,
        "the export completes before the upgrade and the import runs after it:\n{calls}"
    );
    assert!(
        calls.contains("KUBECTL_CALL: delete pod"),
        "a verified migration releases the staging pod:\n{calls}"
    );
}

#[test]
fn migrate_store_refuses_a_mixed_removed_and_renamed_batch() {
    let fixture = HelmFixture::new(installation_for_the_stateful_guard(), None);
    let live_statefulsets = live_mixed_store_statefulsets();

    let output = fixture.apply(
        &["--migrate-store"],
        &[
            ("CURIE_TEST_HELM_MIXED_STATEFULSETS", "1"),
            ("CURIE_TEST_KUBECTL_STS", &live_statefulsets),
        ],
    );

    let calls = fixture.calls();
    let error = json_error(output, "apply --migrate-store");
    let message = error["error"].as_str().expect("apply error message string");
    assert!(
        message.contains("nameOverride"),
        "a renamed StatefulSet must direct the operator to nameOverride:\n{message}"
    );
    assert!(
        message.contains("parity-minio")
            && message.contains("parity-postgres")
            && message.contains("parity-curie-postgres"),
        "the refusal must include the removed store and renamed StatefulSet:\n{message}"
    );
    assert!(
        !calls.contains("KUBECTL_CALL: run "),
        "a mixed batch must stop before the migration export:\n{calls}"
    );
    assert!(
        !calls.contains("HELM_CALL: upgrade"),
        "a mixed batch must stop before the upgrade:\n{calls}"
    );
}

#[test]
fn migrate_store_does_not_read_a_failed_cluster_read_as_a_removal() {
    // #1351, the other face of the same defect. While the refusal was the only
    // error the guard could raise, `--migrate-store` read any Err as "a removal
    // was found" and started moving data. Once the guard could also fail
    // because the cluster was unreadable, that reading promoted "I could not
    // find out" to "definitely at risk, start staging" -- and the run then died
    // inside the export with "nothing to migrate", a message about a decision
    // the operator never made, blaming the wrong thing.
    let fixture = HelmFixture::new(installation_for_the_stateful_guard(), None);

    let output = fixture.apply(&["--migrate-store"], &[("CURIE_TEST_KUBECTL_FAIL", "1")]);

    let calls = fixture.calls();
    assert!(
        calls.contains("KUBECTL_CALL: get statefulset"),
        "the guard must have tried to read the live StatefulSets:\n{calls}"
    );
    assert!(
        !calls.contains("KUBECTL_CALL: run "),
        "an unreadable cluster must not start an export:\n{calls}"
    );
    assert!(
        !calls.contains("HELM_CALL: upgrade"),
        "an unreadable cluster must stop the apply before the upgrade:\n{calls}"
    );
    // ADR-0021's exit-code contract: an automation loop branches on the code
    // rather than parsing prose, so an apiserver rolling restart reports exit 3
    // Transient (retry the same argv) and not exit 1 Failure, which reads as
    // "stop".
    assert_eq!(
        output.status.code(),
        Some(3),
        "an unreachable apiserver is transient; stdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let error = json_error(output, "apply --migrate-store");
    let message = error["error"].as_str().expect("error message string");
    assert!(
        message.contains("The connection to the server localhost:8080 was refused"),
        "the failure must carry kubectl's own words: {error}"
    );
    assert!(
        !message.contains("nothing to migrate"),
        "a failed read must not be reported as a migration decision: {error}"
    );
}

#[test]
fn allow_stateful_removal_alone_still_proceeds() {
    // AC3: the override short circuits the guard entirely, so the same live
    // minio store that stops a plain apply proceeds straight to the upgrade.
    let fixture = HelmFixture::new(installation_for_the_stateful_guard(), None);

    let output = fixture.apply(
        &["--allow-stateful-removal"],
        &[("CURIE_TEST_KUBECTL_STS", &live_minio_statefulset())],
    );

    let calls = fixture.calls();
    assert!(
        output.status.success(),
        "--allow-stateful-removal alone must still apply; stdout:\n{}\nstderr:\n{}\ncalls:\n{calls}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        calls.contains("HELM_CALL: upgrade"),
        "the upgrade must run:\n{calls}"
    );
    assert!(
        !calls.contains("KUBECTL_CALL: get statefulset"),
        "the override skips the guard, so nothing reads the live StatefulSets:\n{calls}"
    );
}

#[test]
fn a_failed_kubectl_read_fails_the_apply() {
    // AC4, the core anti regression for #1351: a kubectl read that FAILED was
    // returned as an empty list, which told the guard "fresh install, nothing
    // to lose" while the upgrade went on to prune the StatefulSet.
    let fixture = HelmFixture::new(installation_for_the_stateful_guard(), None);

    let output = fixture.apply(&[], &[("CURIE_TEST_KUBECTL_FAIL", "1")]);

    let calls = fixture.calls();
    assert!(
        !calls.contains("HELM_CALL: upgrade"),
        "an unreadable cluster must stop the apply before the upgrade:\n{calls}"
    );
    assert!(
        !output.status.success(),
        "an unreadable cluster must not exit successfully; stdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let reported = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        reported.contains("The connection to the server localhost:8080 was refused"),
        "the failure must carry kubectl's own words, so a guard failure reads differently from a refusal:\n{reported}"
    );
}

#[test]
fn a_forbidden_kubectl_read_fails_the_apply_as_a_permanent_failure() {
    // Closes a gap the spec reviewer named for #1351: the only kubectl-failure
    // stderr both `a_failed_kubectl_read_fails_the_apply` and
    // `migrate_store_does_not_read_a_failed_cluster_read_as_a_removal` drive is
    // a CONNECTIVITY failure. Nothing pinned the sibling branch,
    // `is_connectivity_failure(stderr) == false`, which is what an RBAC
    // Forbidden denial takes. A regression shaped as "swallow the failure only
    // when it is NOT a connectivity failure" would leave both of those tests
    // green while restoring the exact vacuous-pass data-loss bug for a
    // Forbidden read: exit 0, no error, the guard reading "fresh install,
    // nothing to lose", and the upgrade pruning a live StatefulSet.
    let fixture = HelmFixture::new(installation_for_the_stateful_guard(), None);

    let output = fixture.apply(&[], &[("CURIE_TEST_KUBECTL_FORBIDDEN", "1")]);

    let calls = fixture.calls();
    assert!(
        !calls.contains("HELM_CALL: upgrade"),
        "an RBAC denied cluster read must stop the apply before the upgrade:\n{calls}"
    );
    // ADR-0021's exit-code contract: exit 3 Transient means "retry the same
    // argv", which is wrong advice for a permission denial that will not
    // clear on its own. This is the assertion that actually closes the gap:
    // it can only pass if the non-connectivity branch runs at all, which the
    // vacuous-pass shape of the bug would never reach.
    assert_eq!(
        output.status.code(),
        Some(1),
        "a Forbidden cluster read is a permanent failure, not transient or a silent success; stdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let error = json_error(output, "apply");
    let message = error["error"].as_str().expect("error message string");
    assert!(
        message.contains("cannot list resource \"statefulsets\""),
        "the failure must carry kubectl's own Forbidden wording, so an operator can tell an RBAC problem from an unreachable cluster: {error}"
    );
}

#[test]
fn a_namespace_with_no_statefulsets_still_passes_the_guard() {
    // AC5, the trap guard. An empty items array with exit 0 is what a real
    // namespaced LIST returns for a fresh install AND for a namespace that does
    // not exist, so it is a genuine "nothing to lose" and must still apply.
    let fixture = HelmFixture::new(installation_for_the_stateful_guard(), None);

    let output = fixture.apply(&[], &[]);

    let calls = fixture.calls();
    assert!(
        output.status.success(),
        "an empty namespace must still apply; stdout:\n{}\nstderr:\n{}\ncalls:\n{calls}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        calls.contains("HELM_CALL: upgrade"),
        "the upgrade must run:\n{calls}"
    );
}
