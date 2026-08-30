//! Integration (#1358 D6b): `curie doctor` resolves its target from the
//! `curie.yaml` in the working directory, announces the inference, and never
//! fails because of that file.
//!
//! Both properties here are structurally invisible to the pure
//! `resolve_target` table in `cli/src/doctor.rs`: hand-passing `None` to the
//! resolver bypasses the dispatch arm's fail-soft read entirely, and
//! `DoctorOutput::to_json()` cannot observe which stream a line was written to.
//! So these run the real binary.
//!
//! Offline by construction: `PATH` is emptied, so docker, kubectl and helm are
//! all absent and every cluster check reports the laptop rung.

use std::fs;
use std::path::Path;
use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

/// Run `curie <args>` with the working directory in `dir` and no tools on
/// `PATH`, so the run needs no cluster and no network.
fn run_doctor(dir: &Path, empty_path: &Path, args: &[&str]) -> (Option<i32>, String, String) {
    let output = Command::new(bin())
        .current_dir(dir)
        .args(args)
        .env("PATH", empty_path)
        .env("LC_ALL", "C")
        .env_remove("CURIE_API_URL")
        .env_remove("CURIE_API_KEY")
        .output()
        .expect("run curie doctor");
    (
        output.status.code(),
        String::from_utf8_lossy(&output.stdout).into_owned(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

fn empty_path_dir(root: &Path) -> std::path::PathBuf {
    let dir = root.join("no-tools");
    fs::create_dir_all(&dir).expect("create empty PATH directory");
    dir
}

/// doctor is read-only and its whole job is to report. A `curie.yaml` it cannot
/// parse must narrow what it knows, never stop it answering -- which is exactly
/// what a `?` or an `expect` on the load would do. The pure resolver cannot
/// catch that: it is handed `None` either way.
#[test]
fn a_malformed_curie_yaml_does_not_fail_doctor() {
    let temp = tempfile::tempdir().expect("create temporary directory");
    let tools = empty_path_dir(temp.path());

    let cases = [
        // Not YAML this schema can make sense of at all.
        ("garbage", ": : not: [valid\n  yaml at all\n"),
        // Parses, but declares a schema version this binary refuses.
        (
            "unsupported version",
            "version: 99\ninstall:\n  namespace: acme\n  release: acme\n",
        ),
    ];

    for (what, body) in cases {
        fs::write(temp.path().join("curie.yaml"), body).expect("write curie.yaml");
        let (code, stdout, stderr) = run_doctor(temp.path(), &tools, &["--color=never", "doctor"]);

        assert_eq!(
            code,
            Some(0),
            "a {what} curie.yaml must not fail a read-only report\n\
             stdout: {stdout}\nstderr: {stderr}"
        );
        for line in ["Model credential", "Bundle in this directory"] {
            assert!(
                stdout.contains(line),
                "a {what} curie.yaml must still produce a full report; \
                 missing {line:?}\nstdout: {stdout}\nstderr: {stderr}"
            );
        }
        assert!(
            stderr.contains("curie.yaml"),
            "the operator must be told their file was not read, or doctor looks \
             like it ignored it\nstderr: {stderr}"
        );
    }
}

/// `--json` owns stdout: a machine consumer parses it whole. An announcement
/// printed with `println!` or `payload_plain` would corrupt that payload, and no
/// `to_json()` assertion could ever see it -- only the real streams can.
#[test]
fn an_inferred_target_keeps_json_stdout_clean() {
    let temp = tempfile::tempdir().expect("create temporary directory");
    let tools = empty_path_dir(temp.path());
    fs::write(
        temp.path().join("curie.yaml"),
        "version: 1\ninstall:\n  namespace: acme\n  release: acme\n",
    )
    .expect("write curie.yaml");

    let (code, stdout, stderr) =
        run_doctor(temp.path(), &tools, &["--color=never", "--json", "doctor"]);
    assert_eq!(code, Some(0), "stdout: {stdout}\nstderr: {stderr}");

    let values: Vec<serde_json::Value> = serde_json::Deserializer::from_str(&stdout)
        .into_iter::<serde_json::Value>()
        .collect::<Result<_, _>>()
        .unwrap_or_else(|e| panic!("stdout must be JSON, got {stdout:?}: {e}"));
    assert_eq!(
        values.len(),
        1,
        "stdout must carry exactly one JSON value: {stdout:?}"
    );
    assert!(
        values[0].is_object(),
        "the one value must be the report object: {stdout:?}"
    );
    for noise in ["curie.yaml", "inferred"] {
        assert!(
            !stdout.contains(noise),
            "{noise:?} reached the machine payload: {stdout:?}"
        );
    }

    assert!(
        stderr.contains("curie.yaml"),
        "the inference must be announced, and named as coming from the file \
         (INFER, DON'T ASK): {stderr}"
    );
    assert!(
        stderr.contains("acme"),
        "the announcement must name the target it resolved: {stderr}"
    );
}
