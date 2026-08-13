use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const DELIBERATE_FAILURE: &str = "000-deliberate-failure";

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn source_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("cli has a repository parent")
        .to_path_buf()
}

fn copy_file(source: &Path, destination: &Path) {
    fs::create_dir_all(destination.parent().expect("file has a parent"))
        .expect("create destination parent");
    fs::copy(source, destination).expect("copy fixture file");
    fs::set_permissions(
        destination,
        fs::metadata(source)
            .expect("read source permissions")
            .permissions(),
    )
    .expect("preserve fixture permissions");
}

fn copy_directory(source: &Path, destination: &Path) {
    fs::create_dir_all(destination).expect("create destination directory");
    for entry in fs::read_dir(source).expect("read fixture directory") {
        let entry = entry.expect("read fixture entry");
        let source_path = entry.path();
        let destination_path = destination.join(entry.file_name());
        let file_type = entry.file_type().expect("read fixture entry type");
        if file_type.is_dir() {
            copy_directory(&source_path, &destination_path);
        } else if file_type.is_file() {
            copy_file(&source_path, &destination_path);
        } else {
            panic!(
                "fixture entry is not a regular file: {}",
                source_path.display()
            );
        }
    }
    fs::set_permissions(
        destination,
        fs::metadata(source)
            .expect("read source directory permissions")
            .permissions(),
    )
    .expect("preserve fixture directory permissions");
}

fn scratch_checkout() -> tempfile::TempDir {
    let source = source_root();
    let scratch = tempfile::tempdir().expect("create scratch checkout");
    copy_directory(
        &source.join("charts/curie"),
        &scratch.path().join("charts/curie"),
    );
    for relative in ["runner/Dockerfile"] {
        copy_file(&source.join(relative), &scratch.path().join(relative));
    }
    scratch
}

fn executable_script_names(directory: &Path) -> Vec<String> {
    let mut names = fs::read_dir(directory)
        .expect("read chart assertion directory")
        .map(|entry| entry.expect("read chart assertion entry"))
        .filter(|entry| {
            entry
                .file_type()
                .expect("read chart assertion file type")
                .is_file()
                && entry
                    .metadata()
                    .expect("read chart assertion metadata")
                    .permissions()
                    .mode()
                    & 0o111
                    != 0
        })
        .map(|entry| {
            entry
                .file_name()
                .into_string()
                .expect("chart assertion name is utf8")
        })
        .collect::<Vec<_>>();
    names.sort();
    names
}

fn run_chart_check(checkout: &Path) -> Output {
    let fake_bin = checkout.join("fake-bin");
    fs::create_dir_all(&fake_bin).expect("create fake bash directory");
    let fake_bash = fake_bin.join("bash");
    fs::write(
        &fake_bash,
        format!(
            "#!/bin/sh\nif [ ! -f \"$1\" ]; then\n  exit 1\nfi\nif [ \"${{1##*/}}\" = \"{DELIBERATE_FAILURE}\" ]; then\n  exec /bin/sh \"$@\"\nfi\nexit 0\n"
        ),
    )
    .expect("write fake bash");
    fs::set_permissions(&fake_bash, fs::Permissions::from_mode(0o755))
        .expect("make fake bash executable");
    let mut path = fake_bin.into_os_string();
    path.push(":");
    path.push(std::env::var_os("PATH").expect("PATH is set"));
    Command::new(bin())
        .args(["--color=never", "dev", "chart-check"])
        .current_dir(checkout)
        .env("PATH", path)
        .output()
        .expect("run curie dev chart-check")
}

fn output_text(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned() + &String::from_utf8_lossy(&output.stderr)
}

#[derive(Clone, Copy)]
enum ResultKind {
    Pass,
    Fail,
}

fn result_line_position(text: &str, script: &str, expected: ResultKind) -> Option<usize> {
    let result = match expected {
        ResultKind::Pass => format!("PASS {script}"),
        ResultKind::Fail => format!("FAIL {script} ("),
    };
    text.lines()
        .enumerate()
        .find_map(|(position, line)| line.contains(&result).then_some(position))
}

#[test]
fn chart_check_passes_unmodified_scratch_chart() {
    let checkout = scratch_checkout();
    let scripts = executable_script_names(&checkout.path().join("charts/curie/ci"));
    assert!(
        !scripts.is_empty(),
        "scratch chart has no executable assertions"
    );

    let output = run_chart_check(checkout.path());
    let text = output_text(&output);
    assert!(
        output.status.success(),
        "unmodified chart check failed\n{text}"
    );
    let mut result_positions = BTreeSet::new();
    for script in &scripts {
        let position = result_line_position(&text, script, ResultKind::Pass)
            .unwrap_or_else(|| panic!("missing explicit pass result for {script}\n{text}"));
        assert!(
            result_positions.insert(position),
            "multiple assertions shared one pass result line\n{text}"
        );
    }
    let pass_result_count = text.lines().filter(|line| line.contains("PASS ")).count();
    assert_eq!(
        pass_result_count,
        scripts.len(),
        "chart check reported an unexpected number of pass results\n{text}"
    );
}

#[test]
fn chart_check_discovers_deliberate_executable_failure() {
    let checkout = scratch_checkout();
    let assertions = checkout.path().join("charts/curie/ci");
    let committed = executable_script_names(&assertions);
    let later_script = committed
        .last()
        .expect("scratch chart has an executable assertion")
        .clone();
    assert!(later_script.as_str() > DELIBERATE_FAILURE);

    let failure = assertions.join(DELIBERATE_FAILURE);
    fs::write(
        &failure,
        "#!/bin/sh\nprintf 'deliberate failure ran\\n'\nexit 17\n",
    )
    .expect("write deliberate failure");
    fs::set_permissions(&failure, fs::Permissions::from_mode(0o755))
        .expect("make deliberate failure executable");

    let output = run_chart_check(checkout.path());
    let text = output_text(&output);
    assert!(
        !output.status.success(),
        "discovered failing assertion must fail chart check\n{text}"
    );
    let failure_position = result_line_position(&text, DELIBERATE_FAILURE, ResultKind::Fail)
        .unwrap_or_else(|| panic!("missing explicit fail result for {DELIBERATE_FAILURE}\n{text}"));
    let later_position = result_line_position(&text, &later_script, ResultKind::Pass)
        .unwrap_or_else(|| panic!("missing later pass result for {later_script}\n{text}"));
    assert!(
        later_position > failure_position,
        "chart check stopped before the later assertion\n{text}"
    );
}

fn helm_workflow_script_names(workflow: &str) -> Vec<String> {
    workflow
        .lines()
        .flat_map(|line| {
            let line = line.split_once('#').map_or(line, |(before, _)| before);
            let tokens = line.split_whitespace().collect::<Vec<_>>();
            tokens
                .windows(2)
                .filter(|pair| pair[0] == "bash")
                .filter_map(|pair| {
                    pair[1]
                        .trim_matches(&['\'', '"'][..])
                        .strip_prefix("charts/curie/ci/")
                })
                .filter(|name| !name.is_empty() && !name.contains('/'))
                .map(str::to_owned)
                .collect::<Vec<_>>()
        })
        .collect()
}

#[test]
fn chart_assertion_scripts_match_helm_ci_steps() {
    let root = source_root();
    let scripts = executable_script_names(&root.join("charts/curie/ci"))
        .into_iter()
        .collect::<BTreeSet<_>>();
    assert!(!scripts.is_empty(), "chart assertion inventory is empty");

    let workflow = fs::read_to_string(root.join(".github/workflows/helm-ci.yaml"))
        .expect("read helm workflow");
    let workflow_names = helm_workflow_script_names(&workflow);
    let mut counts = BTreeMap::new();
    for name in &workflow_names {
        *counts.entry(name.clone()).or_insert(0usize) += 1;
    }
    let duplicates = counts
        .iter()
        .filter(|(_, count)| **count > 1)
        .map(|(name, count)| format!("{name} ({count} references)"))
        .collect::<Vec<_>>();
    assert!(
        duplicates.is_empty(),
        "duplicate helm workflow assertion references: {}",
        duplicates.join(", ")
    );

    let workflow_scripts = workflow_names.into_iter().collect::<BTreeSet<_>>();
    let missing_from_workflow = scripts
        .difference(&workflow_scripts)
        .cloned()
        .collect::<Vec<_>>();
    let missing_from_directory = workflow_scripts
        .difference(&scripts)
        .cloned()
        .collect::<Vec<_>>();
    assert!(
        missing_from_workflow.is_empty() && missing_from_directory.is_empty(),
        "chart assertion parity mismatch\nmissing from workflow: {}\nmissing from directory: {}",
        missing_from_workflow.join(", "),
        missing_from_directory.join(", ")
    );
}
