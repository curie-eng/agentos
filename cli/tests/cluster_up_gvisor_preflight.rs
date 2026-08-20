//! Binary integration contract for the gVisor preflight diagnosis in issue
//! #1653. Every case drives the real `curie cluster up` entrypoint with fake
//! Helm and kubectl executables on PATH.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::thread;
use std::time::{Duration, Instant};

const TARGET_RELEASE: &str = "target-release";
const TARGET_NAMESPACE: &str = "target-namespace";
const RENDERED_JOB: &str = "acme-runtime-preflight-gvisor";
const RUNTIME_CLASS_REJECTION: &str = "Error creating: pods \"acme-runtime-preflight-gvisor-example\" is forbidden: pod rejected: RuntimeClass \"gvisor\" not found";

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
    upgrade_log: PathBuf,
    event_log: PathBuf,
    helm_pid: PathBuf,
    helm_termination_log: PathBuf,
    helm_pending: PathBuf,
    namespace_ready: PathBuf,
    fresh_event: PathBuf,
    watch_pid: PathBuf,
    event_emitted: PathBuf,
    event_mode: String,
}

impl Fixture {
    fn new(event_mode: &str) -> Self {
        let temp = tempfile::tempdir().expect("temporary directory");
        let bin_dir = temp.path().join("bin");
        fs::create_dir(&bin_dir).expect("create fake binary directory");
        let upgrade_log = temp.path().join("upgrades.log");
        let event_log = temp.path().join("event-queries.log");
        let helm_pid = temp.path().join("helm.pid");
        let helm_termination_log = temp.path().join("helm-termination.log");
        let helm_pending = temp.path().join("helm-pending");
        let namespace_ready = temp.path().join("namespace-ready");
        let fresh_event = temp.path().join("fresh-event");
        let watch_pid = temp.path().join("watch.pid");
        let event_emitted = temp.path().join("event-emitted");

        write_exec(
            &bin_dir,
            "helm",
            r#"#!/bin/sh
if [ "$1" = "get" ] && [ "$2" = "values" ]; then
    printf '%s\n' 'Error: release: not found' >&2
    exit 1
fi

if [ "$1" = "template" ]; then
    fullname="target-release"
    platform_create="true"
    sandbox_create="true"
    gvisor_mode="auto"
    fake_model="true"
    install_runtimeclass="false"
    show_only=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --set|--set-string)
                shift
                case "$1" in
                    fullnameOverride=*) fullname=${1#*=} ;;
                    priorityClasses.platform.create=*) platform_create=${1#*=} ;;
                    priorityClasses.sandbox.create=*) sandbox_create=${1#*=} ;;
                    security.gvisor.mode=*) gvisor_mode=${1#*=} ;;
                    security.gvisor.installRuntimeClass=*) install_runtimeclass=${1#*=} ;;
                    agentSandbox.runner.fakeModel=*) fake_model=${1#*=} ;;
                esac
                ;;
            --show-only)
                shift
                show_only="$1"
                ;;
            --show-only=*)
                show_only=${1#*=}
                ;;
        esac
        shift
    done

    if [ "$show_only" = "templates/preflight-gvisor.yaml" ]; then
        if [ "$gvisor_mode" = "off" ] || { [ "$gvisor_mode" = "auto" ] && [ "$fake_model" = "true" ]; }; then
            printf '%s\n' 'Error: could not find template templates/preflight-gvisor.yaml in chart' >&2
            exit 1
        fi
        if [ "$install_runtimeclass" = "true" ]; then
            printf '%s\n' \
                'apiVersion: node.k8s.io/v1' \
                'kind: RuntimeClass' \
                'metadata:' \
                '  name: gvisor' \
                'handler: runsc' \
                '---'
        fi
        printf '%s\n' \
            'apiVersion: batch/v1' \
            'kind: Job' \
            'metadata:' \
            "  name: $fullname-preflight-gvisor" \
            'spec:' \
            '  backoffLimit: 0'
        exit 0
    fi

    if [ "$show_only" = "templates/priorityclass.yaml" ]; then
        first="true"
        if [ "$platform_create" = "true" ]; then
            printf '%s\n' \
                'apiVersion: scheduling.k8s.io/v1' \
                'kind: PriorityClass' \
                'metadata:' \
                '  name: curie-platform' \
                'value: 1000000' \
                'globalDefault: false'
            first="false"
        fi
        if [ "$sandbox_create" = "true" ]; then
            if [ "$first" = "false" ]; then
                printf '%s\n' '---'
            fi
            printf '%s\n' \
                'apiVersion: scheduling.k8s.io/v1' \
                'kind: PriorityClass' \
                'metadata:' \
                '  name: curie-sandbox' \
                'value: 100000' \
                'globalDefault: false'
        fi
        exit 0
    fi

    printf 'unexpected helm template invocation: %s\n' "$*" >&2
    exit 64
fi

if [ "$1" = "upgrade" ] && [ "$2" = "--install" ]; then
    printf '%s\n' "$*" >> "$CURIE_TEST_UPGRADE_LOG"
    printf '%s\n' "$$" > "$CURIE_TEST_HELM_PID"
    gvisor_mode="auto"
    for argument in "$@"; do
        case "$argument" in
            security.gvisor.mode=*) gvisor_mode=${argument#*=} ;;
        esac
    done
    if [ -e "$CURIE_TEST_HELM_PENDING" ]; then
        printf '%s\n' 'Error: UPGRADE FAILED: another operation (install/upgrade/rollback) is in progress' >&2
        exit 1
    fi
    if { [ "$CURIE_TEST_EVENT_MODE" = "matching" ] || [ "$CURIE_TEST_EVENT_MODE" = "fresh-namespace" ]; } && [ "$gvisor_mode" = "require" ]; then
        : > "$CURIE_TEST_HELM_PENDING"
        graceful_exit() {
            signal="$1"
            if [ -n "${sleep_pid:-}" ]; then
                kill "$sleep_pid" 2>/dev/null || true
                wait "$sleep_pid" 2>/dev/null || true
            fi
            rm -f "$CURIE_TEST_HELM_PENDING"
            printf '%s\n' "$signal" >> "$CURIE_TEST_HELM_TERMINATION_LOG"
            exit 1
        }
        trap 'graceful_exit HUP' HUP
        trap 'graceful_exit INT' INT
        trap 'graceful_exit TERM' TERM
        if [ "$CURIE_TEST_EVENT_MODE" = "fresh-namespace" ]; then
            : > "$CURIE_TEST_NAMESPACE_READY"
            : > "$CURIE_TEST_FRESH_EVENT"
        fi
        sleep 5 &
        sleep_pid=$!
        wait "$sleep_pid"
        trap - HUP INT TERM
        rm -f "$CURIE_TEST_HELM_PENDING"
        printf '%s\n' 'Error: UPGRADE FAILED: pre-upgrade hooks failed: job target-release-preflight-gvisor failed: DeadlineExceeded' >&2
        exit 1
    fi
    sleep 1
    printf '%s\n' 'Release installed'
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
    if [ "$3" = "target-namespace" ] && [ "$CURIE_TEST_EVENT_MODE" = "fresh-namespace" ] && [ ! -e "$CURIE_TEST_NAMESPACE_READY" ]; then
        printf '%s\n' 'Error from server (NotFound): namespaces "target-namespace" not found' >&2
        exit 1
    fi
    exit 0
fi

if [ "$1" = "get" ] && [ "$2" = "priorityclass" ]; then
    exit 0
fi

if [ "$1" = "get" ] && { [ "$2" = "event" ] || [ "$2" = "events" ]; }; then
    printf '%s\n' "$*" >> "$CURIE_TEST_EVENT_LOG"
    case " $* " in
        *" --all-namespaces "*)
            printf '%s\n' 'Error from server (Forbidden): events is forbidden at the cluster scope' >&2
            exit 1
            ;;
        *" -n target-namespace "*) ;;
        *)
            printf 'event query was not scoped to target-namespace: %s\n' "$*" >&2
            exit 64
            ;;
    esac
    watch="false"
    watch_only="false"
    output_watch_events="false"
    combined_jsonpath="false"
    unit_separator_jsonpath="false"
    uid_snapshot="false"
    for arg in "$@"; do
        case "$arg" in
            --watch|--watch=true|-w) watch="true" ;;
            --watch-only) watch_only="true" ;;
            --output-watch-events) output_watch_events="true" ;;
            jsonpath=*items*metadata.uid*) uid_snapshot="true" ;;
            jsonpath=*.object.metadata.uid*object.involvedObject.kind*) combined_jsonpath="true" ;;
        esac
        case "$arg" in
            *\\u001f*) unit_separator_jsonpath="true" ;;
        esac
    done

    if [ "$CURIE_TEST_EVENT_MODE" = "watch-unavailable" ]; then
        printf '%s\n' 'Error from server (Forbidden): events is forbidden in target-namespace' >&2
        exit 1
    fi
    if [ "$watch" != "true" ]; then
        if [ "$uid_snapshot" != "true" ] || [ "$watch_only" = "true" ] || [ "$output_watch_events" = "true" ]; then
            printf 'unexpected Event UID snapshot invocation: %s\n' "$*" >&2
            exit 64
        fi
        if [ "$CURIE_TEST_EVENT_MODE" = "matching" ]; then
            printf '%s\n' 'existing-event-uid'
        fi
        exit 0
    fi
    if [ "$watch" != "true" ] || [ "$watch_only" = "true" ] || [ "$output_watch_events" != "true" ] || [ "$combined_jsonpath" != "true" ] || [ "$unit_separator_jsonpath" != "true" ]; then
        printf 'unexpected list and watch invocation: %s\n' "$*" >&2
        exit 64
    fi
    if [ "$CURIE_TEST_EVENT_MODE" = "fresh-namespace" ] && [ ! -e "$CURIE_TEST_NAMESPACE_READY" ]; then
        printf '%s\n' 'Error from server (NotFound): namespaces "target-namespace" not found' >&2
        exit 1
    fi
    printf '%s\n' "$$" > "$CURIE_TEST_WATCH_PID"
    trap 'exit 0' HUP INT TERM
    if [ "$CURIE_TEST_EVENT_MODE" = "fresh-namespace" ] && [ -e "$CURIE_TEST_FRESH_EVENT" ]; then
        : > "$CURIE_TEST_EVENT_EMITTED"
        printf '%s\037%s\037%s\037%s\037%s\037%s\037%s\n' \
            'ADDED' 'fresh-event-uid' 'Job' 'target-namespace' 'acme-runtime-preflight-gvisor' 'FailedCreate' \
            'Error creating: pods "acme-runtime-preflight-gvisor-example" is forbidden: pod rejected: RuntimeClass "gvisor" not found'
    fi
    if [ "$CURIE_TEST_EVENT_MODE" = "fresh-namespace" ]; then
        sleep 30
        exit 0
    fi
    if [ "$CURIE_TEST_EVENT_MODE" = "matching" ]; then
        printf '%s\037%s\037%s\037%s\037%s\037%s\037%s\n' \
            'ADDED' 'existing-event-uid' 'Job' 'target-namespace' 'acme-runtime-preflight-gvisor' 'FailedCreate' \
            'Error creating: pods "stale-added-preflight-example" is forbidden: pod rejected: RuntimeClass "stale-added" not found'
    fi
    attempts=0
    while [ ! -e "$CURIE_TEST_HELM_PID" ] && [ "$attempts" -lt 100 ]; do
        sleep 0.01
        attempts=$((attempts + 1))
    done
    if [ ! -e "$CURIE_TEST_HELM_PID" ]; then
        printf '%s\n' 'Helm did not start while the Event stream had no initial row' >&2
        exit 64
    fi
    sleep 0.15
    if [ "$CURIE_TEST_EVENT_MODE" = "matching" ]; then
        printf '%s\037%s\037%s\037%s\037%s\037%s\037%s\n' \
            'MODIFIED' 'existing-event-uid' 'Job' 'target-namespace' 'acme-runtime-preflight-gvisor' 'FailedCreate' \
            'Error creating: pods "stale-modified-preflight-example" is forbidden: pod rejected: RuntimeClass "stale-modified" not found'
        sleep 0.05
        event_type='ADDED'
        event_uid='new-event-uid'
        message='Error creating: pods "acme-runtime-preflight-gvisor-example" is forbidden: pod rejected: RuntimeClass "gvisor" not found'
    else
        event_type='ADDED'
        event_uid='new-event-uid'
        message='admission webhook "images.example.com" denied the request'
    fi
    : > "$CURIE_TEST_EVENT_EMITTED"
    printf '%s\037%s\037%s\037%s\037%s\037%s\037%s\n' \
        "$event_type" "$event_uid" 'Job' 'target-namespace' 'acme-runtime-preflight-gvisor' 'FailedCreate' "$message"
    sleep 30
    exit 0
fi

printf 'unexpected kubectl invocation: %s\n' "$*" >&2
exit 64
"#,
        );

        Self {
            _temp: temp,
            bin_dir,
            upgrade_log,
            event_log,
            helm_pid,
            helm_termination_log,
            helm_pending,
            namespace_ready,
            fresh_event,
            watch_pid,
            event_emitted,
            event_mode: event_mode.to_string(),
        }
    }

    fn run(&self, extra: &[&str]) -> (Output, Duration) {
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
            "--fake-model",
            "--set",
            "security.gvisor.mode=require",
            "--set",
            "fullnameOverride=acme-runtime",
        ];
        args.extend_from_slice(extra);

        let started = Instant::now();
        let output = Command::new(bin())
            .args(args)
            .env("PATH", path)
            .env("CI", "1")
            .env("TERM", "dumb")
            .env("NO_COLOR", "1")
            .env("CURIE_TEST_UPGRADE_LOG", &self.upgrade_log)
            .env("CURIE_TEST_EVENT_LOG", &self.event_log)
            .env("CURIE_TEST_HELM_PID", &self.helm_pid)
            .env(
                "CURIE_TEST_HELM_TERMINATION_LOG",
                &self.helm_termination_log,
            )
            .env("CURIE_TEST_HELM_PENDING", &self.helm_pending)
            .env("CURIE_TEST_NAMESPACE_READY", &self.namespace_ready)
            .env("CURIE_TEST_FRESH_EVENT", &self.fresh_event)
            .env("CURIE_TEST_WATCH_PID", &self.watch_pid)
            .env("CURIE_TEST_EVENT_EMITTED", &self.event_emitted)
            .env("CURIE_TEST_EVENT_MODE", &self.event_mode)
            .env_remove("CURIE_CREDENTIALS")
            .env_remove("CURIE_MODEL_CREDENTIALS")
            .env_remove("CURIE_GITHUB_TOKEN")
            .env_remove("CURIE_MODEL")
            .output()
            .expect("run curie cluster up");
        (output, started.elapsed())
    }

    fn upgrade_count(&self) -> usize {
        fs::read_to_string(&self.upgrade_log)
            .unwrap_or_default()
            .lines()
            .count()
    }

    fn assert_event_was_observed_for_rendered_job(&self) {
        assert!(
            self.event_emitted.is_file(),
            "the fake kubectl watch must emit an event before the command finishes"
        );
        let invocations = fs::read_to_string(&self.event_log).unwrap_or_default();
        assert!(
            invocations.contains(&format!("involvedObject.name={RENDERED_JOB}")),
            "the event selector must use the rendered fullname override:\n{invocations}"
        );
        let watch_invocations: Vec<&str> = invocations
            .lines()
            .filter(|line| line.contains("--watch"))
            .collect();
        assert_eq!(
            watch_invocations.len(),
            1,
            "exactly one list and watch stream must observe the install:\n{invocations}"
        );
        assert!(
            watch_invocations[0].contains("{.object.metadata.uid}")
                && watch_invocations[0].contains("{.object.involvedObject.kind}")
                && watch_invocations[0].contains(r#"{"\u001f"}"#)
                && !watch_invocations[0].contains("--watch-only")
                && !invocations.contains("--resource-version"),
            "one list and watch stream must cover current and future Events:\n{invocations}"
        );
        if self.event_mode != "fresh-namespace" {
            let snapshots: Vec<&str> = invocations
                .lines()
                .filter(|line| !line.contains("--watch"))
                .collect();
            assert_eq!(
                snapshots.len(),
                1,
                "an existing namespace must snapshot matching Event UIDs once:\n{invocations}"
            );
            assert!(
                snapshots[0].contains(r#"{range .items[*]}{.metadata.uid}"#),
                "the stale Event boundary must use object UIDs, not list resource versions:\n{invocations}"
            );
        }
        assert!(
            invocations
                .lines()
                .all(|line| line.contains("-n target-namespace")
                    && !line.contains("--all-namespaces")),
            "every event query must use namespaced permissions:\n{invocations}"
        );
    }

    fn assert_graceful_helm_interruption(&self) {
        let signals = fs::read_to_string(&self.helm_termination_log).unwrap_or_default();
        assert_eq!(
            signals, "INT\n",
            "Helm must receive one graceful interrupt before any forced cleanup"
        );
        assert!(
            !self.helm_pending.exists(),
            "the interrupted Helm operation must not leave a pending release"
        );
    }

    fn assert_no_event_watch(&self) {
        assert!(
            fs::read_to_string(&self.event_log)
                .unwrap_or_default()
                .is_empty(),
            "a nonrendering gVisor preflight must not query Events"
        );
        assert!(
            !self.watch_pid.exists(),
            "a nonrendering gVisor preflight must not spawn an Event watch"
        );
    }

    fn assert_children_stopped(&self) {
        assert_process_stopped(&self.helm_pid, "helm upgrade");
        assert_process_stopped(&self.watch_pid, "kubectl event watch");
    }
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn assert_process_stopped(pid_file: &Path, label: &str) {
    let pid = fs::read_to_string(pid_file)
        .unwrap_or_else(|error| panic!("read {label} pid: {error}"))
        .trim()
        .parse::<u32>()
        .unwrap_or_else(|error| panic!("parse {label} pid: {error}"));
    let process = PathBuf::from(format!("/proc/{pid}"));
    for _ in 0..50 {
        if !process.exists() {
            return;
        }
        thread::sleep(Duration::from_millis(10));
    }
    panic!("{label} process {pid} survived curie cluster up");
}

#[test]
fn matching_runtimeclass_rejection_wins_before_helm_deadline() {
    let fixture = Fixture::new("matching");
    let (output, elapsed) = fixture.run(&[]);
    let shown = stderr(&output);

    assert_eq!(
        output.status.code(),
        Some(1),
        "the RuntimeClass rejection must be a permanent runtime failure\nstdout:\n{}\nstderr:\n{shown}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert!(
        elapsed < Duration::from_secs(3),
        "the event must win well before the five second fake Helm deadline, elapsed {elapsed:?}\nstderr:\n{shown}"
    );
    assert!(
        shown.contains(RUNTIME_CLASS_REJECTION),
        "the original Kubernetes rejection must be preserved:\n{shown}"
    );
    assert!(
        !shown.contains("stale-added") && !shown.contains("stale-modified"),
        "Events that existed before this install must be ignored even when modified:\n{shown}"
    );
    assert!(
        shown.contains("--set security.gvisor.mode=off"),
        "the failure must name the explicit opt out:\n{shown}"
    );
    assert!(
        !shown.contains("DeadlineExceeded"),
        "the later Helm timeout must not become the diagnosis:\n{shown}"
    );
    assert_eq!(
        fixture.upgrade_count(),
        1,
        "helm upgrade --install must start exactly once"
    );
    fixture.assert_event_was_observed_for_rendered_job();
    fixture.assert_graceful_helm_interruption();
    fixture.assert_children_stopped();
}

#[test]
fn runtimeclass_document_before_job_still_observes_the_rendered_job() {
    let fixture = Fixture::new("nonmatching");
    let (output, _) = fixture.run(&["--set", "security.gvisor.installRuntimeClass=true"]);
    assert!(
        output.status.success(),
        "a RuntimeClass document before the Job must not block the install\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        stderr(&output)
    );
    assert_eq!(fixture.upgrade_count(), 1);
    fixture.assert_event_was_observed_for_rendered_job();
    fixture.assert_children_stopped();
}

#[test]
fn fresh_namespace_rejection_is_observed_after_helm_creates_the_namespace() {
    let fixture = Fixture::new("fresh-namespace");
    let (output, elapsed) = fixture.run(&[]);
    let shown = stderr(&output);

    assert_eq!(
        output.status.code(),
        Some(1),
        "a fresh namespace must preserve the RuntimeClass failure classification\nstdout:\n{}\nstderr:\n{shown}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert!(
        elapsed < Duration::from_secs(3),
        "the post Helm namespace retry must still beat the fake deadline, elapsed {elapsed:?}\nstderr:\n{shown}"
    );
    assert!(
        shown.contains(RUNTIME_CLASS_REJECTION),
        "the initial watch list must preserve the RuntimeClass rejection:\n{shown}"
    );
    assert!(
        shown.contains("--set security.gvisor.mode=off"),
        "the fresh namespace failure must retain the recovery:\n{shown}"
    );
    assert!(
        !shown.contains("DeadlineExceeded"),
        "the Helm deadline must not replace the fresh namespace diagnosis:\n{shown}"
    );
    assert_eq!(fixture.upgrade_count(), 1);
    assert!(
        fixture.namespace_ready.is_file() && fixture.event_emitted.is_file(),
        "Helm must create the namespace and event before the retry observes them"
    );
    let invocations = fs::read_to_string(&fixture.event_log).unwrap_or_default();
    assert_eq!(
        invocations.lines().count(),
        1,
        "fresh namespace recovery must use one list and watch request after Helm starts:\n{invocations}"
    );
    assert!(
        invocations.contains("{.object.metadata.uid}")
            && invocations.contains("{.object.involvedObject.kind}")
            && invocations.contains(r#"{"\u001f"}"#)
            && !invocations.contains("--resource-version"),
        "the stream must inspect current Events without an EventList resource version:\n{invocations}"
    );
    assert!(
        invocations
            .lines()
            .all(|line| line.contains("-n target-namespace") && !line.contains("--all-namespaces")),
        "fresh namespace retries must retain namespaced permissions:\n{invocations}"
    );
    fixture.assert_graceful_helm_interruption();
    fixture.assert_children_stopped();
}

#[test]
fn nonmatching_failedcreate_does_not_abort_successful_install() {
    let fixture = Fixture::new("nonmatching");
    let (output, _) = fixture.run(&[]);
    let shown = stderr(&output);

    assert!(
        output.status.success(),
        "an unrelated FailedCreate event must not abort cluster up\nstdout:\n{}\nstderr:\n{shown}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert_eq!(
        fixture.upgrade_count(),
        1,
        "helm upgrade --install must run exactly once"
    );
    assert!(
        !shown.contains("--set security.gvisor.mode=off"),
        "an unrelated event must not produce the gVisor remediation:\n{shown}"
    );
    fixture.assert_event_was_observed_for_rendered_job();
    fixture.assert_children_stopped();
}

#[test]
fn matching_runtimeclass_rejection_has_one_json_error() {
    let fixture = Fixture::new("matching");
    let (output, elapsed) = fixture.run(&["--json"]);

    assert_eq!(
        output.status.code(),
        Some(1),
        "the JSON path must preserve the permanent failure classification\nstderr:\n{}",
        stderr(&output)
    );
    assert!(
        elapsed < Duration::from_secs(3),
        "the JSON path must also beat the fake Helm deadline, elapsed {elapsed:?}"
    );
    let payload: serde_json::Value = serde_json::from_slice(&output.stdout)
        .unwrap_or_else(|error| panic!("--json must emit exactly one error object: {error}"));
    assert!(
        payload["error"]
            .as_str()
            .is_some_and(|error| error.contains(RUNTIME_CLASS_REJECTION)),
        "the machine readable error must preserve the Kubernetes rejection: {payload}"
    );
    assert!(
        payload["fix"]
            .as_str()
            .is_some_and(|fix| fix.contains("curie cluster up --set security.gvisor.mode=off")),
        "the machine readable fix must name the explicit opt out: {payload}"
    );
    assert_eq!(fixture.upgrade_count(), 1);
    fixture.assert_event_was_observed_for_rendered_job();
    fixture.assert_graceful_helm_interruption();
    fixture.assert_children_stopped();
}

#[test]
fn graceful_interruption_allows_the_mode_off_recovery_to_run_next() {
    let fixture = Fixture::new("matching");
    let (first, _) = fixture.run(&[]);
    assert_eq!(
        first.status.code(),
        Some(1),
        "the first install must observe the RuntimeClass rejection\nstderr:\n{}",
        stderr(&first)
    );
    fixture.assert_graceful_helm_interruption();

    let (recovery, _) = fixture.run(&["--set", "security.gvisor.mode=off"]);
    assert!(
        recovery.status.success(),
        "the advertised mode off recovery must not be blocked by a pending Helm operation\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&recovery.stdout),
        stderr(&recovery)
    );
    assert_eq!(
        fixture.upgrade_count(),
        2,
        "the rejected install and its recovery must each invoke Helm once"
    );
    assert!(
        !fixture.helm_pending.exists(),
        "the successful recovery must leave no pending Helm operation"
    );
}

#[test]
fn fake_model_auto_and_mode_off_skip_the_event_observer() {
    for setting in ["security.gvisor.mode=auto", "security.gvisor.mode=off"] {
        let fixture = Fixture::new("matching");
        let (output, _) = fixture.run(&["--set", setting]);
        assert!(
            output.status.success(),
            "{setting} must proceed when Helm reports that the conditional template rendered nothing\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            stderr(&output)
        );
        assert_eq!(fixture.upgrade_count(), 1);
        fixture.assert_no_event_watch();
    }
}

#[test]
fn unavailable_event_watch_preserves_the_helm_result() {
    let fixture = Fixture::new("watch-unavailable");
    let (output, _) = fixture.run(&[]);
    assert!(
        output.status.success(),
        "an unavailable Event read must fall back to Helm's result\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        stderr(&output)
    );
    assert_eq!(fixture.upgrade_count(), 1);
    assert!(
        !fixture.watch_pid.exists(),
        "a failed Event snapshot must not spawn the watch process"
    );
}
