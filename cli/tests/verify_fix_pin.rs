use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn output_text(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned() + &String::from_utf8_lossy(&output.stderr)
}

fn write_file(root: &Path, rel: &str, body: &str) {
    let path = root.join(rel);
    fs::create_dir_all(path.parent().expect("file has a parent")).expect("create parent");
    fs::write(path, body).expect("write fixture file");
}

fn write_exec(root: &Path, name: &str, body: &str) {
    let path = root.join(name);
    fs::write(&path, body).expect("write executable");
    let mut permissions = fs::metadata(&path)
        .expect("executable metadata")
        .permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(path, permissions).expect("make executable");
}

fn git(root: &Path, args: &[&str]) -> Output {
    let output = Command::new("git")
        .arg("-c")
        .arg("commit.gpgsign=false")
        .arg("-c")
        .arg("core.hooksPath=/dev/null")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .expect("run git");
    assert!(
        output.status.success(),
        "git command failed\n{}",
        output_text(&output)
    );
    output
}

fn stdout_has_result(output: &Output, result: &str) -> bool {
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .any(|line| line.trim() == result)
}

struct Fixture {
    temp: tempfile::TempDir,
    root: PathBuf,
    tools: PathBuf,
}

impl Fixture {
    fn new(test_path: &str, test_body: &str) -> Self {
        let temp = tempfile::tempdir().expect("create temporary directory");
        let root = temp.path().join("repo");
        let tools = temp.path().join("tools");
        fs::create_dir_all(&root).expect("create repository");
        fs::create_dir_all(&tools).expect("create tools directory");

        write_file(&root, "runner/Dockerfile", "value=old\n");
        write_file(&root, test_path, test_body);
        let source_script =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("scripts/verify-fix-pin.sh");
        let script = fs::read_to_string(source_script).expect("read checked in verify script");
        write_file(&root, "cli/scripts/verify-fix-pin.sh", &script);

        git(&root, &["init", "-q"]);
        git(&root, &["config", "user.name", "Curie Test"]);
        git(&root, &["config", "user.email", "curie@example.com"]);
        git(&root, &["add", "."]);
        git(&root, &["commit", "-q", "-m", "Initial fixture"]);

        Self { temp, root, tools }
    }

    fn write(&self, rel: &str, body: &str) {
        write_file(&self.root, rel, body);
    }

    fn commit(&self, changes: &[(&str, &str)], message: &str) -> String {
        for (path, body) in changes {
            self.write(path, body);
        }
        git(&self.root, &["add", "."]);
        git(&self.root, &["commit", "-q", "-m", message]);
        self.head()
    }

    fn head(&self) -> String {
        String::from_utf8(git(&self.root, &["rev-parse", "HEAD"]).stdout)
            .expect("head is UTF 8")
            .trim()
            .to_owned()
    }

    fn patch(&self, base: &str, head: &str) -> Vec<u8> {
        git(&self.root, &["diff", base, head]).stdout
    }

    fn command(&self, change: &str, selector: &str) -> Command {
        let mut paths = vec![self.tools.clone()];
        if let Some(path) = std::env::var_os("PATH") {
            paths.extend(std::env::split_paths(&path));
        }
        let path = std::env::join_paths(paths).expect("join PATH");

        let mut command = Command::new(bin());
        command
            .current_dir(&self.root)
            .args(["dev", "verify-fix-pin", change, selector])
            .env("PATH", path);
        command
    }

    fn assert_clean_and_single_worktree(&self) {
        let status = git(&self.root, &["status", "--porcelain"]);
        assert!(
            status.stdout.is_empty(),
            "the source repository changed\n{}",
            String::from_utf8_lossy(&status.stdout)
        );
        let worktrees = git(&self.root, &["worktree", "list", "--porcelain"]);
        let count = String::from_utf8_lossy(&worktrees.stdout)
            .lines()
            .filter(|line| line.starts_with("worktree "))
            .count();
        assert_eq!(count, 1, "the scratch worktree was not removed");
    }

    fn external_path(&self, name: &str) -> PathBuf {
        self.temp.path().join(name)
    }
}

const CHART_OLD: &str = r#"#!/bin/sh
set -eu
grep -qx 'value=old' runner/Dockerfile
"#;

const CHART_FIXED: &str = r#"#!/bin/sh
set -eu
grep -qx 'value=fixed' runner/Dockerfile
"#;

#[test]
fn commit_is_pinned_only_when_reversing_product_code_breaks_the_changed_chart_assertion() {
    let fixture = Fixture::new("charts/curie/ci/assert-pin.sh", CHART_OLD);
    let change = fixture.commit(
        &[
            ("runner/Dockerfile", "value=fixed\n"),
            ("charts/curie/ci/assert-pin.sh", CHART_FIXED),
        ],
        "Fix chart behavior",
    );

    let output = fixture
        .command(&change, "charts/curie/ci/assert-pin.sh")
        .output()
        .expect("run verify fix pin");
    assert!(
        output.status.success(),
        "a pinned change must succeed\n{}",
        output_text(&output)
    );
    assert!(
        stdout_has_result(&output, "PINNED"),
        "stdout must report PINNED\n{}",
        output_text(&output)
    );
    assert_eq!(
        fs::read_to_string(fixture.root.join("runner/Dockerfile")).expect("read source behavior"),
        "value=fixed\n"
    );
    fixture.assert_clean_and_single_worktree();
}

#[test]
fn commit_is_unpinned_when_the_changed_assertion_stays_green_after_product_reversal() {
    let fixture = Fixture::new(
        "charts/curie/ci/assert-pin.sh",
        "#!/bin/sh\nset -eu\ntest -s runner/Dockerfile\n",
    );
    let change = fixture.commit(
        &[
            ("runner/Dockerfile", "value=fixed\n"),
            (
                "charts/curie/ci/assert-pin.sh",
                "#!/bin/sh\nset -eu\ntest -f runner/Dockerfile\n",
            ),
        ],
        "Add weak chart assertion",
    );

    let output = fixture
        .command(&change, "charts/curie/ci/assert-pin.sh")
        .output()
        .expect("run verify fix pin");
    assert!(
        !output.status.success(),
        "an unpinned change must fail\n{}",
        output_text(&output)
    );
    assert!(
        stdout_has_result(&output, "UNPINNED"),
        "stdout must report UNPINNED\n{}",
        output_text(&output)
    );
    fixture.assert_clean_and_single_worktree();
}

#[test]
fn change_without_a_test_hunk_refuses_before_running_the_selector() {
    let fixture = Fixture::new(
        "charts/curie/ci/assert-pin.sh",
        "#!/bin/sh\nset -eu\nprintf 'run\\n' >> \"$VERIFY_RUN_LOG\"\n",
    );
    let change = fixture.commit(
        &[("runner/Dockerfile", "value=fixed\n")],
        "Change behavior only",
    );
    let run_log = fixture.external_path("run.log");

    let output = fixture
        .command(&change, "charts/curie/ci/assert-pin.sh")
        .env("VERIFY_RUN_LOG", &run_log)
        .output()
        .expect("run verify fix pin");
    let shown = output_text(&output);
    let lower = shown.to_lowercase();
    assert!(
        !output.status.success(),
        "missing test hunks must fail\n{shown}"
    );
    assert!(
        lower.contains("no test") && lower.contains("file"),
        "the refusal must explain that the change has no test files\n{shown}"
    );
    assert!(!run_log.exists(), "the selector must not run");
    assert!(!stdout_has_result(&output, "PINNED"));
    assert!(!stdout_has_result(&output, "UNPINNED"));
    fixture.assert_clean_and_single_worktree();
}

#[test]
fn overlapping_later_edit_refuses_before_the_post_reversal_selector_run() {
    let fixture = Fixture::new(
        "charts/curie/ci/assert-pin.sh",
        "#!/bin/sh\nset -eu\nprintf 'run\\n' >> \"$VERIFY_RUN_LOG\"\ntest -s runner/Dockerfile\n",
    );
    let change = fixture.commit(
        &[
            ("runner/Dockerfile", "value=fixed\n"),
            (
                "charts/curie/ci/assert-pin.sh",
                "#!/bin/sh\nset -eu\nprintf 'run\\n' >> \"$VERIFY_RUN_LOG\"\ngrep -q '^value=' runner/Dockerfile\n",
            ),
        ],
        "Fix behavior with assertion",
    );
    fixture.commit(
        &[("runner/Dockerfile", "value=later\n")],
        "Change the same behavior later",
    );
    let run_log = fixture.external_path("run.log");

    let output = fixture
        .command(&change, "charts/curie/ci/assert-pin.sh")
        .env("VERIFY_RUN_LOG", &run_log)
        .output()
        .expect("run verify fix pin");
    let shown = output_text(&output);
    assert!(
        !output.status.success(),
        "a reverse conflict must fail\n{shown}"
    );
    assert!(
        shown.contains("runner/Dockerfile") && shown.to_lowercase().contains("reverse"),
        "the refusal must name the path that cannot be reversed\n{shown}"
    );
    assert_eq!(
        fs::read_to_string(&run_log).expect("read selector log"),
        "run\n",
        "only the baseline selector may run"
    );
    assert!(!stdout_has_result(&output, "PINNED"));
    assert!(!stdout_has_result(&output, "UNPINNED"));
    fixture.assert_clean_and_single_worktree();
}

#[test]
fn red_baseline_refuses_before_reversing_product_code() {
    let fixture = Fixture::new("charts/curie/ci/assert-pin.sh", CHART_OLD);
    let change = fixture.commit(
        &[
            ("runner/Dockerfile", "value=fixed\n"),
            (
                "charts/curie/ci/assert-pin.sh",
                "#!/bin/sh\nset -eu\nprintf 'run\\n' >> \"$VERIFY_RUN_LOG\"\ngrep -qx 'value=missing' runner/Dockerfile\n",
            ),
        ],
        "Add a red assertion",
    );
    let run_log = fixture.external_path("run.log");

    let output = fixture
        .command(&change, "charts/curie/ci/assert-pin.sh")
        .env("VERIFY_RUN_LOG", &run_log)
        .output()
        .expect("run verify fix pin");
    let shown = output_text(&output);
    assert!(
        !output.status.success(),
        "a red baseline must fail\n{shown}"
    );
    assert!(
        shown.to_lowercase().contains("baseline"),
        "the refusal must identify the red baseline\n{shown}"
    );
    assert_eq!(
        fs::read_to_string(run_log).expect("read selector log"),
        "run\n",
        "the red selector must run only once"
    );
    assert!(!stdout_has_result(&output, "PINNED"));
    assert!(!stdout_has_result(&output, "UNPINNED"));
    fixture.assert_clean_and_single_worktree();
}

#[test]
fn selector_not_changed_by_the_reference_refuses_before_running_it() {
    let fixture = Fixture::new(
        "charts/curie/ci/selected.sh",
        "#!/bin/sh\nset -eu\nprintf 'run\\n' >> \"$VERIFY_RUN_LOG\"\ngrep -qx 'value=fixed' runner/Dockerfile\n",
    );
    let change = fixture.commit(
        &[
            ("runner/Dockerfile", "value=fixed\n"),
            ("charts/curie/ci/other.sh", "#!/bin/sh\nset -eu\nexit 0\n"),
        ],
        "Change behavior with another assertion",
    );
    let run_log = fixture.external_path("run.log");

    let output = fixture
        .command(&change, "charts/curie/ci/selected.sh")
        .env("VERIFY_RUN_LOG", &run_log)
        .output()
        .expect("run verify fix pin");
    let shown = output_text(&output);
    assert!(
        !output.status.success(),
        "an unchanged selector must fail\n{shown}"
    );
    assert!(
        shown.to_lowercase().contains("selector") && shown.to_lowercase().contains("changed"),
        "the refusal must identify the unchanged selector\n{shown}"
    );
    assert!(!run_log.exists(), "the unchanged selector must not run");
    assert!(!stdout_has_result(&output, "PINNED"));
    assert!(!stdout_has_result(&output, "UNPINNED"));
    fixture.assert_clean_and_single_worktree();
}

#[test]
fn change_with_only_test_files_refuses_before_running_the_selector() {
    let fixture = Fixture::new("charts/curie/ci/assert-pin.sh", CHART_OLD);
    let change = fixture.commit(
        &[(
            "charts/curie/ci/assert-pin.sh",
            "#!/bin/sh\nset -eu\nprintf 'run\\n' >> \"$VERIFY_RUN_LOG\"\ngrep -qx 'value=old' runner/Dockerfile\n",
        )],
        "Change assertion only",
    );
    let run_log = fixture.external_path("run.log");

    let output = fixture
        .command(&change, "charts/curie/ci/assert-pin.sh")
        .env("VERIFY_RUN_LOG", &run_log)
        .output()
        .expect("run verify fix pin");
    let shown = output_text(&output);
    assert!(
        !output.status.success(),
        "a change without product files must fail\n{shown}"
    );
    assert!(
        shown.to_lowercase().contains("no product files"),
        "the refusal must identify the missing product files\n{shown}"
    );
    assert!(!run_log.exists(), "the selector must not run");
    assert!(!stdout_has_result(&output, "PINNED"));
    assert!(!stdout_has_result(&output, "UNPINNED"));
    fixture.assert_clean_and_single_worktree();
}

fn assert_tool_selector_route(test_path: &str, selector: &str, tool: &str, argv: &[&str]) {
    let fixture = Fixture::new(test_path, "old assertion\n");
    let change = fixture.commit(
        &[
            ("runner/Dockerfile", "value=fixed\n"),
            (test_path, "new assertion\n"),
        ],
        "Fix behavior with routed assertion",
    );
    let route_log = fixture.external_path("route.log");
    write_exec(
        &fixture.tools,
        tool,
        r#"#!/bin/sh
{
    printf 'call\n'
    printf 'cwd=%s\n' "$PWD"
    printf 'argc=%s\n' "$#"
    for arg in "$@"; do
        printf 'arg=%s\n' "$arg"
    done
} >> "$VERIFY_ROUTE_LOG"
grep -qx 'value=fixed' runner/Dockerfile
"#,
    );

    let output = fixture
        .command(&change, selector)
        .env("VERIFY_ROUTE_LOG", &route_log)
        .output()
        .expect("run verify fix pin");
    assert!(
        output.status.success(),
        "the routed selector must prove the pin\n{}",
        output_text(&output)
    );
    assert!(stdout_has_result(&output, "PINNED"));

    let log = fs::read_to_string(route_log).expect("read route log");
    let calls: Vec<Vec<&str>> = log
        .split("call\n")
        .filter(|call| !call.is_empty())
        .map(|call| call.lines().collect())
        .collect();
    assert_eq!(
        calls.len(),
        2,
        "the selector must run before and after reversal"
    );
    let mut scratch_cwd: Option<&str> = None;
    for call in calls {
        let cwd = call[0].strip_prefix("cwd=").expect("cwd record");
        assert_ne!(Path::new(cwd), fixture.root.as_path());
        if let Some(expected) = scratch_cwd {
            assert_eq!(cwd, expected, "both runs must use one scratch repository");
        } else {
            scratch_cwd = Some(cwd);
        }
        assert_eq!(call[1], format!("argc={}", argv.len()));
        let actual: Vec<&str> = call[2..]
            .iter()
            .map(|line| line.strip_prefix("arg=").expect("argument record"))
            .collect();
        assert_eq!(actual, argv);
    }
    fixture.assert_clean_and_single_worktree();
}

#[test]
fn python_app_selector_uses_uv_with_the_exact_root_command() {
    let selector = "apps/api/tests/test_pin.py::test_pin";
    assert_tool_selector_route(
        "apps/api/tests/test_pin.py",
        selector,
        "uv",
        &["run", "--python", "3.13", "pytest", selector],
    );
}

#[test]
fn python_package_selector_uses_uv_with_the_exact_root_command() {
    let selector = "packages/example/tests/test_pin.py::test_pin";
    assert_tool_selector_route(
        "packages/example/tests/test_pin.py",
        selector,
        "uv",
        &["run", "--python", "3.13", "pytest", selector],
    );
}

#[test]
fn rust_selector_uses_cargo_with_the_exact_manifest_and_test_name() {
    assert_tool_selector_route(
        "cli/tests/verify_pin.rs",
        "cli/tests/verify_pin.rs::pin_breaks",
        "cargo",
        &[
            "test",
            "--manifest-path",
            "cli/Cargo.toml",
            "--test",
            "verify_pin",
            "pin_breaks",
        ],
    );
}

#[test]
fn unsupported_selector_path_is_refused() {
    let fixture = Fixture::new("charts/curie/ci/assert-pin.sh", CHART_OLD);
    let change = fixture.commit(
        &[
            ("runner/Dockerfile", "value=fixed\n"),
            ("charts/curie/ci/assert-pin.sh", CHART_FIXED),
        ],
        "Fix chart behavior",
    );

    let output = fixture
        .command(&change, "tests/assert-pin.sh")
        .output()
        .expect("run verify fix pin");
    let shown = output_text(&output);
    assert!(
        !output.status.success(),
        "an unsupported selector must fail\n{shown}"
    );
    assert!(
        shown.to_lowercase().contains("selector"),
        "the refusal must identify the selector\n{shown}"
    );
    assert!(!stdout_has_result(&output, "PINNED"));
    assert!(!stdout_has_result(&output, "UNPINNED"));
    fixture.assert_clean_and_single_worktree();
}

#[test]
fn pr_url_uses_the_resolved_patch_and_reports_the_same_pinned_result() {
    let fixture = Fixture::new("charts/curie/ci/assert-pin.sh", CHART_OLD);
    let base = fixture.head();
    let head = fixture.commit(
        &[
            ("runner/Dockerfile", "value=fixed\n"),
            ("charts/curie/ci/assert-pin.sh", CHART_FIXED),
        ],
        "Fix chart behavior",
    );
    let patch_path = fixture.external_path("pr.patch");
    fs::write(&patch_path, fixture.patch(&base, &head)).expect("write PR patch");
    let gh_log = fixture.external_path("gh.log");
    write_exec(
        &fixture.tools,
        "gh",
        r#"#!/bin/sh
printf '%s\n' "$*" >> "$VERIFY_GH_LOG"
if [ "$1" = pr ] && [ "$2" = view ]; then
    exit 0
fi
if [ "$1" = pr ] && [ "$2" = diff ]; then
    # The GitHub CLI manual says --patch requests patch format. This verifier needs the default combined diff so one reverse apply covers later edits: https://cli.github.com/manual/gh_pr_diff
    case "$*" in
        *--patch*) exit 65 ;;
    esac
    case "$*" in
        *--name-only*)
            printf '%s\n' 'charts/curie/ci/assert-pin.sh' 'runner/Dockerfile'
            ;;
        *) cat "$VERIFY_GH_PATCH" ;;
    esac
    exit 0
fi
exit 64
"#,
    );

    let output = fixture
        .command(
            "https://github.com/curie-eng/curie/pull/1479",
            "charts/curie/ci/assert-pin.sh",
        )
        .env("VERIFY_GH_PATCH", &patch_path)
        .env("VERIFY_GH_LOG", &gh_log)
        .output()
        .expect("run verify fix pin for PR");
    assert!(
        output.status.success(),
        "the PR patch must prove the pin\n{}",
        output_text(&output)
    );
    assert!(stdout_has_result(&output, "PINNED"));
    let calls = fs::read_to_string(gh_log).expect("read gh calls");
    assert!(calls.lines().any(|line| line.starts_with("pr view ")));
    assert!(calls.lines().any(|line| line.starts_with("pr diff ")));
    fixture.assert_clean_and_single_worktree();
}

#[test]
fn numeric_pr_uses_gh_even_when_git_would_resolve_the_number_as_a_commit() {
    let fixture = Fixture::new("charts/curie/ci/assert-pin.sh", CHART_OLD);
    let base = fixture.head();
    let head = fixture.commit(
        &[
            ("runner/Dockerfile", "value=fixed\n"),
            ("charts/curie/ci/assert-pin.sh", CHART_FIXED),
        ],
        "Fix chart behavior",
    );
    let patch_path = fixture.external_path("pr.patch");
    fs::write(&patch_path, fixture.patch(&base, &head)).expect("write PR patch");
    let gh_log = fixture.external_path("gh.log");
    let git_log = fixture.external_path("git.log");
    write_exec(
        &fixture.tools,
        "gh",
        r#"#!/bin/sh
printf '%s\n' "$*" >> "$VERIFY_GH_LOG"
if [ "$1" = pr ] && [ "$2" = view ]; then
    exit 0
fi
if [ "$1" = pr ] && [ "$2" = diff ]; then
    # The GitHub CLI manual says --patch requests patch format. This verifier needs the default combined diff so one reverse apply covers later edits: https://cli.github.com/manual/gh_pr_diff
    case "$*" in
        *--patch*) exit 65 ;;
    esac
    case "$*" in
        *--name-only*)
            printf '%s\n' 'charts/curie/ci/assert-pin.sh' 'runner/Dockerfile'
            ;;
        *) cat "$VERIFY_GH_PATCH" ;;
    esac
    exit 0
fi
exit 64
"#,
    );
    write_exec(
        &fixture.tools,
        "git",
        r#"#!/bin/sh
if [ "$1" = -C ] && [ "$3" = rev-parse ] && [ "$4" = --verify ] && [ "$5" = '1479^{commit}' ]; then
    printf 'numeric probe\n' >> "$VERIFY_GIT_LOG"
    printf '%s\n' "$VERIFY_COLLIDING_COMMIT"
    exit 0
fi
command -p git "$@"
"#,
    );

    let output = fixture
        .command("1479", "charts/curie/ci/assert-pin.sh")
        .env("VERIFY_COLLIDING_COMMIT", &head)
        .env("VERIFY_GH_PATCH", &patch_path)
        .env("VERIFY_GH_LOG", &gh_log)
        .env("VERIFY_GIT_LOG", &git_log)
        .output()
        .expect("run verify fix pin for numeric PR");
    assert!(
        output.status.success(),
        "the numeric PR patch must prove the pin\n{}",
        output_text(&output)
    );
    assert!(stdout_has_result(&output, "PINNED"));
    let calls = fs::read_to_string(gh_log).expect("read gh calls");
    assert!(calls.lines().any(|line| line.starts_with("pr view 1479")));
    assert!(calls.lines().any(|line| line.starts_with("pr diff 1479")));
    assert!(
        !git_log.exists(),
        "a numeric PR must not be resolved as a Git commit"
    );
    fixture.assert_clean_and_single_worktree();
}
