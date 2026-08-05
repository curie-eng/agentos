use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
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

struct HelmFixture {
    temp: tempfile::TempDir,
    file: PathBuf,
    live_values: Option<String>,
}

impl HelmFixture {
    fn new(config: &str, live_values: Option<Value>) -> Self {
        let temp = tempfile::tempdir().expect("tempdir");
        let file = temp.path().join("curie.yaml");
        fs::write(&file, config).expect("write curie.yaml");

        let bin_dir = temp.path().join("bin");
        fs::create_dir(&bin_dir).expect("create stub bin directory");
        let helm = bin_dir.join("helm");
        fs::write(
            &helm,
            r#"#!/bin/sh
if [ "$1" = get ] && [ "$2" = values ]; then
    if [ "${CURIE_TEST_HELM_ABSENT:-}" = 1 ]; then
        printf '%s\n' 'release not found' >&2
        exit 1
    fi
    printf '%s\n' "$CURIE_TEST_HELM_VALUES"
    exit 0
fi
printf 'unexpected helm invocation: %s\n' "$*" >&2
exit 64
"#,
        )
        .expect("write helm stub");
        let mut permissions = fs::metadata(&helm).expect("helm metadata").permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&helm, permissions).expect("make helm stub executable");

        Self {
            temp,
            file,
            live_values: live_values.map(|values| values.to_string()),
        }
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
