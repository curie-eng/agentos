//! Plain `skill up` reloads a verified same-bundle edit without `--replace` (#1905).
//!
//! The recorded-state gate used to refuse every second `up` whenever
//! `.curie/runner.json` existed. The edit loop (up, edit SKILL.md, up again)
//! then required the user to discover `--replace`. These tests drive the real
//! binary and a real Docker identity so a revert that brings the refuse back
//! fails here rather than only in a planner unit test.
//!
//! Dummy containers are imported from an empty tar so the tests do not depend
//! on a runner image, a registry pull, or a live Curie sandbox.

use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};

use curie::bundle;
use curie::state::{self, RunnerState};

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn err_str(o: &Output) -> String {
    String::from_utf8_lossy(&o.stderr).into_owned()
}

fn out_str(o: &Output) -> String {
    String::from_utf8_lossy(&o.stdout).into_owned()
}

static SEQ: AtomicU64 = AtomicU64::new(0);

/// An identity-only Docker container whose name and id can be written into a
/// runner record. Nothing with these names exists on the host except this
/// fixture, and Drop removes both the container and the imported image.
struct DummyContainer {
    name: String,
    id: String,
    image: String,
}

impl DummyContainer {
    fn create() -> Self {
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let token = format!("{}-{}", std::process::id(), seq);
        let image = format!("curie-1905-empty:{token}");
        let name = format!("curie-1905-reload-{token}");
        let import = Command::new("docker")
            .args(["import", "-", &image])
            .stdin(std::process::Stdio::piped())
            .output()
            .expect("docker import");
        assert!(
            import.status.success(),
            "docker import empty image: {}",
            String::from_utf8_lossy(&import.stderr)
        );
        let create = Command::new("docker")
            .args(["create", "--name", &name, &image, "true"])
            .output()
            .expect("docker create");
        assert!(
            create.status.success(),
            "docker create dummy container: {}",
            String::from_utf8_lossy(&create.stderr)
        );
        let inspect = Command::new("docker")
            .args(["inspect", "--format", "{{.Id}}", &name])
            .output()
            .expect("docker inspect");
        assert!(
            inspect.status.success(),
            "docker inspect dummy container: {}",
            String::from_utf8_lossy(&inspect.stderr)
        );
        let id = String::from_utf8_lossy(&inspect.stdout).trim().to_string();
        assert!(!id.is_empty(), "dummy container id");
        Self { name, id, image }
    }

    fn still_exists(&self) -> bool {
        Command::new("docker")
            .args(["inspect", "--format", "{{.Id}}", &self.name])
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    }
}

impl Drop for DummyContainer {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "-f", &self.name])
            .status();
        let _ = Command::new("docker")
            .args(["rmi", "-f", &self.image])
            .status();
    }
}

fn scaffold_bundle() -> tempfile::TempDir {
    let dir = tempfile::tempdir().expect("tempdir");
    let init = Command::new(bin())
        .args(["init", "demo-agent", "--dir"])
        .arg(dir.path().join("bundle"))
        .stdin(std::process::Stdio::null())
        .output()
        .expect("run curie init");
    assert!(init.status.success(), "scaffold bundle: {}", err_str(&init));
    dir
}

fn bundle_path(dir: &tempfile::TempDir) -> std::path::PathBuf {
    dir.path()
        .join("bundle")
        .canonicalize()
        .expect("canonicalize bundle")
}

fn record_for(
    bundle: &std::path::Path,
    dummy: &DummyContainer,
    digest: Option<String>,
) -> RunnerState {
    RunnerState {
        container_id: dummy.id.clone(),
        container_name: dummy.name.clone(),
        image: "curie-runner".into(),
        port: 7245,
        base_url: "http://localhost:7245".into(),
        session_id: "local-1".into(),
        plugin_dir: bundle.display().to_string(),
        fake_model: true,
        ollama_container: None,
        network: None,
        model_base_url: None,
        bundle_digest: digest,
        bundle_snapshot_dir: None,
    }
}

fn skill_up(bundle: &std::path::Path, name: &str, extra: &[&str]) -> Output {
    Command::new(bin())
        .current_dir(bundle)
        .args(["skill", "up", "--fake-model", "--name", name])
        .args(extra)
        .output()
        .expect("run curie skill up")
}

/// AC4 / AC1 gate: a verified same-directory runner with a changed bundle must
/// not be refused for missing `--replace`. `--budget` is garbage so the run
/// still aborts before Docker teardown (AC2): the dummy container and the
/// record have to survive a cheap validation failure.
#[test]
fn plain_up_on_a_verified_same_bundle_edit_does_not_require_replace() {
    let dir = scaffold_bundle();
    let bundle = bundle_path(&dir);
    let dummy = DummyContainer::create();
    let recorded = record_for(&bundle, &dummy, Some("0".repeat(64)));
    state::save(&bundle, &recorded).expect("save recorded runner");

    let out = skill_up(&bundle, &dummy.name, &["--budget", "not-json"]);
    let stderr = err_str(&out);

    assert!(
        !stderr.contains("a local runner is already recorded"),
        "plain skill up must reload a verified same-bundle edit without --replace; got stderr: {stderr}"
    );
    assert!(
        !out.status.success(),
        "an invalid --budget must still fail the run, got success"
    );
    assert!(
        stderr.contains("--budget is not a valid ACI budget"),
        "expected the budget bail after the recorded-state gate, got stderr: {stderr}"
    );
    assert!(
        dummy.still_exists(),
        "a preflight failure must leave the original runner intact"
    );
    let still = state::load(&bundle)
        .expect("load state")
        .expect("a preflight failure must leave the record intact");
    assert_eq!(still.container_name, recorded.container_name);
    assert_eq!(still.container_id, recorded.container_id);
}

/// AC3: a container that merely reuses the recorded name is not the recorded
/// runner. Plain `up` must refuse and must not remove it.
#[test]
fn plain_up_does_not_remove_a_foreign_container_holding_the_recorded_name() {
    let dir = scaffold_bundle();
    let bundle = bundle_path(&dir);
    let dummy = DummyContainer::create();
    let mut recorded = record_for(&bundle, &dummy, Some("0".repeat(64)));
    recorded.container_id =
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into();
    state::save(&bundle, &recorded).expect("save hijacked record");

    let out = skill_up(&bundle, &dummy.name, &[]);
    let stderr = err_str(&out);

    assert!(
        !out.status.success(),
        "an unverified identity must still refuse, got success"
    );
    assert!(
        stderr.contains("a local runner is already recorded"),
        "the foreign-runner refuse must remain, got stderr: {stderr}"
    );
    assert!(
        dummy.still_exists(),
        "plain skill up must not remove a container whose id does not match the record"
    );
    let still = state::load(&bundle)
        .expect("load state")
        .expect("a refused up must not clear a foreign record");
    assert_eq!(still.container_id, recorded.container_id);
}

/// AC3: an explicit `--name` that is not the recorded runner never implies
/// replacement of the recorded one.
#[test]
fn plain_up_refuses_a_mismatched_explicit_name() {
    let dir = scaffold_bundle();
    let bundle = bundle_path(&dir);
    let dummy = DummyContainer::create();
    let recorded = record_for(&bundle, &dummy, Some("0".repeat(64)));
    state::save(&bundle, &recorded).expect("save recorded runner");
    let other = format!("curie-1905-other-{}", std::process::id());

    let out = skill_up(&bundle, &other, &[]);
    let stderr = err_str(&out);

    assert!(
        !out.status.success(),
        "a mismatched --name must still refuse, got success"
    );
    assert!(
        stderr.contains("a local runner is already recorded"),
        "the mismatched-name refuse must remain, got stderr: {stderr}"
    );
    assert!(
        dummy.still_exists(),
        "a mismatched --name must not tear down the recorded runner"
    );
}

/// Unchanged source on a verified same-bundle runner should report that it is
/// already running rather than tearing the runner down to snapshot the same
/// bytes again.
#[test]
fn plain_up_reports_already_running_when_the_bundle_is_unchanged() {
    let dir = scaffold_bundle();
    let bundle = bundle_path(&dir);
    let dummy = DummyContainer::create();
    let snapshot = bundle::snapshot(&bundle).expect("snapshot current bundle");
    let recorded = record_for(&bundle, &dummy, Some(snapshot.digest));
    state::save(&bundle, &recorded).expect("save recorded runner");

    let out = skill_up(&bundle, &dummy.name, &[]);
    let combined = format!("{}{}", out_str(&out), err_str(&out));

    assert!(
        out.status.success(),
        "an unchanged verified bundle must not fail plain skill up; stderr: {}",
        err_str(&out)
    );
    assert!(
        combined.contains("already running"),
        "an unchanged bundle must report that the recorded runner is already running; got: {combined}"
    );
    assert!(
        dummy.still_exists(),
        "already-running must not tear down the recorded runner"
    );
    let still = state::load(&bundle)
        .expect("load state")
        .expect("already-running must leave the record intact");
    assert_eq!(still.container_id, recorded.container_id);
}
