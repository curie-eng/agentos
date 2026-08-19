//! Unit-level coverage for the semantic exit-code + error-classification
//! contract (ADR-0021 decision 1, AC 2 and AC 4). These reference the
//! yet-to-exist `curie::exit` module, so the file will not compile until the
//! implementer adds it; that red state is the contract handoff.

use curie::exit::{self, CliError, ExitClass};
use std::process::{Command, Output};

const SEAL_VALUE: &str = "placeholder-seal-value";

fn run_seal(connector: &str, json: bool) -> Output {
    let keypair = curie::sealing::generate_keypair();
    let connector_arg = format!("--connector={connector}");
    let mut command = Command::new(env!("CARGO_BIN_EXE_curie"));
    if json {
        command.arg("--json");
    }
    command
        .args([
            "seal",
            &connector_arg,
            "GRAFANA_TOKEN",
            "--public-key",
            &keypair.public_key,
            "--from-env",
            "CURIE_TEST_SEAL_VALUE",
        ])
        .env("CURIE_TEST_SEAL_VALUE", SEAL_VALUE)
        .output()
        .expect("run curie seal")
}

#[test]
fn exit_class_codes_are_stable() {
    assert_eq!(ExitClass::Success.code(), 0);
    assert_eq!(ExitClass::Failure.code(), 1);
    assert_eq!(ExitClass::Usage.code(), 2);
    assert_eq!(ExitClass::Transient.code(), 3);
}

#[test]
fn classify_usage_error_is_usage_class() {
    let err = exit::usage("bad");
    let (class, _fix) = exit::classify(&err);
    assert_eq!(class, ExitClass::Usage);
}

#[test]
fn classify_transient_error_is_transient_with_fix() {
    let err = exit::transient("net");
    let (class, fix) = exit::classify(&err);
    assert_eq!(class, ExitClass::Transient);
    assert!(fix.is_some(), "transient errors carry a retry hint");
}

#[test]
fn classify_plain_anyhow_is_failure_with_no_fix() {
    let err = anyhow::anyhow!("boom");
    let (class, fix) = exit::classify(&err);
    assert_eq!(class, ExitClass::Failure);
    assert_eq!(fix, None);
}

#[test]
fn classify_finds_clierror_through_context_wrapping() {
    // A tagged CliError buried under an anyhow context layer must still be
    // discovered by walking the error chain: class + fix survive wrapping.
    let base: anyhow::Error = CliError::usage("nope").with_fix("do X").into();
    let wrapped = base.context("outer");
    let (class, fix) = exit::classify(&wrapped);
    assert_eq!(class, ExitClass::Usage);
    assert_eq!(fix.as_deref(), Some("do X"));
}

#[test]
fn error_json_carries_message_and_fix() {
    let err: anyhow::Error = CliError::usage("nope").with_fix("do X").into();
    let value = exit::error_json(&err);
    assert_eq!(value["error"], "nope");
    assert_eq!(value["fix"], "do X");
}

#[test]
fn error_json_fix_is_null_for_plain_error() {
    let err = anyhow::anyhow!("kaboom");
    let value = exit::error_json(&err);
    assert_eq!(value["error"], "kaboom");
    assert!(value["fix"].is_null());
}

#[test]
fn seal_rejects_invalid_connector_names_as_usage_with_a_fix() {
    let invalid = [
        "Grafana".to_string(),
        "graf ana".to_string(),
        "graf_ana".to_string(),
        "-grafana".to_string(),
        "grafana-".to_string(),
        "a".repeat(41),
    ];

    for connector in invalid {
        let output = run_seal(&connector, true);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert_eq!(
            output.status.code(),
            Some(ExitClass::Usage.code()),
            "{connector:?} must be rejected as Usage\nstdout: {stdout}\nstderr: {stderr}"
        );
        let payload: serde_json::Value = serde_json::from_slice(&output.stdout)
            .unwrap_or_else(|err| panic!("{connector:?} must emit JSON: {err}; {stdout}"));
        assert!(
            payload["fix"].as_str().is_some_and(|fix| !fix.is_empty()),
            "{connector:?} must include actionable paste guidance: {payload}"
        );
        assert!(
            !stdout.contains(SEAL_VALUE) && !stderr.contains(SEAL_VALUE),
            "a rejected connector must not expose the value"
        );
    }
}

#[test]
fn seal_accepts_the_connector_name_length_boundary_and_interior_dash() {
    for connector in ["a".repeat(40), "grafana-cloud".to_string()] {
        let output = run_seal(&connector, true);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert_eq!(
            output.status.code(),
            Some(ExitClass::Success.code()),
            "{connector:?} must be accepted\nstdout: {stdout}\nstderr: {stderr}"
        );
        let payload: serde_json::Value = serde_json::from_slice(&output.stdout)
            .unwrap_or_else(|err| panic!("{connector:?} must emit JSON: {err}; {stdout}"));
        assert_eq!(payload["connector"].as_str(), Some(connector.as_str()));
        assert!(
            !stdout.contains(SEAL_VALUE) && !stderr.contains(SEAL_VALUE),
            "a sealed value must not expose its plaintext"
        );
    }
}

#[test]
fn seal_human_output_warns_before_pasting_rejected_sealed_secrets() {
    let human = run_seal("grafana", false);
    let human_stdout = String::from_utf8_lossy(&human.stdout);
    let human_stderr = String::from_utf8_lossy(&human.stderr);
    let human_output = format!("{human_stdout}{human_stderr}");
    assert_eq!(
        human.status.code(),
        Some(ExitClass::Success.code()),
        "human seal must succeed\nstdout: {human_stdout}\nstderr: {human_stderr}"
    );
    assert!(
        human_stdout.contains("sealed_secrets"),
        "human seal must retain the paste block:\n{human_stdout}"
    );
    assert!(
        human_output.contains("connector validation")
            && human_output.contains("sealed_secrets")
            && human_output.contains("#1434")
            && human_output.contains("until"),
        "human seal must warn that connector validation rejects sealed_secrets until #1434 lands:\n{human_output}"
    );
    let human_output_lower = human_output.to_ascii_lowercase();
    assert!(
        !human_output_lower.contains("merge") && !human_output_lower.contains("paste"),
        "human seal must locate the block without telling the user to merge or paste it:\n{human_output}"
    );

    let json = run_seal("grafana", true);
    let json_stdout = String::from_utf8_lossy(&json.stdout);
    let json_stderr = String::from_utf8_lossy(&json.stderr);
    assert_eq!(
        json.status.code(),
        Some(ExitClass::Success.code()),
        "JSON seal must succeed\nstdout: {json_stdout}\nstderr: {json_stderr}"
    );
    let payload: serde_json::Value = serde_json::from_slice(&json.stdout)
        .unwrap_or_else(|err| panic!("JSON seal must emit only JSON: {err}; {json_stdout}"));
    let fields = payload.as_object().expect("JSON seal must emit an object");
    assert_eq!(
        fields
            .keys()
            .map(String::as_str)
            .collect::<std::collections::BTreeSet<_>>(),
        ["connector", "env_name", "sealed", "public_key", "yaml"]
            .into_iter()
            .collect(),
        "JSON seal must retain its exact machine readable shape"
    );
    assert!(
        !json_stdout.contains("#1434") && !json_stderr.contains("#1434"),
        "the human warning must not appear in JSON mode:\nstdout: {json_stdout}\nstderr: {json_stderr}"
    );
}
