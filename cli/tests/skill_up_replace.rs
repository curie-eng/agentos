//! `skill up --replace` must never strand what the record described (#747).
//!
//! The flag exists to recover a bundle whose recorded runner is in the way. Two
//! ways that recovery can make things worse, both pinned here:
//!
//! 1. Clearing `.curie/runner.json` BEFORE the run can still abort leaves the
//!    old runner alive and now untracked, which is the un-stoppable orphan the
//!    ticket exists to remove.
//! 2. Tearing down only the runner container forgets the `--local-model`
//!    sidecar and network the same record owns, leaving both running with
//!    nothing recording them.
//!
//! Both observe the CLI's own behavior through the real binary rather than any
//! internal shape. Container and network names are pid-unique and fabricated, so
//! nothing on the host can match them.

use std::process::{Command, Output};

use curie::state::{self, RunnerState};

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn err_str(o: &Output) -> String {
    String::from_utf8_lossy(&o.stderr).into_owned()
}

/// A scaffolded bundle carrying a recorded runner. The bundle comes from the
/// real `init` verb and the record from the real state type, so neither fixture
/// can drift from what `skill up` loads. The recorded names are fabricated and
/// pid-scoped: nothing with these names exists, so the teardown they trigger
/// cannot touch a real container or network.
fn bundle_with_recorded_runner(with_local_model: bool) -> (tempfile::TempDir, RunnerState) {
    let dir = tempfile::tempdir().expect("tempdir");
    let init = Command::new(bin())
        .args(["init", "demo-agent", "--dir"])
        .arg(dir.path().join("bundle"))
        .stdin(std::process::Stdio::null())
        .output()
        .expect("run curie init");
    assert!(init.status.success(), "scaffold bundle: {}", err_str(&init));

    let name = format!("curie-747-replace-{}", std::process::id());
    let recorded = RunnerState {
        container_id: "deadbeef0000".into(),
        container_name: name.clone(),
        image: "curie-runner".into(),
        port: 7245,
        base_url: "http://localhost:7245".into(),
        session_id: "local-1".into(),
        plugin_dir: dir.path().join("bundle").display().to_string(),
        fake_model: true,
        ollama_container: with_local_model.then(|| format!("{name}-ollama")),
        network: with_local_model.then(|| format!("{name}-net")),
        model_base_url: None,
        bundle_digest: None,
        bundle_snapshot_dir: None,
    };
    state::save(&dir.path().join("bundle"), &recorded).expect("save recorded runner");
    (dir, recorded)
}

fn skill_up(dir: &tempfile::TempDir, recorded: &RunnerState, extra: &[&str]) -> Output {
    Command::new(bin())
        .current_dir(dir.path().join("bundle"))
        .args([
            "skill",
            "up",
            "--replace",
            "--name",
            &recorded.container_name,
        ])
        .args(extra)
        .output()
        .expect("run curie skill up")
}

/// The record survives a run that aborts before the replacement happens.
///
/// `--budget` is deliberately garbage: it fails a validation that costs nothing
/// and never reaches Docker. If the record were cleared up front (the defect),
/// the old runner would still be live with nothing left describing it.
#[test]
fn a_failed_validation_leaves_the_recorded_runner_on_record() {
    let (dir, recorded) = bundle_with_recorded_runner(false);
    let out = skill_up(&dir, &recorded, &["--budget", "not-json"]);

    assert!(
        !out.status.success(),
        "an invalid --budget must fail the run, got success"
    );
    assert!(
        err_str(&out).contains("--budget is not a valid ACI budget"),
        "expected the budget bail, got stderr: {}",
        err_str(&out)
    );
    let still_recorded = state::load(&dir.path().join("bundle"))
        .expect("load state")
        .expect("--replace must not clear the record before the run can still abort");
    assert_eq!(still_recorded.container_name, recorded.container_name);
}

/// Replacing a recorded `--local-model` run tears down everything that record
/// owns, not just the runner container.
///
/// The image is a tag that cannot resolve, so the boot dies after the teardown
/// and before any container exists. The observable is that the sidecar and the
/// network were both addressed by name; tearing down only the runner (the
/// defect) leaves them running with nothing recording them.
#[test]
fn replacing_a_recorded_local_model_run_tears_down_its_sidecar_and_network() {
    let (dir, recorded) = bundle_with_recorded_runner(true);
    let ollama = recorded.ollama_container.clone().expect("sidecar recorded");
    let network = recorded.network.clone().expect("network recorded");
    let out = skill_up(
        &dir,
        &recorded,
        &["--image", "curie-runner:747-image-that-does-not-exist"],
    );
    let stderr = err_str(&out);

    assert!(
        stderr.contains(&recorded.container_name),
        "the recorded runner must be torn down; got stderr: {stderr}"
    );
    assert!(
        stderr.contains(&ollama),
        "the recorded ollama sidecar must be torn down too; got stderr: {stderr}"
    );
    assert!(
        stderr.contains(&network),
        "the recorded network must be torn down too; got stderr: {stderr}"
    );
    assert!(
        state::load(&dir.path().join("bundle"))
            .expect("load state")
            .is_none(),
        "the record is cleared once what it described is gone"
    );
}
