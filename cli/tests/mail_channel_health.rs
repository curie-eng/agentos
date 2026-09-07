//! The real status and doctor entry points must expose an unusable mail token.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::{Command, Output};

use serde_json::Value;

struct Fixture(tempfile::TempDir);

impl Fixture {
    fn new(state: &str) -> Self {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        fs::write(
            root.join("status.json"),
            serde_json::json!({
                "status": "ready",
                "channel_token": {"present": true, "exp": 1_800_000_000, "state": state},
                "last_ingress_status": if state == "rejected" { Some(401) } else { None },
            })
            .to_string(),
        )
        .unwrap();
        let pod = serde_json::json!({
            "kind": "Pod", "metadata": {"name": "acme-mail-abc", "labels": {
                "app.kubernetes.io/component": "mail-adapter",
                "app.kubernetes.io/instance": "acme", "component": "acme-probe"
            }},
            "spec": {"containers": [{"name":"probe", "image":"busybox:1"}]},
            "status": {"phase":"Running", "containerStatuses":[{
                "name":"probe", "image":"busybox:1", "imageID":"containerd://sha256:example",
                "ready":true, "state":{"running":{}}
            }]}
        });
        fs::write(
            root.join("pods.json"),
            serde_json::json!({"items":[pod]}).to_string(),
        )
        .unwrap();
        fs::write(
            root.join("converged.sh"),
            include_str!("data/converged-installation-read.sh"),
        )
        .unwrap();
        let script = r#"#!/bin/sh
case "${0##*/}:$*" in
  kubectl:*--raw*) cat "${0%/*}/status.json"; exit 0 ;;
  kubectl:"get pods"*) cat "${0%/*}/pods.json"; exit 0 ;;
  kubectl:"config current-context") printf '%s\n' 'owned-test'; exit 0 ;;
  kubectl:*"component=api"*) printf '%s\n' 'acme-api'; exit 0 ;;
  kubectl:*"config view"*) printf '%s\n' 'https://127.0.0.1:6443'; exit 0 ;;
  helm:status*)
    case "$*" in
      *-o*json*) printf '%s\n' '{"version":1,"info":{"status":"deployed"},"hooks":[]}' ;;
      *) printf '%s\n' 'STATUS: deployed' 'REVISION: 1' ;;
    esac
    exit 0 ;;
  helm:version*) printf '%s\n' 'v3.14.0'; exit 0 ;;
  helm:list*) printf '%s\n' '[{"name":"acme","chart":"curie-0.8.7"}]'; exit 0 ;;
  helm:"get values"*) printf '%s\n' '{"mailAdapter":{"deploy":true},"dispatcher":{"slack":{"appToken":"x","botToken":"x"}},"api":{"ingress":{"enabled":true}}}'; exit 0 ;;
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
        command.args([verb, "--namespace", "mail-test", "--release", "acme"]);
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
fn expired_mail_token_is_visible_on_status_and_doctor_and_valid_token_recovers() {
    for state in ["expired", "rejected", "ok"] {
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
                assert_eq!(value["healthy"], state == "ok", "{value}");
                assert_eq!(
                    value["pods"]["rows"][0]["mail_channel"]["channel_token"]["state"],
                    state
                );
                assert_eq!(
                    output.status.code(),
                    Some(if state == "ok" { 0 } else { 1 })
                );
            } else {
                let check = value["checks"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|c| c["id"] == "mail-channel")
                    .expect("mail check");
                assert_eq!(check["state"], if state == "ok" { "ok" } else { "missing" });
                assert!(check["detail"].as_str().unwrap().contains(state));
                if state != "ok" {
                    assert!(check["fix"].as_str().unwrap().contains("/channels/token"));
                }
            }
        }
    }
}

#[test]
fn unreadable_mail_status_is_unknown_and_does_not_render_response_data() {
    let fixture = Fixture::new("ok");
    fs::write(
        fixture.0.path().join("status.json"),
        "private-response-sentinel",
    )
    .unwrap();
    for verb in ["status", "doctor"] {
        let output = fixture.run(verb);
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(!text.contains("private-response-sentinel"));
        let value: Value = serde_json::from_str(&text).unwrap();
        if verb == "status" {
            assert_eq!(value["healthy"], false);
            assert!(value["pods"]["rows"][0]["status"]
                .as_str()
                .unwrap()
                .contains("unknown"));
        } else {
            let check = value["checks"]
                .as_array()
                .unwrap()
                .iter()
                .find(|check| check["id"] == "mail-channel")
                .unwrap();
            assert_eq!(check["state"], "missing");
            assert!(check["detail"].as_str().unwrap().contains("unknown"));
        }
    }
}
