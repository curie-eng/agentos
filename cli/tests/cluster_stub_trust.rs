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

if "get deployment curie-dispatcher" in args:
    if os.environ.get("CURIE_TEST_CONNECTED") == "1":
        print("deployment.apps/curie-dispatcher")
    sys.exit(0)

# Permit implementations to snapshot either the Deployment or Helm values.
if "get deployment curie-worker" in args and "-o json" in args:
    print(json.dumps({"spec":{"template":{"spec":{"containers":[{
        "name":"worker", "env":[{"name":"CURIE_SLACK_TRUSTED_ORIGINS","value":""}]
    }]}}}}))
    sys.exit(0)
if "get values" in args and "-o json" in args:
    print("{}")
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

fn run_cluster_message(connected: bool) -> (Output, Vec<String>) {
    let tools = tempfile::tempdir().expect("create fake tool directory");
    write_tool(tools.path(), "kubectl");
    write_tool(tools.path(), "helm");
    let log_path = tools.path().join("cluster.log");
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
        .env_remove("CURIE_API_KEY")
        .env_remove("CURIE_VALKEY_PASSWORD");
    if connected {
        command
            .env("CURIE_TEST_CONNECTED", "1")
            .env("CURIE_SLACK_BOT_TOKEN", "xoxb-example");
    }
    let output = command.output().expect("run cluster message");
    let lines = fs::read_to_string(log_path)
        .expect("read fake cluster log")
        .lines()
        .map(str::to_owned)
        .collect();
    (output, lines)
}

fn trust_mutations(lines: &[String]) -> Vec<(usize, &str)> {
    lines
        .iter()
        .enumerate()
        .filter(|(_, line)| {
            (line.contains("set env") || line.contains("patch") || line.contains("upgrade"))
                && (line.contains("CURIE_SLACK_TRUSTED_ORIGINS")
                    || line.contains("worker.slackTrustedOrigins"))
        })
        .map(|(index, line)| (index, line.as_str()))
        .collect()
}

#[test]
fn disconnected_cluster_message_temporarily_trusts_portless_stub_and_restores_on_error() {
    let (output, lines) = run_cluster_message(false);
    assert!(!output.status.success(), "fixture must reach its forced tunnel error");

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
    assert!(apply_index < forward_index, "trust must precede enqueue plumbing: {lines:#?}");
    assert!(restore_index > forward_index, "trust must be restored on error: {lines:#?}");
}

#[test]
fn connected_cluster_message_never_mutates_worker_stub_trust() {
    let (output, lines) = run_cluster_message(true);
    assert!(!output.status.success(), "fixture must reach its forced tunnel error");
    assert!(
        trust_mutations(&lines).is_empty(),
        "a Slack-connected release must not receive temporary stub trust: {lines:#?}"
    );
}
