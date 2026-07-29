//! Integration: the platform API client's deploy flow against the OpenAPI
//! contract shapes (apps/api openapi.json), served by a wire-level test server.

mod support;

use curie::api::{ApiClient, ChannelOutcome};
use curie::bundle::pack_tar_gz;
use curie::scaffold::scaffold;
use support::{serve, Response};

const AGENT_ID: &str = "11111111-1111-1111-1111-111111111111";
const VERSION_ID: &str = "22222222-2222-2222-2222-222222222222";
const DEPLOYMENT_ID: &str = "33333333-3333-3333-3333-333333333333";

fn route(method: &str, path: &str) -> Response {
    match (method, path) {
        ("GET", "/agents") => Response::json(200, "[]"),
        ("POST", "/agents") => Response::json(
            201,
            &format!(
                r##"{{"id":"{AGENT_ID}","name":"deal-desk","slack_channel":"#local-dev","created_at":"2026-07-05T00:00:00Z"}}"##
            ),
        ),
        ("POST", p) if p == format!("/agents/{AGENT_ID}/versions") => Response::json(
            201,
            &format!(
                r#"{{"id":"{VERSION_ID}","agent_id":"{AGENT_ID}","version_label":"0.1.0-1","bundle_ref":null,"bundle_sha256":null,"created_by":"tester","created_at":"2026-07-05T00:00:00Z"}}"#
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
        other => panic!("unexpected request: {other:?}"),
    }
}

#[tokio::test]
async fn deploy_walks_the_full_contract_flow_with_auth() {
    let server = serve(|req| route(&req.method, &req.path));
    let client = ApiClient::new(&server.base_url, "test-key").unwrap();

    let dir = tempfile::tempdir().unwrap();
    scaffold(dir.path(), "deal-desk").unwrap();
    let archive = pack_tar_gz(dir.path()).unwrap();

    let outcome = client
        .deploy(
            "deal-desk",
            Some("#local-dev"),
            "0.1.0-1",
            "tester",
            "dev",
            archive,
            &std::collections::BTreeMap::new(),
            None,
        )
        .await
        .unwrap();

    assert_eq!(outcome.agent.id, AGENT_ID);
    assert_eq!(outcome.version.id, VERSION_ID);
    assert_eq!(outcome.bundle.bundle_sha256, "deadbeef");
    assert_eq!(outcome.deployment.id, DEPLOYMENT_ID);
    assert_eq!(outcome.deployment.environment, "dev");

    let recorded = server.recorded();
    let flow: Vec<(String, String)> = recorded
        .iter()
        .map(|r| (r.method.clone(), r.path.clone()))
        .collect();
    assert_eq!(
        flow,
        vec![
            ("GET".to_string(), "/agents".to_string()),
            ("POST".to_string(), "/agents".to_string()),
            ("POST".to_string(), format!("/agents/{AGENT_ID}/versions")),
            (
                "PUT".to_string(),
                format!("/agents/{AGENT_ID}/versions/{VERSION_ID}/bundle")
            ),
            ("POST".to_string(), "/deployments".to_string()),
        ]
    );
    for request in &recorded {
        assert_eq!(request.header("x-api-key"), Some("test-key"));
    }

    // The bundle upload is multipart with the archive under the `file` field.
    let upload = &recorded[3];
    assert!(upload
        .header("content-type")
        .unwrap()
        .starts_with("multipart/form-data"));
    let body = String::from_utf8_lossy(&upload.body);
    assert!(body.contains("name=\"file\""));
    assert!(body.contains("filename=\"bundle.tar.gz\""));
}

#[tokio::test]
async fn reuses_an_existing_agent_instead_of_creating() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => Response::json(
            200,
            &format!(
                r##"[{{"id":"{AGENT_ID}","name":"deal-desk","slack_channel":"#x","created_at":"2026-07-05T00:00:00Z"}}]"##
            ),
        ),
        other => panic!("unexpected request: {other:?}"),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    let agent = client
        .find_or_create_agent("deal-desk", "#local-dev")
        .await
        .unwrap();
    assert_eq!(agent.id, AGENT_ID);
    assert_eq!(server.recorded().len(), 1);
}

/// The version/bundle/deployment tail of the deploy flow, shared by the
/// channel-reconciliation tests (which differ only in the agent-resolution head).
fn deploy_tail(method: &str, path: &str) -> Option<Response> {
    match (method, path) {
        ("POST", p) if p == format!("/agents/{AGENT_ID}/versions") => Some(Response::json(
            201,
            &format!(
                r#"{{"id":"{VERSION_ID}","agent_id":"{AGENT_ID}","version_label":"0.1.0-1","bundle_ref":null,"bundle_sha256":null,"created_by":"tester","created_at":"2026-07-05T00:00:00Z"}}"#
            ),
        )),
        ("PUT", p) if p == format!("/agents/{AGENT_ID}/versions/{VERSION_ID}/bundle") => {
            Some(Response::json(
                201,
                &format!(
                    r#"{{"version_id":"{VERSION_ID}","bundle_ref":"bundles/x.tar.gz","bundle_sha256":"deadbeef","size_bytes":512}}"#
                ),
            ))
        }
        ("POST", "/deployments") => Some(Response::json(
            201,
            &format!(
                r#"{{"id":"{DEPLOYMENT_ID}","agent_id":"{AGENT_ID}","version_id":"{VERSION_ID}","environment":"dev","status":"active","deployed_at":"2026-07-05T00:00:00Z"}}"#
            ),
        )),
        _ => None,
    }
}

fn existing_agent(channel: &str) -> Response {
    Response::json(
        200,
        &format!(
            r#"[{{"id":"{AGENT_ID}","name":"deal-desk","slack_channel":"{channel}","created_at":"2026-07-05T00:00:00Z"}}]"#
        ),
    )
}

async fn run_deploy(client: &ApiClient, channel: Option<&str>) -> ChannelOutcome {
    let dir = tempfile::tempdir().unwrap();
    scaffold(dir.path(), "deal-desk").unwrap();
    let archive = pack_tar_gz(dir.path()).unwrap();
    client
        .deploy(
            "deal-desk",
            channel,
            "0.1.0-1",
            "tester",
            "dev",
            archive,
            &std::collections::BTreeMap::new(),
            None,
        )
        .await
        .unwrap()
        .channel
}

#[tokio::test]
async fn redeploy_with_explicit_channel_patches_the_existing_agent() {
    // An existing agent on #old + `--slack-channel #new` must PATCH the agent to
    // move the channel (the audit MAJOR: the channel was silently ignored).
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => existing_agent("#old"),
        ("PATCH", p) if *p == format!("/agents/{AGENT_ID}") => Response::json(
            200,
            &format!(
                r##"{{"id":"{AGENT_ID}","name":"deal-desk","slack_channel":"#new","created_at":"2026-07-05T00:00:00Z"}}"##
            ),
        ),
        (m, p) => deploy_tail(m, p).unwrap_or_else(|| panic!("unexpected request: {m} {p}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let outcome = run_deploy(&client, Some("#new")).await;
    assert_eq!(
        outcome,
        ChannelOutcome::Updated {
            from: "#old".to_string(),
            to: "#new".to_string(),
        }
    );

    let patches: Vec<_> = server
        .recorded()
        .into_iter()
        .filter(|r| r.method == "PATCH" && r.path == format!("/agents/{AGENT_ID}"))
        .collect();
    assert_eq!(patches.len(), 1, "expected exactly one PATCH");
    let body = String::from_utf8_lossy(&patches[0].body);
    assert!(body.contains("#new"), "PATCH body was {body}");
}

#[tokio::test]
async fn redeploy_without_channel_does_not_patch() {
    // Omitting `--slack-channel` on a redeploy must leave the agent's channel
    // untouched: no PATCH is issued at all.
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => existing_agent("#old"),
        ("PATCH", _) => panic!("redeploy without --slack-channel must not PATCH"),
        (m, p) => deploy_tail(m, p).unwrap_or_else(|| panic!("unexpected request: {m} {p}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let outcome = run_deploy(&client, None).await;
    assert_eq!(
        outcome,
        ChannelOutcome::Unchanged {
            channel: "#old".to_string(),
            passed: false,
        }
    );
    assert!(
        server.recorded().iter().all(|r| r.method != "PATCH"),
        "no PATCH should have been issued"
    );
}

#[tokio::test]
async fn surfaces_api_errors_with_status_and_body() {
    let server = serve(|_req| Response::json(401, r#"{"detail":"invalid API key"}"#));
    let client = ApiClient::new(&server.base_url, "wrong").unwrap();
    let err = client.list_agents().await.unwrap_err();
    let text = err.to_string();
    assert!(text.contains("401"), "unexpected error: {text}");
    assert!(text.contains("invalid API key"), "unexpected error: {text}");
}
