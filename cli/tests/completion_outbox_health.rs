//! cluster status and doctor must expose an owed completion outbox (#2422).

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::{Command, Output};

use serde_json::Value;

struct Fixture(tempfile::TempDir);

impl Fixture {
    fn new(state: &str) -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let (count, inflight, retry, oldest, degraded) = match state {
            "retry" => (1, 0, 1, 120.0, true),
            "inflight" => (1, 1, 0, 5.0, false),
            "empty" => (0, 0, 0, 0.0, false),
            _ => panic!("unknown state {state}"),
        };
        fs::write(
            root.join("outbox.json"),
            serde_json::json!({
                "count": count,
                "oldest_age_s": oldest,
                "inflight": inflight,
                "retry": retry,
                "terminal": 0,
                "state": state,
                "degraded": degraded,
            })
            .to_string(),
        )
        .unwrap();
        let worker = serde_json::json!({
            "kind": "Pod", "metadata": {"name": "acme-worker-abc", "labels": {
                "app.kubernetes.io/component": "worker",
                "app.kubernetes.io/instance": "acme"
            }},
            "spec": {"containers": [{"name":"worker", "image":"busybox:1"}]},
            "status": {"phase":"Running", "containerStatuses":[{
                "name":"worker", "image":"busybox:1", "imageID":"containerd://sha256:example",
                "ready":true, "state":{"running":{}}
            }]}
        });
        fs::write(
            root.join("pods.json"),
            serde_json::json!({"items":[worker]}).to_string(),
        )
        .unwrap();
        fs::write(
            root.join("values.json"),
            serde_json::json!({
                "mailAdapter": {"deploy": false},
                "dispatcher": {"slack": {"appToken": "x", "botToken": "x"}},
                "api": {"ingress": {"enabled": true}},
            })
            .to_string(),
        )
        .unwrap();
        fs::write(
            root.join("converged.sh"),
            include_str!("data/converged-installation-read.sh"),
        )
        .unwrap();
        let script = r#"#!/bin/sh
case "${0##*/}:$*" in
  kubectl:*exec*curie_worker.completion_health*) cat "${0%/*}/outbox.json"; exit 0 ;;
  kubectl:*exec*curie_worker.upgrade_drain*)
    printf '%s\n' '{"state":"claims_enabled","since":null,"revision":null}'
    exit 0 ;;
  kubectl:"get pods"*) cat "${0%/*}/pods.json"; exit 0 ;;
  kubectl:"config current-context") printf '%s\n' 'owned-test'; exit 0 ;;
  kubectl:*"component=api"*) printf '%s\n' 'acme-api'; exit 0 ;;
  kubectl:*"component=worker"*) printf '%s\n' 'acme-worker-abc'; exit 0 ;;
  kubectl:*"config view"*) printf '%s\n' 'https://127.0.0.1:6443'; exit 0 ;;
  helm:status*)
    case "$*" in
      *-o*json*) printf '%s\n' '{"version":1,"info":{"status":"deployed"},"hooks":[]}' ;;
      *) printf '%s\n' 'STATUS: deployed' 'REVISION: 1' ;;
    esac
    exit 0 ;;
  helm:version*) printf '%s\n' 'v3.14.0'; exit 0 ;;
  helm:list*) printf '%s\n' '[{"name":"acme","chart":"curie-0.8.7"}]'; exit 0 ;;
  helm:"get values"*) cat "${0%/*}/values.json"; exit 0 ;;
  docker:*) exit 0 ;;
esac
. "${0%/*}/converged.sh"
exit 1
"#;
        for name in ["kubectl", "helm", "docker"] {
            let path = root.join(name);
            fs::write(&path, script).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
        }
        Self(temp)
    }

    fn run(&self, verb: &str) -> Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_curie"));
        command.arg("--json");
        if verb == "status" {
            command.arg("cluster");
        }
        command.args([verb, "--namespace", "outbox-test", "--release", "acme"]);
        command
            .current_dir(self.0.path())
            .env("PATH", format!("{}:/usr/bin:/bin", self.0.path().display()))
            .env_remove("CURIE_API_URL")
            .env_remove("CURIE_API_KEY")
            .output()
            .unwrap()
    }
}

#[test]
fn owed_completion_degrades_status_and_doctor_and_empty_outbox_recovers() {
    for state in ["retry", "inflight", "empty"] {
        let fixture = Fixture::new(state);
        for verb in ["status", "doctor"] {
            let output = fixture.run(verb);
            let value: Value = serde_json::from_slice(&output.stdout).unwrap_or_else(|e| {
                panic!(
                    "{e}: {} / {}",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr)
                )
            });
            if verb == "status" {
                assert_eq!(value["healthy"], state != "retry", "{value}");
                assert_eq!(value["delivery"]["state"], state);
                assert_eq!(value["delivery"]["degraded"], state == "retry");
                assert!(
                    value["delivery"]
                        .as_object()
                        .unwrap()
                        .get("event_id")
                        .is_none(),
                    "{value}"
                );
                assert_eq!(
                    output.status.code(),
                    Some(if state == "retry" { 1 } else { 0 })
                );
            } else {
                let check = value["checks"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|c| c["id"] == "completion-outbox")
                    .expect("completion check");
                assert_eq!(
                    check["state"],
                    if state == "retry" { "missing" } else { "ok" }
                );
                if state == "retry" {
                    assert!(check["detail"].as_str().unwrap().contains("retry"));
                    assert!(check["fix"]
                        .as_str()
                        .unwrap()
                        .contains("do not clear the outbox"));
                }
            }
        }
    }
}

#[test]
fn unreadable_outbox_is_unknown_and_does_not_print_worker_stdout() {
    let fixture = Fixture::new("empty");
    fs::write(
        fixture.0.path().join("outbox.json"),
        "private-event-id-sentinel",
    )
    .unwrap();
    for verb in ["status", "doctor"] {
        let output = fixture.run(verb);
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(!text.contains("private-event-id-sentinel"), "{text}");
        let value: Value = serde_json::from_str(&text).unwrap();
        if verb == "status" {
            assert_eq!(value["delivery"]["state"], "unknown");
            assert_eq!(value["healthy"], true, "{value}");
        }
    }
}
