//! Exercise the actual CLI upgrade driver across its external Helm/Kubernetes
//! process boundary. These recording executables are plumbing regressions;
//! the released-upgrade matrix remains the real cluster acceptance gate.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::process::{Command, Output};

use serde_json::{json, Value};

struct Fixture {
    temp: tempfile::TempDir,
}

impl Fixture {
    fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        for name in ["helm", "kubectl"] {
            let path = temp.path().join(name);
            fs::write(&path, include_str!("data/upgrade-driver.py")).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
        }
        fs::write(temp.path().join("values.json"), "{}").unwrap();
        fs::write(
            temp.path().join("candidate-chart"),
            "immutable chart fixture",
        )
        .unwrap();
        Self { temp }
    }

    fn path(&self, name: &str) -> PathBuf {
        self.temp.path().join(name)
    }

    fn values(&self, values: Value) {
        fs::write(
            self.path("values.json"),
            serde_json::to_vec(&values).unwrap(),
        )
        .unwrap();
    }

    fn run(&self, scenario: &str) -> Output {
        self.run_args(scenario, &[])
    }

    fn command(&self, scenario: &str, extra: &[&str]) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_curie"));
        command
            .args([
                "--json",
                "cluster",
                "upgrade",
                "--to",
                "0.9.0",
                "--chart",
                self.path("candidate-chart").to_str().unwrap(),
                "--namespace",
                "upgrade-test",
                "--release",
                "acme-bot",
                "--yes",
            ])
            .args(extra)
            .env(
                "PATH",
                format!("{}:/usr/bin:/bin", self.temp.path().display()),
            )
            .env("UPGRADE_DRIVER_ROOT", self.temp.path())
            .env("UPGRADE_DRIVER_SCENARIO", scenario);
        command
    }

    fn run_args(&self, scenario: &str, extra: &[&str]) -> Output {
        self.command(scenario, extra).output().unwrap()
    }

    fn run_status(&self) -> Output {
        Command::new(env!("CARGO_BIN_EXE_curie"))
            .args([
                "--json",
                "cluster",
                "status",
                "--namespace",
                "upgrade-test",
                "--release",
                "acme-bot",
            ])
            .env(
                "PATH",
                format!("{}:/usr/bin:/bin", self.temp.path().display()),
            )
            .env("UPGRADE_DRIVER_ROOT", self.temp.path())
            .env("UPGRADE_DRIVER_SCENARIO", "healthy")
            .output()
            .unwrap()
    }

    fn calls(&self) -> String {
        fs::read_to_string(self.path("calls.jsonl")).unwrap_or_default()
    }

    fn assert_refused_without_upgrade(&self, output: &Output) {
        assert!(
            !output.status.success(),
            "must refuse: {}",
            String::from_utf8_lossy(&output.stdout)
        );
        assert!(
            !self.calls().contains("\"helm\", \"upgrade\""),
            "must not apply Helm after refusal"
        );
    }
}

#[test]
fn checkpoint_failure_stops_before_helm_mutation() {
    let fixture = Fixture::new();
    let output = fixture.run("checkpoint-fails");
    fixture.assert_refused_without_upgrade(&output);
    assert!(String::from_utf8_lossy(&output.stdout).contains("checkpoint"));
}

#[test]
fn retained_configuration_conflict_refuses_before_checkpoint_or_upgrade() {
    let fixture = Fixture::new();
    fixture.values(json!({"worker": {
        "runnerTotalTimeoutSeconds": 90,
        "extraEnv": [{"name": "CURIE_RUNNER_TOTAL_TIMEOUT_S", "value": "120"}]
    }}));
    let output = fixture.run("healthy");
    fixture.assert_refused_without_upgrade(&output);
    for mutation in ["apply", "create", "replace"] {
        assert!(!fixture
            .calls()
            .contains(&format!("\"kubectl\", \"{mutation}\"")));
    }
}

#[test]
fn actual_upgrade_uses_migrated_retained_values_and_external_secret_references() {
    let fixture = Fixture::new();
    fixture.values(json!({
        "worker": {
            "adapterCredentialsExistingSecret": "acme-adapter",
            "adapterCredentialsExistingSecretKey": "adapter-key",
            "adapterCredentials": "fixture-inline-must-not-return",
            "extraEnv": [{"name": "CURIE_RUNNER_TOTAL_TIMEOUT_S", "value": "1200"}]
        },
        "ui": {"deploy": false}
    }));
    let output = fixture.run("healthy");
    assert!(
        fixture.path("applied-values.json").exists(),
        "upgrade reached apply: {}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert!(
        output.status.success(),
        "healthy upgrade failed: {}",
        String::from_utf8_lossy(&output.stdout)
    );
    let values: Value =
        serde_json::from_slice(&fs::read(fixture.path("applied-values.json")).unwrap()).unwrap();
    assert_eq!(values["config"]["schemaVersion"], "0.9.0");
    assert_eq!(values["worker"]["runnerTotalTimeoutSeconds"], 1200.0);
    assert_eq!(
        values["worker"]["adapterCredentialsExistingSecret"],
        "acme-adapter"
    );
    assert_eq!(
        values["worker"]["adapterCredentialsExistingSecretKey"],
        "adapter-key"
    );
    assert!(values["worker"].get("adapterCredentials").is_none());
    assert_eq!(values["ui"]["deploy"], false);
    assert!(!String::from_utf8_lossy(&output.stdout).contains("fixture-inline-must-not-return"));
}

#[test]
fn chart_target_mismatch_refuses_before_checkpoint_or_upgrade() {
    let fixture = Fixture::new();
    let output = fixture.run("wrong-chart");
    fixture.assert_refused_without_upgrade(&output);
    for mutation in ["apply", "create", "replace"] {
        assert!(!fixture
            .calls()
            .contains(&format!("\"kubectl\", \"{mutation}\"")));
    }
}

#[test]
fn helm_read_error_cannot_be_reclassified_as_fresh_install() {
    let fixture = Fixture::new();
    let output = fixture.run("helm-forbidden");
    fixture.assert_refused_without_upgrade(&output);
    for mutation in ["apply", "create", "replace"] {
        assert!(!fixture
            .calls()
            .contains(&format!("\"kubectl\", \"{mutation}\"")));
    }
}

#[test]
fn malformed_checkpoint_cannot_be_discarded_and_overwritten() {
    let fixture = Fixture::new();
    fs::write(fixture.path("record.json"), "not valid json").unwrap();
    let output = fixture.run("healthy");
    fixture.assert_refused_without_upgrade(&output);
    assert_eq!(
        fs::read_to_string(fixture.path("record.json")).unwrap(),
        "not valid json"
    );
}

#[test]
fn healthy_replica_counts_do_not_hide_wrong_image_or_stale_generation() {
    for scenario in ["wrong-image", "stale-generation", "wrong-manifest"] {
        let fixture = Fixture::new();
        let output = fixture.run(scenario);
        assert!(!output.status.success(), "{scenario} cannot report success");
        let result: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(result["status"], "failed", "{scenario}");
        assert_eq!(result["convergence"]["exact"], false, "{scenario}");
        assert_eq!(result["known_good_version"], "0.8.5", "{scenario}");
    }
}

#[test]
fn failed_real_canary_cannot_commit_a_healthy_looking_release() {
    let fixture = Fixture::new();
    let output = fixture.run("canary-fails");
    assert_eq!(output.status.code(), Some(1));
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(result["status"], "failed");
    assert_eq!(result["canary"]["passed"], false);
    assert_eq!(result["known_good_version"], "0.8.5");
}

#[test]
fn secret_string_data_is_compared_to_persisted_data_without_disclosure() {
    let fixture = Fixture::new();
    let output = fixture.run("secret-string-data");
    assert!(
        output.status.success(),
        "persisted Secret bytes must converge: {}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert!(!String::from_utf8_lossy(&output.stdout).contains("fixture-secret-value"));
}

#[test]
fn failed_canary_resumes_the_same_attempt_without_reapplying_helm() {
    let fixture = Fixture::new();
    let failed = fixture.run("canary-fails");
    assert_eq!(failed.status.code(), Some(1));
    let resumed = fixture.run("healthy");
    assert!(
        resumed.status.success(),
        "{}",
        String::from_utf8_lossy(&resumed.stdout)
    );
    let result: Value = serde_json::from_slice(&resumed.stdout).unwrap();
    assert_eq!(result["resumed"], true);
    assert_eq!(fixture.calls().matches("\"helm\", \"upgrade\"").count(), 1);
    assert_eq!(result["from_version"], "0.8.5");
}

#[test]
fn every_persisted_phase_resumes_without_replaying_a_completed_apply() {
    // Drain and migration are Helm-owned hooks within Apply. Their individual
    // interruption matrix is a separate real-cluster gate, not this fixture.
    for phase in ["validate", "apply", "converge", "canary", "commit"] {
        let fixture = Fixture::new();
        let interrupted = fixture.run(&format!("interrupt-after-{phase}"));
        assert!(!interrupted.status.success(), "{phase} must interrupt");
        let resumed = fixture.run("healthy");
        assert!(
            resumed.status.success(),
            "{phase}: {}",
            String::from_utf8_lossy(&resumed.stdout)
        );
        let result: Value = serde_json::from_slice(&resumed.stdout).unwrap();
        assert_eq!(result["resumed"], true, "{phase}");
        assert_eq!(
            fixture.calls().matches("\"helm\", \"upgrade\"").count(),
            1,
            "{phase}"
        );
    }
}

#[test]
fn stale_checkpoint_version_refuses_concurrent_coordinators() {
    // Kubernetes updates must carry resourceVersion; stale writes return 409:
    // https://kubernetes.io/docs/reference/using-api/api-concepts/#resource-versions
    let fixture = Fixture::new();
    assert!(fixture.run("healthy").status.success());
    let conflicting = fixture.run("checkpoint-conflict");
    assert!(
        !conflicting.status.success(),
        "stale checkpoint cannot be overwritten"
    );
    assert!(String::from_utf8_lossy(&conflicting.stdout).contains("checkpoint"));
    assert_eq!(fixture.calls().matches("\"helm\", \"upgrade\"").count(), 1);
}

#[test]
fn schema_contract_and_unverifiable_database_refuse_before_any_mutation() {
    for scenario in [
        "schema-contract",
        "schema-unknown",
        "schema-probe-fails",
        "schema-metadata-mismatch",
    ] {
        let fixture = Fixture::new();
        let output = fixture.run(scenario);
        fixture.assert_refused_without_upgrade(&output);
        for mutation in ["apply", "create", "replace"] {
            assert!(
                !fixture
                    .calls()
                    .contains(&format!("\"kubectl\", \"{mutation}\"")),
                "{scenario}"
            );
        }
    }
}

#[test]
fn explicit_forward_only_allows_pending_contract_and_records_decision() {
    let fixture = Fixture::new();
    fixture.values(json!({"api": {"migrate": {"forwardOnly": true}}}));
    let output = fixture.run("schema-contract");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    let record: Value =
        serde_json::from_slice(&fs::read(fixture.path("record.json")).unwrap()).unwrap();
    assert!(record["plan"]
        .as_array()
        .unwrap()
        .iter()
        .any(|line| line.as_str().unwrap().contains("forward-only")));
}

#[test]
fn failed_helm_hook_cannot_record_unexecuted_phases_as_completed() {
    let fixture = Fixture::new();
    let failed = fixture.run("helm-hook-fails");
    assert!(!failed.status.success());
    let record: Value =
        serde_json::from_slice(&fs::read(fixture.path("record.json")).unwrap()).unwrap();
    assert_eq!(record["completed"], json!(["plan", "validate"]));
    assert_eq!(record["drain_completed"], false);
}

#[test]
fn lost_success_reply_reconciles_the_helm_revision_without_a_second_apply() {
    let fixture = Fixture::new();
    assert!(!fixture.run("helm-success-reply-lost").status.success());
    let resumed = fixture.run("healthy");
    assert!(
        resumed.status.success(),
        "{}",
        String::from_utf8_lossy(&resumed.stdout)
    );
    assert_eq!(fixture.calls().matches("\"helm\", \"upgrade\"").count(), 1);
}

#[test]
fn every_retained_platform_image_pin_must_match_the_target() {
    for component in ["api", "worker", "dispatcher", "ui", "mailAdapter"] {
        let fixture = Fixture::new();
        fixture.values(json!({component: {"image": {"tag": "0.8.4"}}}));
        fixture.assert_refused_without_upgrade(&fixture.run("healthy"));
    }
}

#[test]
fn missing_owned_object_is_a_failed_convergence_with_recovery() {
    let fixture = Fixture::new();
    let failed = fixture.run("missing-object");
    assert!(!failed.status.success());
    let result: Value = serde_json::from_slice(&failed.stdout).unwrap();
    assert_eq!(result["status"], "failed");
    assert_eq!(result["convergence"]["manifest_matches"], false);
    assert!(result["fail_forward"]["command"]
        .as_str()
        .unwrap()
        .contains("upgrade"));
}

#[test]
fn corrupt_checkpoint_status_cannot_invent_a_known_good_idle_release() {
    let fixture = Fixture::new();
    fs::write(fixture.path("record.json"), "not valid json").unwrap();
    let status = fixture.run_status();
    let result: Value = serde_json::from_slice(&status.stdout).unwrap();
    assert_eq!(result["upgrade"]["status"], "unavailable");
    assert!(result["upgrade"]["known_good_version"].is_null());
}

#[test]
fn operator_forward_only_flag_is_a_real_upgrade_input() {
    let fixture = Fixture::new();
    let output = fixture.run_args("schema-contract", &["--forward-only"]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let values: Value =
        serde_json::from_slice(&fs::read(fixture.path("applied-values.json")).unwrap()).unwrap();
    assert_eq!(values["api"]["migrate"]["forwardOnly"], true);
}

#[test]
fn installed_release_with_missing_schema_tracking_is_not_an_empty_install() {
    let fixture = Fixture::new();
    fixture.assert_refused_without_upgrade(&fixture.run("schema-null"));
}

#[test]
fn missing_helm_hook_evidence_cannot_commit_the_target() {
    let fixture = Fixture::new();
    assert!(!fixture.run("missing-hooks").status.success());
}

#[test]
fn same_revision_id_with_different_source_content_refuses_before_mutation() {
    let fixture = Fixture::new();
    fixture.assert_refused_without_upgrade(&fixture.run("schema-content-mismatch"));
}

#[test]
fn successful_api_responses_cannot_hide_lost_retained_agent_identities() {
    let fixture = Fixture::new();
    let failed = fixture.run("lost-agents");
    assert!(!failed.status.success());
    let result: Value = serde_json::from_slice(&failed.stdout).unwrap();
    assert_eq!(result["status"], "failed");
    assert_eq!(result["canary"]["passed"], false);
}

#[test]
fn offline_dry_run_does_not_claim_to_have_inspected_the_source() {
    let fixture = Fixture::new();
    let output = fixture.run_args("healthy", &["--dry-run"]);
    assert!(output.status.success());
    assert!(fixture.calls().is_empty());
    assert!(String::from_utf8_lossy(&output.stdout).contains("not inspected"));
}

#[test]
fn failed_helm_revision_is_not_inferred_as_known_good() {
    let fixture = Fixture::new();
    assert!(!fixture.run("helm-failed").status.success());
    let record: Value =
        serde_json::from_slice(&fs::read(fixture.path("record.json")).unwrap()).unwrap();
    assert!(record["known_good_version"].is_null());
}

#[test]
fn resumed_verification_cannot_reuse_a_stale_convergence_result() {
    for phase in ["converge", "canary", "commit"] {
        let fixture = Fixture::new();
        assert!(!fixture
            .run(&format!("interrupt-after-{phase}"))
            .status
            .success());
        let resumed = fixture.run("wrong-image");
        assert!(
            !resumed.status.success(),
            "{phase}: stale proof cannot commit drifted images"
        );
        assert_eq!(fixture.calls().matches("\"helm\", \"upgrade\"").count(), 1);
    }
}

#[test]
fn successful_same_version_rerun_revalidates_without_reapplying_helm() {
    let fixture = Fixture::new();
    assert!(fixture.run("healthy").status.success());
    let rerun = fixture.run("healthy");
    assert!(rerun.status.success());
    assert_eq!(fixture.calls().matches("\"helm\", \"upgrade\"").count(), 1);
    let result: Value = serde_json::from_slice(&rerun.stdout).unwrap();
    assert_eq!(result["unchanged"], true);
}

#[test]
fn new_attempt_checkpoints_agent_identities_added_since_previous_success() {
    let fixture = Fixture::new();
    assert!(fixture.run("healthy").status.success());
    let rerun = fixture.run("additional-agents");
    assert!(
        rerun.status.success(),
        "{}",
        String::from_utf8_lossy(&rerun.stdout)
    );
}

#[test]
fn malformed_durable_phase_state_is_preserved_before_any_new_mutation() {
    for corruption in ["phase-order", "status"] {
        let fixture = Fixture::new();
        assert!(fixture.run("healthy").status.success());
        let mut record: Value =
            serde_json::from_slice(&fs::read(fixture.path("record.json")).unwrap()).unwrap();
        if corruption == "phase-order" {
            record["completed"] = json!(["plan", "apply"]);
        } else {
            record["status"] = "invented-status".into();
        }
        let bytes = serde_json::to_vec(&record).unwrap();
        fs::write(fixture.path("record.json"), &bytes).unwrap();
        fs::write(fixture.path("calls.jsonl"), "").unwrap();
        fixture.assert_refused_without_upgrade(&fixture.run("healthy"));
        assert_eq!(fs::read(fixture.path("record.json")).unwrap(), bytes);
    }
}

#[test]
fn retained_runner_image_pin_must_match_the_upgrade_target() {
    let fixture = Fixture::new();
    fixture.values(json!({"agentSandbox": {"runner": {"tag": "0.8.4"}}}));
    fixture.assert_refused_without_upgrade(&fixture.run("healthy"));
}

#[test]
fn malformed_forward_only_parent_returns_structured_refusal_without_mutation() {
    for api in [json!("invalid"), json!({"migrate": "invalid"})] {
        let fixture = Fixture::new();
        fixture.values(json!({"api": api}));
        let output = fixture.run_args("healthy", &["--forward-only"]);
        fixture.assert_refused_without_upgrade(&output);
        let result: Value = serde_json::from_slice(&output.stdout)
            .expect("invalid retained values must produce a structured error, never panic");
        assert!(result["error"].as_str().unwrap().contains("api"));
        assert!(!fixture.path("record.json").exists());
    }
}

#[test]
fn matching_incomplete_upgrade_recovers_schema_probe_without_a_running_api() {
    let fixture = Fixture::new();
    assert!(!fixture.run("helm-hook-fails").status.success());
    fs::write(fixture.path("api-unavailable"), "true").unwrap();
    let output = fixture.run("recovery-api-down");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert!(fixture.calls().contains("upgrade-database-recovery"));
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(result["status"], "succeeded");
    assert_eq!(result["known_good_version"], "0.9.0");
}

#[test]
fn unavailable_api_recovery_refuses_unverified_checkpoint_artifact_or_database() {
    for scenario in [
        "recovery-no-checkpoint",
        "recovery-changed-chart",
        "recovery-db-mismatch",
        "recovery-db-fails",
    ] {
        let fixture = Fixture::new();
        if scenario != "recovery-no-checkpoint" {
            assert!(!fixture.run("helm-hook-fails").status.success());
        }
        if scenario == "recovery-changed-chart" {
            fs::write(fixture.path("candidate-chart"), "different artifact").unwrap();
        }
        fs::write(fixture.path("api-unavailable"), "true").unwrap();
        fs::write(fixture.path("calls.jsonl"), "").unwrap();
        let previous = fs::read(fixture.path("record.json")).ok();
        let output = fixture.run(scenario);
        fixture.assert_refused_without_upgrade(&output);
        assert_eq!(
            previous,
            fs::read(fixture.path("record.json")).ok(),
            "{scenario}"
        );
        if scenario != "recovery-db-fails" {
            assert!(
                !fixture.calls().contains("upgrade-database-recovery"),
                "{scenario}"
            );
        }
    }
}

#[test]
fn running_pod_image_identity_is_observed_and_stale_or_missing_images_refuse() {
    let healthy = Fixture::new();
    let output = healthy.run("healthy");
    assert!(output.status.success());
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    let images = result["convergence"]["observed_images"]
        .as_array()
        .expect("actual running image observations are part of convergence evidence");
    assert_eq!(images.len(), 1);
    assert_eq!(images[0]["container"], "api");
    assert!(images[0]["image_id"]
        .as_str()
        .unwrap()
        .ends_with(&"d".repeat(64)));
    for scenario in [
        "wrong-running-image",
        "missing-running-image-id",
        "missing-running-pod",
        "stale-extra-pod",
    ] {
        let fixture = Fixture::new();
        let output = fixture.run(scenario);
        assert_eq!(output.status.code(), Some(1), "{scenario}");
        let result: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(result["status"], "failed", "{scenario}");
        assert_eq!(result["convergence"]["images"], false, "{scenario}");
        assert_eq!(result["known_good_version"], "0.8.5", "{scenario}");
    }
}

#[test]
fn recovery_replans_from_the_actual_new_database_revision() {
    let fixture = Fixture::new();
    assert!(!fixture.run("helm-hook-fails").status.success());
    fs::write(fixture.path("api-unavailable"), "true").unwrap();
    let output = fixture.run("recovery-db-advanced");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    let record: Value =
        serde_json::from_slice(&fs::read(fixture.path("record.json")).unwrap()).unwrap();
    assert_eq!(record["schema_decision"]["current_revision"], "0040");
    assert_eq!(record["schema_decision"]["pending"], json!([]));
}

#[test]
fn recovery_refuses_wrong_catalog_ambiguous_owner_or_running_api_probe_failure() {
    for scenario in [
        "recovery-db-unknown",
        "recovery-db-byo",
        "recovery-running-api-probe-fails",
        "recovery-duplicate-db",
        "recovery-foreign-namespace",
        "recovery-live-catalog-mismatch",
    ] {
        let fixture = Fixture::new();
        if scenario == "recovery-db-byo" {
            fixture.values(json!({"postgres": {"deploy": false}}));
        }
        assert!(!fixture.run("helm-hook-fails").status.success());
        fs::write(fixture.path("api-unavailable"), "true").unwrap();
        fs::write(fixture.path("calls.jsonl"), "").unwrap();
        let before = fs::read(fixture.path("record.json")).unwrap();
        let output = fixture.run(scenario);
        fixture.assert_refused_without_upgrade(&output);
        assert_eq!(
            before,
            fs::read(fixture.path("record.json")).unwrap(),
            "{scenario}"
        );
        if !matches!(
            scenario,
            "recovery-db-unknown" | "recovery-live-catalog-mismatch"
        ) {
            assert!(
                !fixture.calls().contains("upgrade-database-recovery"),
                "{scenario}"
            );
        }
    }
}

#[test]
fn init_container_image_identity_uses_the_same_running_image_contract() {
    let fixture = Fixture::new();
    let output = fixture.run("init-image-healthy");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        result["convergence"]["observed_images"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    let missing = Fixture::new();
    let output = missing.run("init-image-missing-id");
    assert_eq!(output.status.code(), Some(1));
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(result["convergence"]["images"], false);
}

#[test]
fn target_schema_metadata_must_bind_the_effective_api_image_before_any_write() {
    for scenario in [
        "metadata-image-missing",
        "metadata-image-empty",
        "metadata-image-invalid",
        "metadata-image-mismatch",
        "rendered-api-image-mismatch",
        "rendered-api-missing",
        "rendered-api-duplicate",
    ] {
        let fixture = Fixture::new();
        let output = fixture.run(scenario);
        fixture.assert_refused_without_upgrade(&output);
        assert!(
            !fixture.path("record.json").exists(),
            "{scenario}: wrote checkpoint before image validation"
        );
        let json: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert!(
            json["fix"].as_str().is_some_and(|fix| !fix.is_empty()),
            "{scenario}: {json}"
        );
    }
}

#[test]
fn retained_repository_override_cannot_self_authorize_schema_compatibility() {
    let fixture = Fixture::new();
    fixture.values(json!({"api":{"image":{"repository":"example.com/acme-api", "tag":"0.9.0"}}}));
    let output = fixture.run("healthy");
    fixture.assert_refused_without_upgrade(&output);
    assert!(!fixture.path("record.json").exists());
    let json: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert!(
        json["fix"].as_str().is_some_and(|fix| !fix.is_empty()),
        "{json}"
    );

    let fixture = Fixture::new();
    fixture.values(
        json!({"api":{"image":{"repository":"ghcr.io/curie-eng/curie-api", "tag":"0.9.0"}}}),
    );
    let output = fixture.run("healthy");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
}

// Kernel semantics: https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html
// Linux parent-death handling is an owned-process capability, not a claim about
// remote Helm writers or the Kubernetes hook Jobs that outlive a Helm client.
#[cfg(target_os = "linux")]
mod owner_death {
    use super::*;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use std::process::{Child, Stdio};
    use std::time::{Duration, Instant};

    struct OwnedProcesses {
        cli: Child,
        helm: Option<OwnedFd>,
    }

    impl Drop for OwnedProcesses {
        fn drop(&mut self) {
            let _ = self.cli.kill();
            let _ = self.cli.wait();
            if let Some(helm) = &self.helm {
                // A pidfd targets this exact owned child even if its numeric PID
                // has been reused by the time a failed assertion unwinds.
                unsafe {
                    libc::syscall(
                        libc::SYS_pidfd_send_signal,
                        helm.as_raw_fd(),
                        libc::SIGKILL,
                        std::ptr::null::<libc::siginfo_t>(),
                        0,
                    );
                }
            }
        }
    }

    fn stopped(fd: &OwnedFd) -> bool {
        let mut event = libc::pollfd {
            fd: fd.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        // pidfd readiness means the process exited, including an unreaped
        // zombie. It does not depend on the host PID 1 reaping promptly.
        unsafe { libc::poll(&mut event, 1, 0) == 1 }
    }

    fn killed_owner_stops_helm(signal: i32) {
        let fixture = Fixture::new();
        let stdout = fs::File::create(fixture.path("cli.stdout")).unwrap();
        let stderr = fs::File::create(fixture.path("cli.stderr")).unwrap();
        let cli = fixture
            .command("owner-death", &[])
            .env("TMPDIR", fixture.temp.path())
            .stdin(Stdio::null())
            .stdout(stdout)
            .stderr(stderr)
            .spawn()
            .unwrap();
        let mut processes = OwnedProcesses { cli, helm: None };
        let deadline = Instant::now() + Duration::from_secs(10);
        while !fixture.path("helm-owner.json").exists() {
            assert!(
                processes.cli.try_wait().unwrap().is_none(),
                "CLI ended before Helm: {}",
                fs::read_to_string(fixture.path("cli.stdout")).unwrap()
            );
            assert!(Instant::now() < deadline, "owned Helm did not start");
            std::thread::sleep(Duration::from_millis(10));
        }
        let owner: Value =
            serde_json::from_slice(&fs::read(fixture.path("helm-owner.json")).unwrap()).unwrap();
        assert_eq!(
            owner["parent"].as_u64(),
            Some(u64::from(processes.cli.id()))
        );
        let helm_pid = owner["pid"].as_i64().unwrap() as libc::pid_t;
        let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, helm_pid, 0) } as i32;
        assert!(
            fd >= 0,
            "opening owned Helm pidfd: {}",
            std::io::Error::last_os_error()
        );
        processes.helm = Some(unsafe { OwnedFd::from_raw_fd(fd) });
        assert!(!stopped(processes.helm.as_ref().unwrap()));
        // This is the actual CLI process, not a dropped future or a stand-in
        // parent. SIGTERM additionally crosses its installed cleanup handler.
        assert_eq!(
            unsafe { libc::kill(processes.cli.id() as libc::pid_t, signal) },
            0
        );
        let deadline = Instant::now() + Duration::from_secs(5);
        while processes.cli.try_wait().unwrap().is_none() {
            assert!(Instant::now() < deadline, "CLI did not stop after signal");
            std::thread::sleep(Duration::from_millis(10));
        }
        fs::write(fixture.path("release-helm"), b"owner stopped").unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while !stopped(processes.helm.as_ref().unwrap()) {
            assert!(Instant::now() < deadline, "Helm survived its owning CLI");
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(
            !fixture.path("after-owner-mutation").exists(),
            "Helm continued to mutate after its actual CLI owner died"
        );
    }

    #[test]
    fn sigkill_of_upgrade_cli_stops_owned_helm_before_later_mutation() {
        killed_owner_stops_helm(libc::SIGKILL);
    }

    #[test]
    fn sigterm_of_upgrade_cli_stops_owned_helm_before_later_mutation() {
        killed_owner_stops_helm(libc::SIGTERM);
    }
}
