//! Regression for #1812: the no-Slack cluster-message stub must be trusted only
//! for the lifetime of that command.  These tests drive the real CLI entrypoint;
//! the fake cluster records mutations and deliberately fails the Valkey tunnel
//! so the cleanup-on-error path is deterministic and needs no live cluster.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn write_tool(dir: &Path, name: &str) -> PathBuf {
    let path = dir.join(name);
    let body = r#"#!/usr/bin/env python3
import json
import os
import sys

args = " ".join(sys.argv[1:])
with open(os.environ["CURIE_TEST_CLUSTER_LOG"], "a", encoding="utf-8") as log:
    log.write(os.path.basename(sys.argv[0]) + " " + args + "\n")

state_path = os.environ["CURIE_TEST_CLUSTER_STATE"]
try:
    with open(state_path, encoding="utf-8") as state_file:
        state = json.load(state_file)
except FileNotFoundError:
    state = {"trust": os.environ.get("CURIE_TEST_EXISTING_TRUST"), "holder": None, "version": 1}

def save():
    with open(state_path, "w", encoding="utf-8") as state_file:
        json.dump(state, state_file)

if "get deployment curie-dispatcher" in args:
    if os.environ.get("CURIE_TEST_DISPATCHER_PROBE_FAIL") == "1":
        print("intentional dispatcher probe failure", file=sys.stderr)
        sys.exit(1)
    if os.environ.get("CURIE_TEST_CONNECTED") == "1":
        print("deployment.apps/curie-dispatcher")
    sys.exit(0)

# Model the Deployment snapshot and resourceVersion JSON Patch used by #1812.
if "get deployment curie-worker" in args and "-o json" in args:
    env = [] if state["trust"] is None else [{"name":"CURIE_SLACK_TRUSTED_ORIGINS", "value":state["trust"]}]
    annotations = {} if state["holder"] is None else {"curie.dev/cluster-message-trust-holder":state["holder"]}
    print(json.dumps({"metadata":{"resourceVersion":str(state["version"]), "annotations":annotations}, "spec":{"template":{"spec":{"containers":[{"name":"worker", "env":env}]}}}}))
    sys.exit(0)
if "patch deployment curie-worker" in args:
    patch = json.loads(sys.argv[sys.argv.index("-p") + 1])
    for operation in patch:
        path = operation["path"].replace("~1", "/").replace("~0", "~")
        if path.endswith("/env"):
            state["trust"] = next((entry["value"] for entry in operation["value"] if entry.get("name") == "CURIE_SLACK_TRUSTED_ORIGINS"), None)
        elif path.endswith("/cluster-message-trust-holder"):
            state["holder"] = operation.get("value") if operation["op"] != "remove" else None
    state["version"] += 1
    save()
    sys.exit(0)

if "rollout status" in args:
    sys.exit(0)

if "get pods" in args and "-o json" in args:
    polls = int(state.get("pod_polls") or 0) + 1
    state["pod_polls"] = polls
    save()
    new_pod = {
        "metadata": {"name": "curie-worker-new"},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
    }
    old_pod = {
        "metadata": {"name": "curie-worker-old", "deletionTimestamp": "2026-08-23T00:00:00Z"},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
    }
    items = [new_pod, old_pod] if os.environ.get("CURIE_TEST_TERMINATING_WORKER") == "1" and polls == 1 else [new_pod]
    print(json.dumps({"items": items}))
    sys.exit(0)

# The Valkey forward fails after temporary trust should have been installed.
if "port-forward" in args and "valkey" in args:
    print("intentional Valkey tunnel failure", file=sys.stderr)
    sys.exit(1)

sys.exit(0)
"#;
    fs::write(&path, body).expect("write fake cluster tool");
    let mut permissions = fs::metadata(&path).expect("stat fake tool").permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&path, permissions).expect("make fake tool executable");
    path
}

fn run_cluster_message(
    connected: bool,
    existing_trust: Option<&str>,
    dispatcher_probe_fails: bool,
) -> (Output, Vec<String>, serde_json::Value) {
    run_cluster_message_with(connected, existing_trust, dispatcher_probe_fails, false)
}

fn run_cluster_message_with(
    connected: bool,
    existing_trust: Option<&str>,
    dispatcher_probe_fails: bool,
    terminating_worker: bool,
) -> (Output, Vec<String>, serde_json::Value) {
    let tools = tempfile::tempdir().expect("create fake tool directory");
    write_tool(tools.path(), "kubectl");
    write_tool(tools.path(), "helm");
    let log_path = tools.path().join("cluster.log");
    let state_path = tools.path().join("cluster-state.json");
    fs::write(
        &state_path,
        serde_json::json!({"trust": existing_trust, "holder": null, "version": 1}).to_string(),
    )
    .expect("seed fake cluster state");
    let inherited_path = std::env::var("PATH").unwrap_or_default();
    let path = format!("{}:{inherited_path}", tools.path().display());

    let mut command = Command::new(bin());
    command
        .args([
            "cluster",
            "message",
            "hello",
            "--channel",
            "C0EXAMPLE1",
            "--chart",
            "charts/curie",
            "--listen-host",
            "10.20.30.40",
            "--listen-port",
            "0",
            "--valkey-local-port",
            "0",
            "--api-key",
            "example-api-key",
            "--valkey-password",
            "example-valkey-password",
            "--timeout-secs",
            "1",
        ])
        .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/.."))
        .env("PATH", path)
        .env("CURIE_TEST_CLUSTER_LOG", &log_path)
        .env("CURIE_TEST_CLUSTER_STATE", &state_path)
        .env_remove("CURIE_API_KEY")
        .env_remove("CURIE_VALKEY_PASSWORD");
    if connected {
        command
            .env("CURIE_TEST_CONNECTED", "1")
            .env("CURIE_SLACK_BOT_TOKEN", "xoxb-example");
    }
    if let Some(existing_trust) = existing_trust {
        command.env("CURIE_TEST_EXISTING_TRUST", existing_trust);
    }
    if dispatcher_probe_fails {
        command.env("CURIE_TEST_DISPATCHER_PROBE_FAIL", "1");
    }
    if terminating_worker {
        command.env("CURIE_TEST_TERMINATING_WORKER", "1");
    }
    let output = command.output().expect("run cluster message");
    let lines = fs::read_to_string(log_path)
        .expect("read fake cluster log")
        .lines()
        .map(str::to_owned)
        .collect();
    let state =
        serde_json::from_str(&fs::read_to_string(state_path).expect("read fake cluster state"))
            .expect("parse fake cluster state");
    (output, lines, state)
}

fn trust_mutations(lines: &[String]) -> Vec<(usize, &str)> {
    lines
        .iter()
        .enumerate()
        .filter(|(_, line)| {
            (line.contains("set env") || line.contains("patch") || line.contains("upgrade"))
                && (line.contains("CURIE_SLACK_TRUSTED_ORIGINS")
                    || line.contains("worker.slackTrustedOrigins")
                    || line.contains("cluster-message-trust-holder"))
        })
        .map(|(index, line)| (index, line.as_str()))
        .collect()
}

#[test]
fn disconnected_cluster_message_temporarily_trusts_portless_stub_and_restores_on_error() {
    let (output, lines, state) = run_cluster_message(false, None, false);
    assert!(
        !output.status.success(),
        "fixture must reach its forced tunnel error"
    );

    let mutations = trust_mutations(&lines);
    assert!(
        mutations.len() >= 2,
        "stub trust must be installed then restored even when plumbing fails: {lines:#?}"
    );
    let (apply_index, apply) = mutations[0];
    assert!(
        apply.contains("http://10.20.30.40"),
        "temporary trust must use the advertised stub host: {apply}"
    );
    assert!(
        !apply.contains("http://10.20.30.40:"),
        "trusted origins are portless, including for an ephemeral stub port: {apply}"
    );
    let forward_index = lines
        .iter()
        .position(|line| line.contains("port-forward") && line.contains("valkey"))
        .expect("message must attempt its Valkey tunnel");
    let restore_index = mutations.last().expect("restore mutation").0;
    assert!(
        apply_index < forward_index,
        "trust must precede enqueue plumbing: {lines:#?}"
    );
    assert!(
        restore_index > forward_index,
        "trust must be restored on error: {lines:#?}"
    );
    assert!(
        state["trust"].is_null(),
        "the default release must finish with no stub trust env at all: {state}"
    );
    assert!(
        state["holder"].is_null(),
        "the temporary ownership marker must also be removed: {state}"
    );
}

#[test]
fn disconnected_cluster_message_restores_a_preexisting_trusted_origin() {
    let original = "https://trusted.example.com";
    let (output, lines, state) = run_cluster_message(false, Some(original), false);
    assert!(
        !output.status.success(),
        "fixture must reach its forced tunnel error"
    );
    assert!(
        trust_mutations(&lines).len() >= 2,
        "fixture must exercise apply and cleanup: {lines:#?}"
    );
    assert_eq!(
        state["trust"], original,
        "cleanup must preserve trust that predated this command"
    );
}

#[test]
fn connected_or_unprobeable_dispatcher_never_mutates_worker_stub_trust() {
    let (output, lines, state) = run_cluster_message(true, None, false);
    assert!(
        !output.status.success(),
        "fixture must reach its forced tunnel error"
    );
    assert!(
        trust_mutations(&lines).is_empty(),
        "a Slack-connected release must not receive temporary stub trust: {lines:#?}"
    );
    assert!(state["trust"].is_null());

    let (output, lines, state) = run_cluster_message(false, None, true);
    assert!(
        !output.status.success(),
        "fixture must reach its forced tunnel error"
    );
    assert!(
        trust_mutations(&lines).is_empty(),
        "a failed dispatcher probe is not proof it is safe to mutate worker trust: {lines:#?}"
    );
    assert!(state["trust"].is_null());
}

#[test]
fn disconnected_cluster_message_waits_out_a_terminating_worker_before_enqueue() {
    // #1532: the first cluster message rolls the worker to install stub trust.
    // Enqueue must wait until the outgoing pod is gone, otherwise that
    // consumer can claim the turn during SIGTERM and strand it for 15 minutes.
    let (output, lines, state) = run_cluster_message_with(false, None, false, true);
    assert!(
        !output.status.success(),
        "fixture must reach its forced tunnel error"
    );

    let rollout_index = lines
        .iter()
        .position(|line| line.contains("rollout status"))
        .expect("trust install waits for rollout status");
    let pod_indices: Vec<usize> = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| line.contains("get pods"))
        .map(|(index, _)| index)
        .collect();
    assert!(
        pod_indices.len() >= 2,
        "a terminating worker must be polled until it disappears: {lines:#?}"
    );
    let forward_index = lines
        .iter()
        .position(|line| line.contains("port-forward") && line.contains("valkey"))
        .expect("message must attempt its Valkey tunnel");
    assert!(
        rollout_index < pod_indices[0],
        "pod wait follows rollout status: {lines:#?}"
    );
    assert!(
        *pod_indices.last().expect("pod wait") < forward_index,
        "enqueue plumbing must not start while a terminating worker remains: {lines:#?}"
    );
    assert!(
        state["pod_polls"].as_u64().unwrap_or(0) >= 2,
        "the terminating snapshot must have been replaced before enqueue: {state}"
    );
}
