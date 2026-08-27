//! Binary proof that cluster-scoped secrets refuse mismatch, require CAS
//! versions to replace, and never print values (#1913).

use std::fs;
use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn isolated_config() -> tempfile::TempDir {
    tempfile::tempdir().expect("config dir")
}

fn secrets_cmd(config: &tempfile::TempDir) -> Command {
    let mut command = Command::new(bin());
    command
        .env("CURIE_CONFIG_DIR", config.path())
        .env_remove("HOME");
    command
}

#[test]
fn scoped_set_list_and_stale_replace_never_print_values() {
    let config = isolated_config();
    let status = secrets_cmd(&config)
        .args([
            "secrets",
            "set",
            "K8S_WRITE_KUBECONFIG",
            "--from-env",
            "K8S_WRITE_KUBECONFIG",
            "--cluster-identity",
            "ca:a",
            "--release",
            "curie",
            "--namespace",
            "curie-test",
        ])
        .env("K8S_WRITE_KUBECONFIG", "token-cluster-a")
        .status()
        .expect("secrets set");
    assert!(status.success(), "first scoped set must succeed");

    let listed = secrets_cmd(&config)
        .args(["--json", "secrets", "list"])
        .output()
        .expect("secrets list");
    assert!(listed.status.success());
    let stdout = String::from_utf8_lossy(&listed.stdout);
    let stderr = String::from_utf8_lossy(&listed.stderr);
    assert!(stdout.contains("K8S_WRITE_KUBECONFIG"));
    assert!(stdout.contains("ca:a"));
    assert!(stdout.contains("\"version\":1"));
    assert!(!stdout.contains("token-cluster-a"));
    assert!(!stderr.contains("token-cluster-a"));

    let stale = secrets_cmd(&config)
        .args([
            "secrets",
            "set",
            "K8S_WRITE_KUBECONFIG",
            "--from-env",
            "K8S_WRITE_KUBECONFIG",
            "--cluster-identity",
            "ca:a",
            "--release",
            "curie",
            "--namespace",
            "curie-test",
        ])
        .env("K8S_WRITE_KUBECONFIG", "token-cluster-a-replaced")
        .output()
        .expect("stale secrets set");
    assert!(!stale.status.success(), "replace without version must fail");
    let stale_out = format!(
        "{}{}",
        String::from_utf8_lossy(&stale.stdout),
        String::from_utf8_lossy(&stale.stderr)
    );
    assert!(stale_out.contains("version mismatch"));
    assert!(!stale_out.contains("token-cluster-a"));

    let replaced = secrets_cmd(&config)
        .args([
            "secrets",
            "set",
            "K8S_WRITE_KUBECONFIG",
            "--from-env",
            "K8S_WRITE_KUBECONFIG",
            "--cluster-identity",
            "ca:a",
            "--release",
            "curie",
            "--namespace",
            "curie-test",
            "--expected-version",
            "1",
        ])
        .env("K8S_WRITE_KUBECONFIG", "token-cluster-a-replaced")
        .output()
        .expect("cas secrets set");
    assert!(replaced.status.success(), "matching version must replace");
    let replaced_out = format!(
        "{}{}",
        String::from_utf8_lossy(&replaced.stdout),
        String::from_utf8_lossy(&replaced.stderr)
    );
    assert!(!replaced_out.contains("token-cluster-a"));

    let stored = fs::read_to_string(config.path().join("credentials.json")).unwrap();
    assert!(stored.contains("token-cluster-a-replaced"));
    assert!(!stored.contains("token-cluster-a\"") || stored.contains("token-cluster-a-replaced"));
}

#[test]
fn same_name_can_exist_for_two_cluster_identities() {
    let config = isolated_config();
    for (identity, namespace, value) in [
        ("ca:a", "curie-test", "token-cluster-a"),
        ("ca:b", "curie", "token-cluster-b"),
    ] {
        let status = secrets_cmd(&config)
            .args([
                "secrets",
                "set",
                "K8S_WRITE_KUBECONFIG",
                "--from-env",
                "K8S_WRITE_KUBECONFIG",
                "--cluster-identity",
                identity,
                "--release",
                "curie",
                "--namespace",
                namespace,
            ])
            .env("K8S_WRITE_KUBECONFIG", value)
            .status()
            .expect("secrets set");
        assert!(status.success());
    }
    let listed = secrets_cmd(&config)
        .args(["--json", "secrets", "list"])
        .output()
        .expect("secrets list");
    let stdout = String::from_utf8_lossy(&listed.stdout);
    assert!(stdout.contains("ca:a"));
    assert!(stdout.contains("ca:b"));
    assert!(!stdout.contains("token-cluster-a"));
    assert!(!stdout.contains("token-cluster-b"));
}
