//! Binary contract for facts inferred by `curie cluster up`.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const TARGET_RELEASE: &str = "target-release";
const TARGET_NAMESPACE: &str = "target-namespace";
const OPENROUTER_CREDENTIAL: &str = "sk-or-v1-PLACEHOLDER";
const ANTHROPIC_CREDENTIAL: &str = "sk-ant-api03-PLACEHOLDER";
const AMBIGUOUS_CREDENTIAL: &str = "sk-MOONSHOT-PLACEHOLDER";
const VALID_RESOLVER: &str = r#"{
  "openrouter.ai": ["1.1.1.1"],
  "api.anthropic.com": ["8.8.8.8"],
  "api.moonshot.ai": ["9.9.9.9"]
}"#;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn chart() -> &'static str {
    concat!(env!("CARGO_MANIFEST_DIR"), "/../charts/curie")
}

fn write_exec(dir: &Path, name: &str, body: &str) {
    let path = dir.join(name);
    fs::write(&path, body).unwrap_or_else(|error| panic!("write {name}: {error}"));
    let mut permissions = fs::metadata(&path)
        .unwrap_or_else(|error| panic!("read {name} metadata: {error}"))
        .permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&path, permissions)
        .unwrap_or_else(|error| panic!("make {name} executable: {error}"));
}

struct Fixture {
    _temp: tempfile::TempDir,
    bin_dir: PathBuf,
    helm_log: PathBuf,
    upgrade_log: PathBuf,
    existing_values: String,
}

impl Fixture {
    fn new(existing_values: &str) -> Self {
        let temp = tempfile::tempdir().expect("temporary directory");
        let bin_dir = temp.path().join("bin");
        fs::create_dir(&bin_dir).expect("create fake binary directory");
        let helm_log = temp.path().join("helm.log");
        let upgrade_log = temp.path().join("upgrades.log");

        write_exec(
            &bin_dir,
            "helm",
            r#"#!/bin/sh
printf '%s\n' "$*" >> "$CURIE_TEST_HELM_LOG"

if [ "$1" = "get" ] && [ "$2" = "values" ]; then
    if [ -z "$CURIE_TEST_EXISTING_VALUES" ]; then
        printf '%s\n' 'Error: release: not found' >&2
        exit 1
    fi
    printf '%s\n' "$CURIE_TEST_EXISTING_VALUES"
    exit 0
fi

if [ "$1" = "template" ]; then
    case " $* " in
        *" --show-only templates/priorityclass.yaml "*|*" --show-only=templates/priorityclass.yaml "*)
            printf '%s\n' 'Error: could not find template templates/priorityclass.yaml in chart' >&2
            exit 1
            ;;
        *" --show-only templates/preflight-gvisor.yaml "*|*" --show-only=templates/preflight-gvisor.yaml "*)
            printf '%s\n' 'Error: could not find template templates/preflight-gvisor.yaml in chart' >&2
            exit 1
            ;;
    esac
    printf 'unexpected helm template invocation: %s\n' "$*" >&2
    exit 64
fi

if [ "$1" = "upgrade" ] && [ "$2" = "--install" ]; then
    printf '%s\n' "$*" >> "$CURIE_TEST_UPGRADE_LOG"
    exit 0
fi

printf 'unexpected helm invocation: %s\n' "$*" >&2
exit 64
"#,
        );

        write_exec(
            &bin_dir,
            "kubectl",
            r#"#!/bin/sh
if [ "$1" = "get" ] && [ "$2" = "namespace" ]; then
    exit 0
fi

case " $* " in
    *" get deployment agent-sandbox-controller "*)
        case " $* " in
            *" -n agent-sandbox-system "*) exit 0 ;;
            *)
                printf 'controller query was not scoped to agent-sandbox-system: %s\n' "$*" >&2
                exit 64
                ;;
        esac
        ;;
esac

printf 'unexpected kubectl invocation: %s\n' "$*" >&2
exit 64
"#,
        );

        Self {
            _temp: temp,
            bin_dir,
            helm_log,
            upgrade_log,
            existing_values: existing_values.to_string(),
        }
    }

    fn run(
        &self,
        credential_environment: &[(&str, &str)],
        resolver: &str,
        extra: &[&str],
    ) -> Output {
        let mut paths = vec![self.bin_dir.clone()];
        if let Some(current) = std::env::var_os("PATH") {
            paths.extend(std::env::split_paths(&current));
        }
        let path = std::env::join_paths(paths).expect("join PATH");

        let mut args = vec![
            "--color",
            "never",
            "cluster",
            "up",
            "--chart",
            chart(),
            "--namespace",
            TARGET_NAMESPACE,
            "--release",
            TARGET_RELEASE,
            "--dev",
            "--no-expose",
        ];
        args.extend_from_slice(extra);

        let mut command = Command::new(bin());
        command
            .args(args)
            .env("PATH", path)
            .env("CI", "1")
            .env("TERM", "dumb")
            .env("NO_COLOR", "1")
            .env("CURIE_TEST_HELM_LOG", &self.helm_log)
            .env("CURIE_TEST_UPGRADE_LOG", &self.upgrade_log)
            .env("CURIE_TEST_EXISTING_VALUES", &self.existing_values)
            .env("CURIE_TEST_PROVIDER_EGRESS_JSON", resolver)
            .env_remove("CURIE_CREDENTIALS")
            .env_remove("CURIE_MODEL_CREDENTIALS")
            .env_remove("CURIE_GITHUB_TOKEN")
            .env_remove("CURIE_MODEL");
        for (key, value) in credential_environment {
            command.env(key, value);
        }
        command.output().expect("run curie cluster up")
    }

    fn helm_log(&self) -> String {
        fs::read_to_string(&self.helm_log).unwrap_or_default()
    }

    fn upgrade_log(&self) -> String {
        fs::read_to_string(&self.upgrade_log).unwrap_or_default()
    }

    fn upgrade_count(&self) -> usize {
        self.upgrade_log().lines().count()
    }
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn all_output(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

fn assert_success(fixture: &Fixture, output: &Output) {
    assert!(
        output.status.success(),
        "cluster up failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        stderr(output)
    );
    assert_eq!(fixture.upgrade_count(), 1, "Helm must install exactly once");
}

fn assert_inference_once(shown: &str, applied_override: &str) {
    assert_eq!(
        shown.matches(applied_override).count(),
        1,
        "the applied override must be disclosed exactly once: {applied_override}\n{shown}"
    );
}

#[test]
fn recognized_credential_prefixes_infer_provider_egress() {
    for (credential, provider, cidr) in [
        (ANTHROPIC_CREDENTIAL, "anthropic", "8.8.8.8/32"),
        (OPENROUTER_CREDENTIAL, "openrouter", "1.1.1.1/32"),
    ] {
        let fixture = Fixture::new("");
        let output = fixture.run(&[("CURIE_CREDENTIALS", credential)], VALID_RESOLVER, &[]);
        assert_success(&fixture, &output);

        let shown = stderr(&output);
        assert_inference_once(&shown, &format!("--allow-egress-host {provider}"));
        assert!(
            fixture.upgrade_log().contains(cidr),
            "the inferred {provider} route must reach Helm"
        );
        assert!(
            !shown.contains("sandbox is sealed"),
            "inferred provider egress must remove the sealed warning: {shown}"
        );
        assert!(
            !all_output(&output).contains(credential),
            "credential leaked"
        );
    }
}

#[test]
fn explicit_provider_list_containing_the_detected_provider_wins_silently() {
    let fixture = Fixture::new("");
    let output = fixture.run(
        &[("CURIE_CREDENTIALS", OPENROUTER_CREDENTIAL)],
        VALID_RESOLVER,
        &[
            "--allow-egress-host",
            "openrouter",
            "--allow-egress-host",
            "anthropic",
        ],
    );
    assert_success(&fixture, &output);

    let shown = stderr(&output);
    assert!(
        !shown.contains("--allow-egress-host openrouter"),
        "an explicit list must not be reported as inferred: {shown}"
    );
    let upgrade = fixture.upgrade_log();
    assert!(upgrade.contains("1.1.1.1/32"), "{upgrade}");
    assert!(upgrade.contains("8.8.8.8/32"), "{upgrade}");
}

#[test]
fn provider_contradiction_fails_before_resolver_or_helm_without_secret_bytes() {
    let fixture = Fixture::new("");
    let output = fixture.run(
        &[("CURIE_CREDENTIALS", OPENROUTER_CREDENTIAL)],
        "not resolver JSON",
        &["--allow-egress-host", "anthropic"],
    );
    let shown = all_output(&output);

    assert_eq!(
        output.status.code(),
        Some(2),
        "a detected provider contradiction is a usage error: {shown}"
    );
    assert!(shown.contains("openrouter"), "{shown}");
    assert!(shown.contains("--allow-egress-host anthropic"), "{shown}");
    assert!(
        !shown.contains("resolver JSON"),
        "the resolver ran: {shown}"
    );
    assert!(
        !shown.contains(OPENROUTER_CREDENTIAL),
        "credential leaked: {shown}"
    );
    assert!(
        fixture.helm_log().is_empty(),
        "no Helm read or mutation may precede the contradiction: {}",
        fixture.helm_log()
    );
}

#[test]
fn ambiguous_credentials_stay_sealed_unless_an_explicit_provider_wins() {
    let sealed = Fixture::new("");
    let output = sealed.run(
        &[("CURIE_CREDENTIALS", AMBIGUOUS_CREDENTIAL)],
        VALID_RESOLVER,
        &[],
    );
    assert_success(&sealed, &output);
    let shown = stderr(&output);
    assert!(shown.contains("sandbox is sealed"), "{shown}");
    assert!(!shown.contains("--allow-egress-host anthropic"), "{shown}");
    assert!(!shown.contains("--allow-egress-host openrouter"), "{shown}");
    assert!(!sealed.upgrade_log().contains("allowedEgress"));

    let explicit = Fixture::new("");
    let output = explicit.run(
        &[("CURIE_CREDENTIALS", AMBIGUOUS_CREDENTIAL)],
        VALID_RESOLVER,
        &["--allow-egress-host", "moonshot"],
    );
    assert_success(&explicit, &output);
    let shown = stderr(&output);
    assert!(!shown.contains("sandbox is sealed"), "{shown}");
    assert!(!shown.contains("--allow-egress-host moonshot"), "{shown}");
    assert!(explicit.upgrade_log().contains("9.9.9.9/32"));
}

#[test]
fn inference_uses_the_effective_credential_precedence() {
    let canonical = Fixture::new("");
    let output = canonical.run(
        &[
            ("CURIE_CREDENTIALS", OPENROUTER_CREDENTIAL),
            ("CURIE_MODEL_CREDENTIALS", ANTHROPIC_CREDENTIAL),
        ],
        VALID_RESOLVER,
        &[],
    );
    assert_success(&canonical, &output);
    assert!(canonical.upgrade_log().contains("1.1.1.1/32"));
    assert!(!canonical.upgrade_log().contains("8.8.8.8/32"));

    let final_helm_value = Fixture::new("");
    let explicit_credential = format!("agentSandbox.runner.credentials={ANTHROPIC_CREDENTIAL}");
    let output = final_helm_value.run(
        &[("CURIE_CREDENTIALS", OPENROUTER_CREDENTIAL)],
        VALID_RESOLVER,
        &["--set-string", explicit_credential.as_str()],
    );
    assert_success(&final_helm_value, &output);
    assert!(final_helm_value.upgrade_log().contains("8.8.8.8/32"));
    assert!(!final_helm_value.upgrade_log().contains("1.1.1.1/32"));

    let preserved =
        Fixture::new(r#"{"agentSandbox":{"runner":{"credentials":"sk-or-v1-PLACEHOLDER"}}}"#);
    let output = preserved.run(&[], VALID_RESOLVER, &[]);
    assert_success(&preserved, &output);
    assert!(preserved.upgrade_log().contains("1.1.1.1/32"));
    assert_inference_once(&stderr(&output), "--allow-egress-host openrouter");
}
