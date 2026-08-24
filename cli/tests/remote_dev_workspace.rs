//! Deployment-level repository-workspace intent across local, cluster fan-out,
//! wire JSON, and the generated command manifests.

mod support;

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use curie::scaffold::scaffold;
use serde_json::{json, Value};
use support::{serve, MockServer, Response};

const LABEL: &str = "v0.7.0-workspace-test";

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

#[derive(Clone, Copy)]
enum ExistingAgent {
    None,
    Unbound,
    Bound(&'static str),
}

fn agent_json(name: &str, repo: Option<&str>) -> Value {
    let mut agent = json!({
        "id": format!("agent-{name}"),
        "name": name,
        "channel": {"kind": "slack", "address": "C0EXAMPLE1"},
        "created_at": "2026-08-23T00:00:00Z"
    });
    if let Some(repo) = repo {
        agent["repo_full_name"] = json!(repo);
    }
    agent
}

fn deploy_response(req: &support::Request, existing: ExistingAgent) -> Response {
    match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => {
            let agents = match existing {
                ExistingAgent::None => json!([]),
                ExistingAgent::Unbound => json!([agent_json("acme-bot", None)]),
                ExistingAgent::Bound(repo) => json!([agent_json("acme-bot", Some(repo))]),
            };
            Response::json(200, &agents.to_string())
        }
        ("POST", "/agents") => {
            let body: Value = serde_json::from_slice(&req.body).expect("agent body is JSON");
            let repo = body.get("repo_full_name").and_then(Value::as_str);
            Response::json(201, &agent_json("acme-bot", repo).to_string())
        }
        ("POST", path) if path.ends_with("/versions") => Response::json(
            201,
            &json!({
                "id": "version-acme-bot",
                "agent_id": "agent-acme-bot",
                "version_label": LABEL,
                "created_by": "tester",
                "created_at": "2026-08-23T00:00:00Z"
            })
            .to_string(),
        ),
        ("PUT", path) if path.ends_with("/bundle") => Response::json(
            201,
            &json!({
                "version_id": "version-acme-bot",
                "bundle_ref": "bundles/acme-bot.tar.gz",
                "bundle_sha256": "sha-acme-bot",
                "size_bytes": 100
            })
            .to_string(),
        ),
        ("POST", "/deployments") => {
            let body: Value = serde_json::from_slice(&req.body).expect("deployment body is JSON");
            let mut result = json!({
                "id": "deployment-acme-bot",
                "agent_id": "agent-acme-bot",
                "version_id": "version-acme-bot",
                "environment": "dev",
                "workspace_enabled": false,
                "status": "active",
                "deployed_at": "2026-08-23T00:00:00Z"
            });
            if let Some(workspace) = body.get("workspace_enabled") {
                result["workspace_enabled"] = workspace.clone();
            }
            Response::json(201, &result.to_string())
        }
        (method, path) => Response::json(500, &format!("unexpected {method} {path}")),
    }
}

fn run_local(existing: ExistingAgent, extra: &[&str]) -> (Output, MockServer) {
    let plugin = tempfile::tempdir().expect("plugin tempdir");
    scaffold(plugin.path(), "acme-bot").expect("scaffold bundle");
    let server = serve(move |req| deploy_response(req, existing));
    let output = Command::new(bin())
        .args(["local", "deploy", "--plugin-dir"])
        .arg(plugin.path())
        .args([
            "--api-url",
            &server.base_url,
            "--api-key",
            "test-key",
            "--label",
            LABEL,
        ])
        .args(extra)
        .env_remove("CURIE_API_URL")
        .env_remove("CURIE_API_KEY")
        .output()
        .expect("run local deploy");
    (output, server)
}

fn deployment_bodies(server: &MockServer) -> Vec<Value> {
    server
        .recorded()
        .into_iter()
        .filter(|request| request.method == "POST" && request.path == "/deployments")
        .map(|request| serde_json::from_slice(&request.body).expect("deployment body is JSON"))
        .collect()
}

#[test]
fn workspace_enable_is_independent_of_git_flow_repo_binding() {
    let (output, server) = run_local(
        ExistingAgent::None,
        &["--workspace", "--repo", "acme-corp/acme-bot"],
    );
    assert!(
        output.status.success(),
        "deploy failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        deployment_bodies(&server)[0]["workspace_enabled"],
        json!(true)
    );
}

#[test]
fn workspace_enable_with_existing_git_flow_binding_still_sends_capability_only() {
    let (output, server) = run_local(ExistingAgent::Bound("acme-corp/acme-bot"), &["--workspace"]);
    assert!(output.status.success());
    assert_eq!(
        deployment_bodies(&server)[0]["workspace_enabled"],
        json!(true)
    );
}

#[test]
fn no_workspace_sends_explicit_false_while_omission_preserves_server_state() {
    let (disabled, disabled_server) = run_local(ExistingAgent::Unbound, &["--no-workspace"]);
    assert!(disabled.status.success());
    let disabled_body = &deployment_bodies(&disabled_server)[0];
    assert_eq!(disabled_body["workspace_enabled"], json!(false));

    let (omitted, omitted_server) = run_local(ExistingAgent::Unbound, &[]);
    assert!(omitted.status.success());
    assert!(deployment_bodies(&omitted_server)[0]
        .get("workspace_enabled")
        .is_none());
}

#[test]
fn workspace_enable_needs_no_preconfigured_repository() {
    let (output, server) = run_local(ExistingAgent::Unbound, &["--workspace"]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        deployment_bodies(&server)[0]["workspace_enabled"],
        json!(true)
    );
}

#[test]
fn workspace_and_no_workspace_are_mutually_exclusive_on_both_tiers() {
    for tier in ["local", "cluster"] {
        let output = Command::new(bin())
            .args([tier, "deploy", "--workspace", "--no-workspace"])
            .output()
            .expect("run clap conflict");
        assert_eq!(output.status.code(), Some(2), "{tier} accepted both flags");
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains("cannot be used with"), "{tier}: {stderr}");
    }
}

fn write_kubectl_stub(dir: &Path) -> PathBuf {
    let path = dir.join("kubectl");
    fs::write(
        &path,
        r#"#!/bin/sh
case "$*" in
  *"get deployment"*) printf '%s' 'curie' ;;
  *"delete deployment,service,networkpolicy,secret"*) exit 0 ;;
  *) printf 'unexpected kubectl invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#,
    )
    .expect("write kubectl stub");
    let mut permissions = fs::metadata(&path).expect("stub metadata").permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&path, permissions).expect("make stub executable");
    path
}

fn target_agent(name: &str, repo: Option<&str>, channel: &str) -> Value {
    let mut value = agent_json(name, repo);
    value["id"] = json!(format!("agent-{name}"));
    value["channel"]["address"] = json!(channel);
    value
}

fn fanout_response(
    req: &support::Request,
    dev_repo: Option<&str>,
    prod_repo: Option<&str>,
) -> Response {
    match (req.method.as_str(), req.path.as_str()) {
        ("POST", "/deploy-targets/list") => Response::json(
            200,
            &json!({"targets": [
                {"name": "dev", "agent": "acme-dev", "env": "dev", "slack_channel": "C0EXAMPLE1"},
                {"name": "prod", "agent": "acme-prod", "env": "prod", "slack_channel": "C0EXAMPLE2"}
            ]})
            .to_string(),
        ),
        ("POST", "/deploy-targets/resolve") => {
            let body: Value = serde_json::from_slice(&req.body).expect("target body JSON");
            let target = body["target"].as_str().expect("target string");
            Response::json(
                200,
                &json!({
                    "agent": format!("acme-{target}"),
                    "env": target,
                    "slack_channel": if target == "dev" { "C0EXAMPLE1" } else { "C0EXAMPLE2" }
                })
                .to_string(),
            )
        }
        ("GET", "/agents") => Response::json(
            200,
            &json!([
                target_agent("acme-dev", dev_repo, "C0EXAMPLE1"),
                target_agent("acme-prod", prod_repo, "C0EXAMPLE2")
            ])
            .to_string(),
        ),
        ("POST", path) if path.ends_with("/versions") => {
            let agent = path.trim_start_matches("/agents/").trim_end_matches("/versions").trim_end_matches('/');
            Response::json(
                201,
                &json!({
                    "id": format!("version-{agent}"),
                    "agent_id": agent,
                    "version_label": LABEL,
                    "created_by": "tester",
                    "created_at": "2026-08-23T00:00:00Z"
                })
                .to_string(),
            )
        }
        ("PUT", path) if path.ends_with("/bundle") => Response::json(
            201,
            &json!({
                "version_id": "version",
                "bundle_ref": "bundles/acme.tar.gz",
                "bundle_sha256": "sha-acme",
                "size_bytes": 100
            })
            .to_string(),
        ),
        ("POST", "/deployments") => {
            let body: Value = serde_json::from_slice(&req.body).expect("deployment JSON");
            let agent = body["agent_id"].as_str().expect("agent id");
            let mut response = json!({
                "id": format!("deployment-{agent}"),
                "agent_id": agent,
                "version_id": body["version_id"],
                "environment": body["environment"],
                "workspace_enabled": false,
                "status": "active",
                "deployed_at": "2026-08-23T00:00:00Z"
            });
            if let Some(value) = body.get("workspace_enabled") {
                response["workspace_enabled"] = value.clone();
            }
            Response::json(201, &response.to_string())
        }
        ("GET", path) if path.contains("/versions/") && path.contains("/connectors?") => {
            Response::json(200, &json!({
                "manifests": [], "owned_secret_name": "", "owned_secret_keys": [], "mcp_entries": {}
            }).to_string())
        }
        (method, path) => Response::json(500, &format!("unexpected {method} {path}")),
    }
}

fn run_fanout(
    dev_repo: Option<&'static str>,
    prod_repo: Option<&'static str>,
    workspace_flag: Option<bool>,
) -> (Output, MockServer) {
    let plugin = tempfile::tempdir().expect("plugin tempdir");
    scaffold(plugin.path(), "acme-bundle").expect("scaffold bundle");
    fs::write(
        plugin.path().join("deploy.yaml"),
        "targets:\n  dev: { agent: acme-dev, env: dev, slack_channel: C0EXAMPLE1 }\n  prod: { agent: acme-prod, env: prod, slack_channel: C0EXAMPLE2 }\n",
    )
    .expect("write deploy targets");
    let tools = tempfile::tempdir().expect("tool tempdir");
    write_kubectl_stub(tools.path());
    let mut paths = vec![tools.path().to_path_buf()];
    paths.extend(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    ));
    let path = std::env::join_paths(paths).expect("join PATH");
    let server = serve(move |req| fanout_response(req, dev_repo, prod_repo));
    let mut command = Command::new(bin());
    command
        .args(["cluster", "deploy", "--all-targets", "--plugin-dir"])
        .arg(plugin.path())
        .args([
            "--api-url",
            &server.base_url,
            "--api-key",
            "test-key",
            "--namespace",
            "curie",
            "--release",
            "curie",
            "--label",
            LABEL,
            "--json",
        ])
        .env("PATH", path)
        .env_remove("CURIE_API_URL")
        .env_remove("CURIE_API_KEY");
    match workspace_flag {
        Some(true) => {
            command.arg("--workspace");
        }
        Some(false) => {
            command.arg("--no-workspace");
        }
        None => {}
    }
    (command.output().expect("run cluster fanout"), server)
}

#[test]
fn all_targets_enables_runtime_workspace_selection_independently() {
    let (output, server) = run_fanout(
        Some("acme-corp/acme-dev"),
        Some("acme-corp/acme-prod"),
        Some(true),
    );
    assert!(
        output.status.success(),
        "fanout failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let bodies = deployment_bodies(&server);
    assert_eq!(bodies.len(), 2);
    assert_eq!(bodies[0]["workspace_enabled"], json!(true));
    assert_eq!(bodies[1]["workspace_enabled"], json!(true));
}

#[test]
fn all_targets_needs_no_repo_binding_on_either_target() {
    let (output, server) = run_fanout(Some("acme-corp/acme-dev"), None, Some(true));
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let bodies = deployment_bodies(&server);
    assert_eq!(bodies.len(), 2);
    assert!(bodies
        .iter()
        .all(|body| body["workspace_enabled"] == json!(true)));
}

#[test]
fn all_targets_applies_disable_to_each_target_and_omission_to_none() {
    let (disabled, disabled_server) = run_fanout(None, None, Some(false));
    assert!(disabled.status.success());
    let disabled_bodies = deployment_bodies(&disabled_server);
    assert_eq!(disabled_bodies.len(), 2);
    assert!(disabled_bodies
        .iter()
        .all(|body| body["workspace_enabled"] == json!(false)));

    let (omitted, omitted_server) = run_fanout(None, None, None);
    assert!(omitted.status.success());
    assert!(deployment_bodies(&omitted_server)
        .iter()
        .all(|body| body.get("workspace_enabled").is_none()));
}

fn deploy_args(manifest: &Value, tier: &str) -> Vec<Value> {
    let tier = manifest["subcommands"]
        .as_array()
        .expect("root subcommands")
        .iter()
        .find(|item| item["name"] == tier)
        .expect("tier command");
    tier["subcommands"]
        .as_array()
        .expect("tier subcommands")
        .iter()
        .find(|item| item["name"] == "deploy")
        .expect("deploy command")["args"]
        .as_array()
        .expect("deploy args")
        .clone()
}

#[test]
fn command_manifests_expose_both_workspace_intent_flags_on_both_tiers() {
    let live_output = Command::new(bin())
        .arg("schema")
        .output()
        .expect("curie schema");
    assert!(live_output.status.success());
    let live: Value = serde_json::from_slice(&live_output.stdout).expect("live manifest JSON");
    let committed_path = concat!(env!("CARGO_MANIFEST_DIR"), "/command-manifest.json");
    let committed: Value = serde_json::from_str(
        &fs::read_to_string(committed_path).expect("committed command manifest"),
    )
    .expect("committed manifest JSON");

    for manifest in [&live, &committed] {
        for tier in ["local", "cluster"] {
            let args = deploy_args(manifest, tier);
            for (id, long) in [("workspace", "workspace"), ("no_workspace", "no-workspace")] {
                let arg = args
                    .iter()
                    .find(|arg| arg["id"] == id)
                    .unwrap_or_else(|| panic!("{tier} deploy missing --{long}"));
                assert_eq!(arg["long"], long);
                assert_eq!(arg["required"], false);
            }
        }
    }

    let ui_manifest =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../apps/ui/src/generated/commandManifest.ts");
    let ui = fs::read_to_string(ui_manifest).expect("committed UI command manifest");
    assert!(ui.contains("\"id\": \"workspace\""));
    assert!(ui.contains("\"id\": \"no_workspace\""));
    assert!(ui.contains("\"long\": \"no-workspace\""));
}
