//! Integration: `cluster deploy` transport + key-discovery contract (#705,
//! ADR-0057). When no `--api-url` is given, deploy self-plumbs a kubectl
//! port-forward loopback tunnel to `svc/<release>-api` and posts to it, instead
//! of sending the release's strong generated key over the cleartext UI /api
//! NodePort proxy (ADR-0024). When `--api-url` IS given, deploy direct-dials it
//! (no tunnel). When no key is given, deploy discovers the release Secret key
//! rather than defaulting to a dev placeholder; an explicit key wins.
//!
//! These tests pin two pure builders the implementer will add to
//! `cli/src/commands.rs` (imported here from the `curie` lib):
//!
//!   pub fn deploy_port_forward(
//!       api_url: Option<&str>,
//!       namespace: &str,
//!       release: &str,
//!       local_port: u16,
//!       remote_port: u16,
//!   ) -> Option<OpsCommand>
//!
//!   pub fn deploy_needs_key_discovery(explicit_api_key: Option<&str>) -> bool
//!
//! Until both exist this test target fails to compile: that is the intended RED,
//! isolated to this file because it imports from the lib rather than adding
//! inline lib tests.

mod support;

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use curie::api::is_insecure_endpoint;
use curie::commands::{deploy_needs_key_discovery, deploy_port_forward, normalize_deploy_api_key};
use curie::ops::{run_capture, CmdArg, OpsCommand};
use curie::scaffold::scaffold;
use support::{serve, Response};

const AGENT_ID: &str = "11111111-1111-1111-1111-111111111111";
const VERSION_ID: &str = "22222222-2222-2222-2222-222222222222";
const DEPLOYMENT_ID: &str = "33333333-3333-3333-3333-333333333333";

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn output_text(output: &std::process::Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned() + &String::from_utf8_lossy(&output.stderr)
}

fn write_exec(dir: &Path, name: &str, body: &str) -> PathBuf {
    let path = dir.join(name);
    fs::write(&path, body).expect("write fake executable");
    let mut permissions = fs::metadata(&path)
        .expect("stat fake executable")
        .permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&path, permissions).expect("mark fake executable");
    path
}

fn secret_bundle(names: &[&str]) -> tempfile::TempDir {
    let dir = tempfile::tempdir().expect("bundle tempdir");
    scaffold(dir.path(), "secret-agent").expect("scaffold bundle");
    let manifest_path = dir.path().join(".claude-plugin/plugin.json");
    let mut manifest: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(&manifest_path).expect("read plugin manifest"))
            .expect("parse plugin manifest");
    manifest["secrets"] = serde_json::json!(names);
    fs::write(
        manifest_path,
        serde_json::to_vec_pretty(&manifest).expect("serialize plugin manifest"),
    )
    .expect("write plugin manifest");
    dir
}

fn cluster_deploy(api_url: &str, plugin_dir: &Path, config_dir: &Path, extra: &[&str]) -> Command {
    let mut command = Command::new(bin());
    command
        .arg("--json")
        .arg("cluster")
        .arg("deploy")
        .arg("--plugin-dir")
        .arg(plugin_dir)
        .arg("--api-url")
        .arg(api_url)
        .arg("--api-key")
        .arg("test-key")
        .arg("--namespace")
        .arg("secret-ns")
        .arg("--release")
        .arg("secret-release")
        .arg("--label")
        .arg("0.1.0-secret-test")
        .env("CURIE_CONFIG_DIR", config_dir)
        .args(extra);
    command
}

fn agent_json(names: &[&str]) -> String {
    format!(
        r##"{{"id":"{AGENT_ID}","name":"acme-a","slack_channel":"#old","secrets":{},"created_at":"2026-07-05T00:00:00Z"}}"##,
        serde_json::to_string(names).unwrap()
    )
}

fn deploy_response(method: &str, path: &str) -> Response {
    match (method, path) {
        ("GET", "/agents") => Response::json(
            200,
            &format!("[{}]", agent_json(&["ALPHA_TOKEN", "ZETA_TOKEN"])),
        ),
        ("PATCH", p) if p == format!("/agents/{AGENT_ID}") => {
            Response::json(200, &agent_json(&["ALPHA_TOKEN", "ZETA_TOKEN"]))
        }
        ("POST", p) if p == format!("/agents/{AGENT_ID}/versions") => Response::json(
            201,
            &format!(
                r#"{{"id":"{VERSION_ID}","agent_id":"{AGENT_ID}","version_label":"0.1.0-secret-test","bundle_ref":null,"bundle_sha256":null,"created_by":"tester","created_at":"2026-07-05T00:00:00Z"}}"#
            ),
        ),
        ("PUT", p) if p == format!("/agents/{AGENT_ID}/versions/{VERSION_ID}/bundle") => {
            Response::json(
                201,
                &format!(
                    r#"{{"version_id":"{VERSION_ID}","bundle_ref":"bundles/x.tar.gz","bundle_sha256":"deadbeef","size_bytes":512}}"#
                ),
            )
        }
        ("POST", "/deployments") => Response::json(
            201,
            &format!(
                r#"{{"id":"{DEPLOYMENT_ID}","agent_id":"{AGENT_ID}","version_id":"{VERSION_ID}","environment":"dev","status":"active","deployed_at":"2026-07-05T00:00:00Z"}}"#
            ),
        ),
        ("GET", p)
            if p.starts_with(&format!(
                "/agents/{AGENT_ID}/versions/{VERSION_ID}/connectors?"
            )) =>
        {
            Response::json(
                200,
                r#"{"manifests":[],"owned_secret_name":"","owned_secret_keys":[],"mcp_entries":{}}"#,
            )
        }
        _ => Response::json(500, r#"{"detail":"unexpected test request"}"#),
    }
}

fn install_cluster_fakes(capture: &Path) -> std::ffi::OsString {
    write_exec(
        capture,
        "helm",
        r#"#!/bin/sh
printf '%s\n' "$*" >> "$CAPTURE_DIR/helm-argv"
values_file=''
for argument in "$@"; do
  case "$argument" in
    *curie-helm-values-*.yaml) values_file="$argument" ;;
  esac
done
test -n "$values_file" || exit 90
printf '%s' "$values_file" > "$CAPTURE_DIR/helm-values-path"
stat -c '%a' "$values_file" > "$CAPTURE_DIR/helm-values-mode"
cat "$values_file" > "$CAPTURE_DIR/helm-values-body"
exit "${HELM_EXIT:-0}"
"#,
    );
    write_exec(
        capture,
        "kubectl",
        r#"#!/bin/sh
printf '%s\n' "$*" >> "$CAPTURE_DIR/kubectl-argv"
case " $* " in
  *" get deployment "*) printf 'curie' ;;
  *" get sandboxwarmpool"*) printf '1' ;;
  *" get sandboxclaim"*|*" get sandbox"*)
    count_file="$CAPTURE_DIR/identity-count"
    count=0
    if [ -f "$count_file" ]; then count=$(cat "$count_file"); fi
    count=$((count + 1))
    printf '%s' "$count" > "$count_file"
    if [ "$count" -eq 1 ]; then identity='sandbox-old'; else identity='sandbox-new'; fi
    case " $* " in
      *" -o json "*) printf '{"items":[{"metadata":{"name":"%s"},"spec":{"sandboxRef":{"name":"%s"}},"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}' "$identity" "$identity" ;;
      *) printf '%s' "$identity" ;;
    esac
    ;;
esac
exit 0
"#,
    );

    let mut paths = vec![capture.to_path_buf()];
    if let Some(current) = std::env::var_os("PATH") {
        paths.extend(std::env::split_paths(&current));
    }
    std::env::join_paths(paths).expect("join fake PATH")
}

/// (1) Auto path (no `--api-url`) builds a kubectl port-forward to the api
/// service: a loopback tunnel is the whole point of Option C, so the discovered
/// strong key never travels over the cleartext NodePort proxy.
#[test]
fn auto_path_builds_port_forward_to_api_service() {
    let cmd = deploy_port_forward(None, "curie", "curie", 18000, 8000)
        .expect("the auto path (no --api-url) must build a port-forward tunnel");

    assert_eq!(cmd.program, "kubectl");
    let argv = cmd.argv();
    assert!(
        argv.iter().any(|a| a == "port-forward"),
        "expected a port-forward subcommand, got argv {argv:?}"
    );
    assert!(
        argv.iter().any(|a| a == "svc/curie-api"),
        "expected the tunnel to target the api service, got argv {argv:?}"
    );
    assert!(
        argv.iter().any(|a| a == "18000:8000"),
        "expected the local:remote port mapping, got argv {argv:?}"
    );
}

/// (2) Explicit `--api-url` direct-dials: no tunnel is built, so deploy posts
/// straight to the operator-supplied URL.
#[test]
fn explicit_api_url_builds_no_port_forward() {
    let cmd = deploy_port_forward(
        Some("http://example:9000/api"),
        "curie",
        "curie",
        18000,
        8000,
    );
    assert!(
        cmd.is_none(),
        "an explicit --api-url must direct-dial with no port-forward, got {cmd:?}"
    );
}

/// (3) Security: the auto-discovered strong key must neither egress cleartext
/// nor ride the port-forward command line. Two real guards (the old assertion
/// was vacuous -- the key is not an input to `deploy_port_forward`, so it could
/// never appear regardless of the implementation):
///
///   (a) the classifier that GATES the cleartext refusal (`cluster deploy`
///       refuses an auto-discovered key over a non-loopback `http://` --api-url).
///       If a regression stopped flagging the leak case, this fails.
///   (b) the port-forward argv carries no credential-shaped flag, the property
///       the vacuous test only pretended to check.
#[test]
fn discovered_key_cleartext_refusal_and_no_credential_argv() {
    // (a) The refusal gate: FLAG a non-loopback cleartext endpoint (the leak
    // case), CLEAR the loopback tunnel path and any https:// endpoint.
    assert!(
        is_insecure_endpoint("http://lan-host:8000"),
        "a non-loopback http:// --api-url must classify as a cleartext key leak (refused)"
    );
    assert!(
        !is_insecure_endpoint("http://localhost:18000"),
        "the loopback port-forward path must be allowed"
    );
    assert!(
        !is_insecure_endpoint("https://api.example.com"),
        "an https:// endpoint encrypts the key and must be allowed"
    );

    // (b) The port-forward command line carries no credential-shaped argument.
    let cmd = deploy_port_forward(None, "curie", "curie", 18000, 8000)
        .expect("the auto path must build a port-forward tunnel");
    for token in cmd.argv() {
        let lower = token.to_ascii_lowercase();
        assert!(
            !lower.contains("api-key") && !lower.contains("apikey") && !lower.contains("x-api"),
            "the port-forward argv must carry no credential-shaped argument, got {token:?}"
        );
    }
}

/// (4) Key-discovery precedence, both branches: no explicit key means discover
/// the release Secret key; an explicit key wins and skips discovery.
#[test]
fn key_discovery_precedence_both_branches() {
    assert!(
        deploy_needs_key_discovery(None),
        "no explicit key must trigger release Secret discovery"
    );
    assert!(
        !deploy_needs_key_discovery(Some("k")),
        "an explicit key must win and skip discovery"
    );
}

/// (5) An empty or whitespace-only `--api-key` normalizes to `None` so it
/// triggers discovery like an omitted flag instead of posting an empty key.
#[test]
fn normalize_deploy_api_key_blanks_empty_and_whitespace() {
    assert_eq!(normalize_deploy_api_key(Some(String::new())), None);
    assert_eq!(normalize_deploy_api_key(Some("  ".to_string())), None);
    assert_eq!(normalize_deploy_api_key(None), None);
    assert_eq!(
        normalize_deploy_api_key(Some("realkey".to_string())),
        Some("realkey".to_string())
    );
}

#[tokio::test]
async fn secret_values_file_is_private_redacted_and_deleted_on_command_failure() {
    const SENTINEL: &str = "helm_file_only_sentinel_440";
    let capture = tempfile::tempdir().expect("capture tempdir");
    let fake = write_exec(
        capture.path(),
        "helm",
        r#"#!/bin/sh
printf '%s\n' "$@" > "$CAPTURE_DIR/argv"
previous=''
values_file=''
for argument in "$@"; do
  if [ "$previous" = '-f' ]; then
    values_file="$argument"
  fi
  previous="$argument"
done
test -n "$values_file" || exit 90
printf '%s' "$values_file" > "$CAPTURE_DIR/path"
stat -c '%a' "$values_file" > "$CAPTURE_DIR/mode"
cat "$values_file" > "$CAPTURE_DIR/body"
echo 'simulated helm failure' >&2
exit 41
"#,
    );
    let command = OpsCommand {
        program: fake.to_string_lossy().into_owned(),
        args: vec![
            CmdArg::Plain("upgrade".to_string()),
            CmdArg::Plain("secret-release".to_string()),
            CmdArg::SecretValuesFile(vec![(
                "agentSandbox.connectorSecrets.acme-a.ALPHA_TOKEN".to_string(),
                SENTINEL.to_string(),
            )]),
        ],
        env: vec![(
            "CAPTURE_DIR".to_string(),
            capture.path().to_string_lossy().into_owned(),
        )],
        secret_env: Vec::new(),
    };

    assert!(
        !command.display().contains(SENTINEL),
        "masked command output exposed the value: {}",
        command.display()
    );
    let (ok, stdout, stderr) = run_capture(&command).await.unwrap();

    assert!(!ok, "the fake helm command must fail");
    assert_eq!(
        fs::read_to_string(capture.path().join("mode"))
            .unwrap()
            .trim(),
        "600"
    );
    let body: serde_json::Value = serde_json::from_str(
        &fs::read_to_string(capture.path().join("body")).expect("captured values file"),
    )
    .unwrap();
    assert_eq!(
        body,
        serde_json::json!({
            "agentSandbox": {
                "connectorSecrets": {
                    "acme-a": {"ALPHA_TOKEN": SENTINEL}
                }
            }
        })
    );
    let argv = fs::read_to_string(capture.path().join("argv")).unwrap();
    assert!(!argv.contains(SENTINEL), "subprocess argv leaked: {argv}");
    assert!(!stdout.contains(SENTINEL), "stdout leaked: {stdout}");
    assert!(!stderr.contains(SENTINEL), "stderr leaked: {stderr}");
    let values_path = PathBuf::from(fs::read_to_string(capture.path().join("path")).unwrap());
    assert!(
        !values_path.exists(),
        "the private values file survived command failure: {}",
        values_path.display()
    );
}

#[test]
fn cluster_secret_missing_value_fails_before_any_api_mutation() {
    let bundle = secret_bundle(&["ALPHA_TOKEN"]);
    let config = tempfile::tempdir().expect("config tempdir");
    let server = serve(|_req| Response::json(500, r#"{"detail":"must not be called"}"#));

    let output = cluster_deploy(
        &server.base_url,
        bundle.path(),
        config.path(),
        &["--agent", "acme-a", "--secret", "ALPHA_TOKEN"],
    )
    .env_remove("ALPHA_TOKEN")
    .output()
    .expect("run cluster deploy");

    assert_eq!(output.status.code(), Some(2), "{}", output_text(&output));
    assert!(
        output_text(&output).contains("ALPHA_TOKEN") && output_text(&output).contains("not set"),
        "{}",
        output_text(&output)
    );
    assert!(
        server.recorded().is_empty(),
        "missing value must fail before any API request"
    );
}

#[test]
fn cluster_secrets_reject_all_targets_before_any_api_mutation() {
    let bundle = secret_bundle(&["ALPHA_TOKEN"]);
    let config = tempfile::tempdir().expect("config tempdir");
    let server = serve(|_req| Response::json(500, r#"{"detail":"must not be called"}"#));

    let output = cluster_deploy(
        &server.base_url,
        bundle.path(),
        config.path(),
        &["--all-targets", "--secret", "ALPHA_TOKEN"],
    )
    .env("ALPHA_TOKEN", "all_targets_sentinel_440")
    .output()
    .expect("run cluster deploy");

    let text = output_text(&output);
    assert_eq!(output.status.code(), Some(2), "{text}");
    assert!(
        text.contains("--all-targets") && text.contains("--secret"),
        "{text}"
    );
    assert!(!text.contains("all_targets_sentinel_440"), "{text}");
    assert!(
        server.recorded().is_empty(),
        "unsupported target combination must fail before any API request"
    );
}

#[test]
fn cluster_secret_shrink_reads_names_then_fails_before_mutation() {
    let bundle = secret_bundle(&["ALPHA_TOKEN"]);
    let config = tempfile::tempdir().expect("config tempdir");
    let server = serve(|req| {
        if req.method == "GET" && req.path == "/agents" {
            Response::json(
                200,
                &format!("[{}]", agent_json(&["ALPHA_TOKEN", "ZETA_TOKEN"])),
            )
        } else {
            Response::json(500, r#"{"detail":"mutation must not run"}"#)
        }
    });

    let output = cluster_deploy(
        &server.base_url,
        bundle.path(),
        config.path(),
        &["--agent", "acme-a", "--secret", "ALPHA_TOKEN"],
    )
    .env("ALPHA_TOKEN", "shrink_sentinel_440")
    .output()
    .expect("run cluster deploy");

    let text = output_text(&output);
    assert_eq!(output.status.code(), Some(2), "{text}");
    assert!(
        text.contains("ZETA_TOKEN") && (text.contains("remove") || text.contains("shrink")),
        "{text}"
    );
    assert!(!text.contains("shrink_sentinel_440"), "{text}");
    let requests = server.recorded();
    assert_eq!(
        requests.len(),
        1,
        "requests were not read only: {requests:?}"
    );
    assert_eq!(requests[0].method, "GET");
    assert_eq!(requests[0].path, "/agents");
}

#[test]
fn cluster_secret_rotation_uses_names_only_private_helm_transport_and_controller_replacement() {
    const ALPHA_VALUE: &str = "alpha_cluster_sentinel_440";
    const ZETA_VALUE: &str = "zeta_cluster_sentinel_440";
    let bundle = secret_bundle(&["ZETA_TOKEN", "ALPHA_TOKEN"]);
    let config = tempfile::tempdir().expect("config tempdir");
    let capture = tempfile::tempdir().expect("capture tempdir");
    let fake_path = install_cluster_fakes(capture.path());
    let server = serve(|req| deploy_response(&req.method, &req.path));
    let chart = Path::new(env!("CARGO_MANIFEST_DIR")).join("../charts/curie");

    let output = cluster_deploy(
        &server.base_url,
        bundle.path(),
        config.path(),
        &[
            "--agent",
            "acme-a",
            "--secret",
            "ZETA_TOKEN",
            "--secret",
            "ALPHA_TOKEN",
        ],
    )
    .arg("--chart")
    .arg(&chart)
    .env("ALPHA_TOKEN", ALPHA_VALUE)
    .env("ZETA_TOKEN", ZETA_VALUE)
    .env("CAPTURE_DIR", capture.path())
    .env("PATH", fake_path)
    .output()
    .expect("run cluster deploy");

    let shown = output_text(&output);
    assert!(output.status.success(), "{shown}");
    assert!(
        !shown.contains(ALPHA_VALUE),
        "stdout or stderr leaked: {shown}"
    );
    assert!(
        !shown.contains(ZETA_VALUE),
        "stdout or stderr leaked: {shown}"
    );

    let requests = server.recorded();
    for request in &requests {
        let body = String::from_utf8_lossy(&request.body);
        assert!(!body.contains(ALPHA_VALUE), "API payload leaked: {body}");
        assert!(!body.contains(ZETA_VALUE), "API payload leaked: {body}");
    }
    let patch_index = requests
        .iter()
        .position(|request| request.method == "PATCH")
        .expect("cluster binding must PATCH the agent");
    let deployment_index = requests
        .iter()
        .position(|request| request.method == "POST" && request.path == "/deployments")
        .expect("bundle deployment must complete before binding");
    assert!(
        deployment_index < patch_index,
        "binding must happen after bundle deployment: {requests:?}"
    );
    let patch: serde_json::Value =
        serde_json::from_slice(&requests[patch_index].body).expect("PATCH JSON");
    assert_eq!(
        patch,
        serde_json::json!({"secret_names": ["ALPHA_TOKEN", "ZETA_TOKEN"]})
    );

    let helm_argv = fs::read_to_string(capture.path().join("helm-argv")).unwrap();
    assert!(helm_argv.contains("upgrade"), "helm argv was {helm_argv}");
    assert!(
        helm_argv.contains("--reuse-values"),
        "helm argv was {helm_argv}"
    );
    assert!(
        !helm_argv.contains(ALPHA_VALUE),
        "helm argv leaked: {helm_argv}"
    );
    assert!(
        !helm_argv.contains(ZETA_VALUE),
        "helm argv leaked: {helm_argv}"
    );
    assert_eq!(
        fs::read_to_string(capture.path().join("helm-values-mode"))
            .unwrap()
            .trim(),
        "600"
    );
    let values: serde_json::Value =
        serde_json::from_str(&fs::read_to_string(capture.path().join("helm-values-body")).unwrap())
            .unwrap();
    assert_eq!(
        values,
        serde_json::json!({
            "agentSandbox": {
                "connectorSecrets": {
                    "acme-a": {
                        "ALPHA_TOKEN": ALPHA_VALUE,
                        "ZETA_TOKEN": ZETA_VALUE
                    }
                }
            }
        })
    );
    let values_path =
        PathBuf::from(fs::read_to_string(capture.path().join("helm-values-path")).unwrap());
    assert!(!values_path.exists(), "values file survived deploy");

    let kubectl = fs::read_to_string(capture.path().join("kubectl-argv")).unwrap();
    assert!(
        kubectl.contains("sandboxwarmpool")
            && (kubectl.contains("wait") || kubectl.contains("readyReplicas")),
        "warm pool readiness was not observed: {kubectl}"
    );
    assert!(
        kubectl.contains("delete") && kubectl.contains("sandbox"),
        "controller Sandbox replacement did not run: {kubectl}"
    );
    assert!(
        kubectl.contains("sandboxclaim") && kubectl.contains("Ready") && kubectl.contains("wait"),
        "replacement claim readiness was not observed: {kubectl}"
    );
    assert!(
        !kubectl.contains("rollout restart") && !kubectl.contains("worker"),
        "worker restart is not credential rollout: {kubectl}"
    );
    let identity_reads: usize = fs::read_to_string(capture.path().join("identity-count"))
        .expect("old and new Sandbox identities must be observed")
        .parse()
        .unwrap();
    assert!(
        identity_reads >= 2,
        "replacement must observe a changed Sandbox identity"
    );
}

#[test]
fn cluster_helm_failure_reports_fail_closed_partial_state_and_cleans_file() {
    const SENTINEL: &str = "partial_state_sentinel_440";
    const SECOND_SENTINEL: &str = "partial_state_second_sentinel_440";
    let bundle = secret_bundle(&["ALPHA_TOKEN", "ZETA_TOKEN"]);
    let config = tempfile::tempdir().expect("config tempdir");
    let capture = tempfile::tempdir().expect("capture tempdir");
    let fake_path = install_cluster_fakes(capture.path());
    let server = serve(|req| deploy_response(&req.method, &req.path));
    let chart = Path::new(env!("CARGO_MANIFEST_DIR")).join("../charts/curie");

    let output = cluster_deploy(
        &server.base_url,
        bundle.path(),
        config.path(),
        &[
            "--agent",
            "acme-a",
            "--secret",
            "ALPHA_TOKEN",
            "--secret",
            "ZETA_TOKEN",
        ],
    )
    .arg("--chart")
    .arg(&chart)
    .env("ALPHA_TOKEN", SENTINEL)
    .env("ZETA_TOKEN", SECOND_SENTINEL)
    .env("CAPTURE_DIR", capture.path())
    .env("HELM_EXIT", "42")
    .env("PATH", fake_path)
    .output()
    .expect("run cluster deploy");

    let shown = output_text(&output);
    assert!(!output.status.success(), "helm failure must fail deploy");
    assert!(!shown.contains(SENTINEL), "failure output leaked: {shown}");
    assert!(
        !shown.contains(SECOND_SENTINEL),
        "failure output leaked: {shown}"
    );
    assert!(
        shown.contains("partial state") && shown.contains("secret names"),
        "failure must describe the fail closed binding: {shown}"
    );
    assert!(
        shown.contains("helm upgrade") && shown.contains("--reuse-values"),
        "failure must carry the exact Helm retry: {shown}"
    );

    let requests = server.recorded();
    let patch = requests
        .iter()
        .find(|request| request.method == "PATCH")
        .expect("names must be bound before Helm");
    let body: serde_json::Value = serde_json::from_slice(&patch.body).unwrap();
    assert_eq!(
        body,
        serde_json::json!({"secret_names": ["ALPHA_TOKEN", "ZETA_TOKEN"]})
    );
    assert!(!String::from_utf8_lossy(&patch.body).contains(SENTINEL));
    assert!(!String::from_utf8_lossy(&patch.body).contains(SECOND_SENTINEL));

    let helm_argv = fs::read_to_string(capture.path().join("helm-argv")).unwrap();
    assert!(
        !helm_argv.contains(SENTINEL),
        "helm argv leaked: {helm_argv}"
    );
    assert!(
        !helm_argv.contains(SECOND_SENTINEL),
        "helm argv leaked: {helm_argv}"
    );
    let values_path =
        PathBuf::from(fs::read_to_string(capture.path().join("helm-values-path")).unwrap());
    assert!(
        !values_path.exists(),
        "values file survived failed deploy: {}",
        values_path.display()
    );
}
