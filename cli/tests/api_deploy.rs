//! Integration: the platform API client's deploy flow against the OpenAPI
//! contract shapes (apps/api openapi.json), served by a wire-level test server.

mod support;

use curie::api::{ApiClient, ChannelOutcome, DeployOutcome};
use curie::bundle::pack_tar_gz;
use curie::scaffold::scaffold;
use support::{serve, MockServer, Response};

const AGENT_ID: &str = "11111111-1111-1111-1111-111111111111";
const AGENT_NAME: &str = "deal-desk";
const VERSION_ID: &str = "22222222-2222-2222-2222-222222222222";
const DEPLOYMENT_ID: &str = "33333333-3333-3333-3333-333333333333";

#[derive(Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct RepoFullNameVectors {
    comment: String,
    valid: Vec<ValidRepoFullNameVector>,
    invalid: Vec<InvalidRepoFullNameVector>,
}

#[derive(Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ValidRepoFullNameVector {
    name: String,
    value: String,
    why: String,
    url_path: String,
}

#[derive(Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct InvalidRepoFullNameVector {
    name: String,
    value: String,
    why: String,
}

fn repo_full_name_vectors() -> RepoFullNameVectors {
    let vectors: RepoFullNameVectors =
        serde_json::from_str(include_str!("../../tests/vectors/repo-full-name.json"))
            .expect("parse tests/vectors/repo-full-name.json");
    assert!(
        !vectors.comment.is_empty(),
        "the shared repository name contract must explain its purpose"
    );
    vectors
}

fn route(method: &str, path: &str) -> Response {
    match (method, path) {
        ("GET", "/agents") => Response::json(200, "[]"),
        ("POST", "/agents") => Response::json(
            201,
            &format!(
                r##"{{"id":"{AGENT_ID}","name":"deal-desk","channel":{{"kind":"slack","address":"#local-dev"}},"created_at":"2026-07-05T00:00:00Z"}}"##
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
                r##"[{{"id":"{AGENT_ID}","name":"deal-desk","channel":{{"kind":"slack","address":"#x"}},"created_at":"2026-07-05T00:00:00Z"}}]"##
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

/// One agent's wire JSON. `repo` emits the `repo_full_name` key only when the
/// agent is bound, so an unbound agent travels as an ABSENT key and exercises
/// the field's real `#[serde(default)]` path rather than an explicit null.
fn agent_json(id: &str, name: &str, channel: &str, repo: Option<&str>) -> String {
    let bound = match repo {
        Some(repo) => format!(r#","repo_full_name":"{repo}""#),
        None => String::new(),
    };
    format!(
        r#"{{"id":"{id}","name":"{name}","channel":{{"kind":"slack","address":"{channel}"}},"created_at":"2026-07-05T00:00:00Z"{bound}}}"#
    )
}

/// The `GET /agents` listing that resolution reads: the one agent under test,
/// as the platform would report it.
fn existing_agents(agent: &str) -> Response {
    Response::json(200, &format!("[{agent}]"))
}

/// The `PATCH /agents/{id}` response: the agent as the API stored it. The
/// deploy must report THIS row, never a locally patched copy of the listed one,
/// or the CLI can claim a binding the API never took.
fn patched_agent(channel: &str, repo: Option<&str>) -> Response {
    Response::json(200, &agent_json(AGENT_ID, AGENT_NAME, channel, repo))
}

/// Every recorded `PATCH /agents/{id}` body, parsed. The body on the wire is
/// the contract under test: what the CLI SENT, not what it decided internally.
fn patch_bodies(server: &MockServer) -> Vec<serde_json::Value> {
    server
        .recorded()
        .into_iter()
        .filter(|r| r.method == "PATCH" && r.path == format!("/agents/{AGENT_ID}"))
        .map(|r| serde_json::from_slice(&r.body).expect("PATCH body should be JSON"))
        .collect()
}

/// Assert that the deploy issued no `PATCH /agents/{id}` at all.
///
/// This assertion is only load-bearing because the no-PATCH tests ANSWER an
/// unexpected PATCH instead of panicking on it. The mock records a request
/// only AFTER its handler returns (`cli/tests/support/mod.rs`), so a handler
/// that panics on a PATCH means the PATCH is never recorded: the check then
/// runs over a list that could not contain the thing it looks for and passes
/// no matter what the CLI did. Such a test goes red only through the socket
/// error the unwound handler thread causes, which is red for the wrong reason
/// and is equally red for unrelated breakage. Answering keeps the request in
/// the recording, so "the CLI sent a PATCH it must not send" is what fails,
/// and the offending body is the failure message.
fn assert_no_patch(server: &MockServer) {
    let patches: Vec<String> = server
        .recorded()
        .iter()
        .filter(|r| r.method == "PATCH")
        .map(|r| format!("{} {}", r.path, String::from_utf8_lossy(&r.body)))
        .collect();
    assert!(
        patches.is_empty(),
        "no PATCH should have been issued, got {patches:?}"
    );
}

async fn run_deploy(
    client: &ApiClient,
    channel: Option<&str>,
    repo: Option<&str>,
) -> DeployOutcome {
    run_deploy_result(client, channel, repo).await.unwrap()
}

async fn run_deploy_result(
    client: &ApiClient,
    channel: Option<&str>,
    repo: Option<&str>,
) -> anyhow::Result<DeployOutcome> {
    let dir = tempfile::tempdir().unwrap();
    scaffold(dir.path(), AGENT_NAME).unwrap();
    let archive = pack_tar_gz(dir.path()).unwrap();
    client
        .deploy(
            AGENT_NAME,
            channel,
            "0.1.0-1",
            "tester",
            "dev",
            archive,
            &std::collections::BTreeMap::new(),
            repo,
        )
        .await
}

#[tokio::test]
async fn invalid_repo_full_names_fail_before_any_http_request() {
    let server = serve(|_req| Response::json(500, r#"{"detail":"request escaped preflight"}"#));
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    for case in repo_full_name_vectors().invalid {
        let err = match run_deploy_result(&client, None, Some(&case.value)).await {
            Ok(_) => panic!("{} must be rejected: {}", case.name, case.why),
            Err(err) => err,
        };
        let requests = server.recorded();
        assert!(
            requests.is_empty(),
            "{} reached HTTP before validation with {:?}: {requests:?}",
            case.name,
            case.value
        );

        let message = format!("{err:#}");
        assert!(
            (message.contains("repo_full_name") || message.contains("--repo"))
                && message.contains("owner/name"),
            "{} returned an unactionable error for {:?}: {message}",
            case.name,
            case.value
        );
    }
}

#[tokio::test]
async fn valid_repo_full_names_preserve_create_behavior() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => Response::json(200, "[]"),
        ("POST", "/agents") => {
            let body: serde_json::Value =
                serde_json::from_slice(&req.body).expect("POST /agents body should be JSON");
            let repo = body["repo_full_name"]
                .as_str()
                .expect("POST /agents should carry repo_full_name");
            Response::json(201, &agent_json(AGENT_ID, AGENT_NAME, "#old", Some(repo)))
        }
        (method, path) => deploy_tail(method, path)
            .unwrap_or_else(|| panic!("unexpected request: {method} {path}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    let vectors = repo_full_name_vectors();

    for case in &vectors.valid {
        let outcome = run_deploy(&client, None, Some(&case.value)).await;
        assert_eq!(
            outcome.agent.repo_full_name.as_deref(),
            Some(case.value.as_str()),
            "{} must preserve {:?}: {} (URL path {:?})",
            case.name,
            case.value,
            case.why,
            case.url_path
        );
    }

    let creates: Vec<serde_json::Value> = server
        .recorded()
        .into_iter()
        .filter(|request| request.method == "POST" && request.path == "/agents")
        .map(|request| {
            serde_json::from_slice(&request.body).expect("POST /agents body should be JSON")
        })
        .collect();
    assert_eq!(creates.len(), vectors.valid.len());
    for (body, case) in creates.iter().zip(&vectors.valid) {
        assert_eq!(body["repo_full_name"].as_str(), Some(case.value.as_str()));
    }
}

#[tokio::test]
async fn valid_repo_full_names_preserve_bind_behavior() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => existing_agents(&agent_json(AGENT_ID, AGENT_NAME, "#old", None)),
        ("PATCH", path) if *path == format!("/agents/{AGENT_ID}") => {
            let body: serde_json::Value =
                serde_json::from_slice(&req.body).expect("PATCH /agents/{id} body should be JSON");
            let repo = body["repo_full_name"]
                .as_str()
                .expect("PATCH /agents/{id} should carry repo_full_name");
            patched_agent("#old", Some(repo))
        }
        (method, path) => deploy_tail(method, path)
            .unwrap_or_else(|| panic!("unexpected request: {method} {path}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    let vectors = repo_full_name_vectors();

    for case in &vectors.valid {
        let outcome = run_deploy(&client, None, Some(&case.value)).await;
        assert_eq!(
            outcome.agent.repo_full_name.as_deref(),
            Some(case.value.as_str()),
            "{} must preserve {:?}: {} (URL path {:?})",
            case.name,
            case.value,
            case.why,
            case.url_path
        );
    }

    let patches = patch_bodies(&server);
    assert_eq!(patches.len(), vectors.valid.len());
    for (body, case) in patches.iter().zip(&vectors.valid) {
        assert_eq!(body["repo_full_name"].as_str(), Some(case.value.as_str()));
        assert!(
            body.get("channel").is_none(),
            "{} must bind without changing the channel: {body}",
            case.name
        );
    }
}

#[tokio::test]
async fn redeploy_with_explicit_channel_patches_the_existing_agent() {
    // An existing agent on #old + `--slack-channel #new` must PATCH the agent to
    // move the channel (the audit MAJOR: the channel was silently ignored).
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => existing_agents(&agent_json(AGENT_ID, AGENT_NAME, "#old", None)),
        ("PATCH", p) if *p == format!("/agents/{AGENT_ID}") => patched_agent("#new", None),
        (m, p) => deploy_tail(m, p).unwrap_or_else(|| panic!("unexpected request: {m} {p}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let outcome = run_deploy(&client, Some("#new"), None).await;
    assert_eq!(
        outcome.channel,
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
        ("GET", "/agents") => existing_agents(&agent_json(AGENT_ID, AGENT_NAME, "#old", None)),
        // Answered, never panicked, so the PATCH would be RECORDED and
        // `assert_no_patch` is what fails. See its doc comment.
        ("PATCH", p) if *p == format!("/agents/{AGENT_ID}") => patched_agent("#old", None),
        (m, p) => deploy_tail(m, p).unwrap_or_else(|| panic!("unexpected request: {m} {p}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let outcome = run_deploy(&client, None, None).await;
    assert_eq!(
        outcome.channel,
        ChannelOutcome::Unchanged {
            channel: "#old".to_string(),
            passed: false,
        }
    );
    assert_no_patch(&server);
}

#[tokio::test]
async fn deploy_binds_an_unbound_agents_repo() {
    // An agent that already exists with NO repo binding is bound by this
    // deploy, not told to recreate itself: `AgentUpdate` has carried
    // `repo_full_name` since ADR-0091 / #1194, and until #1212 the CLI kept
    // behaving as though it did not.
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => existing_agents(&agent_json(AGENT_ID, AGENT_NAME, "#old", None)),
        ("PATCH", p) if *p == format!("/agents/{AGENT_ID}") => {
            patched_agent("#old", Some("acme/bundle"))
        }
        (m, p) => deploy_tail(m, p).unwrap_or_else(|| panic!("unexpected request: {m} {p}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let outcome = run_deploy(&client, None, Some("acme/bundle")).await;

    let patches = patch_bodies(&server);
    assert_eq!(
        patches.len(),
        1,
        "expected exactly one PATCH, got {patches:?}"
    );
    assert_eq!(patches[0]["repo_full_name"], "acme/bundle");
    // The binding travels as one object under `channel` ({kind, address}).
    assert!(
        patches[0].get("channel").is_none(),
        "no channel was passed, so the PATCH must not carry one: {}",
        patches[0]
    );
    assert!(
        outcome.repo_note.is_none(),
        "a binding that was applied must not warn: {:?}",
        outcome.repo_note
    );
    // Read back from the PATCH response, so the CLI cannot report a binding
    // the API never stored.
    assert_eq!(outcome.agent.repo_full_name.as_deref(), Some("acme/bundle"));
}

#[tokio::test]
async fn deploy_binds_the_repo_while_also_moving_the_channel() {
    // The channel move and the repo bind travel in ONE PATCH. The old code
    // returned early out of the channel-updated branch, so a fix applied only
    // to the channel-unchanged branch would move the channel and silently drop
    // the binding on exactly this path.
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => existing_agents(&agent_json(AGENT_ID, AGENT_NAME, "#old", None)),
        ("PATCH", p) if *p == format!("/agents/{AGENT_ID}") => {
            patched_agent("#new", Some("acme/bundle"))
        }
        (m, p) => deploy_tail(m, p).unwrap_or_else(|| panic!("unexpected request: {m} {p}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let outcome = run_deploy(&client, Some("#new"), Some("acme/bundle")).await;

    let patches = patch_bodies(&server);
    assert_eq!(
        patches.len(),
        1,
        "one PATCH carries both changes: {patches:?}"
    );
    // The binding travels as one object under `channel`; both sub-fields are
    // asserted so a bare string cannot pass as a channel move.
    assert_eq!(patches[0]["channel"]["kind"], "slack");
    assert_eq!(patches[0]["channel"]["address"], "#new");
    assert_eq!(patches[0]["repo_full_name"], "acme/bundle");
    assert_eq!(
        outcome.channel,
        ChannelOutcome::Updated {
            from: "#old".to_string(),
            to: "#new".to_string(),
        }
    );
    assert_eq!(outcome.agent.repo_full_name.as_deref(), Some("acme/bundle"));
    assert!(
        outcome.repo_note.is_none(),
        "a binding that was applied must not warn: {:?}",
        outcome.repo_note
    );
}

#[tokio::test]
async fn deploy_warns_when_the_platform_drops_the_repo_binding() {
    // A platform older than `AgentUpdate.repo_full_name` (#1194) answers this
    // PATCH 200 with the unknown key IGNORED: the agent comes back still
    // unbound. `AgentUpdate` declares no `extra="forbid"`, so there is no 4xx
    // and no unrouted-path 404 for the skew detector to key on -- the only
    // evidence is the row that came back. Reporting a clean success here is
    // exactly the failure #1064 exists to prevent: the operator believes the
    // binding took, and git-flow never routes a push.
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => existing_agents(&agent_json(AGENT_ID, AGENT_NAME, "#old", None)),
        ("PATCH", p) if *p == format!("/agents/{AGENT_ID}") => patched_agent("#old", None),
        (m, p) => deploy_tail(m, p).unwrap_or_else(|| panic!("unexpected request: {m} {p}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let outcome = run_deploy(&client, None, Some("acme/bundle")).await;

    // The CLI did its half: the key went out on the wire.
    let patches = patch_bodies(&server);
    assert_eq!(
        patches.len(),
        1,
        "expected exactly one PATCH, got {patches:?}"
    );
    assert_eq!(patches[0]["repo_full_name"], "acme/bundle");
    // And it reports the row the API returned, not the one it asked for.
    assert_eq!(outcome.agent.repo_full_name, None);
    let note = outcome
        .repo_note
        .expect("a binding the platform did not store must warn");
    assert!(note.contains("acme/bundle"), "note was: {note}");
    // The load-bearing half of this test is the `expect` above (no warning at
    // all is the defect); these two pin that the note is about the PLATFORM
    // dropping it, not about a declined rebind.
    assert!(note.contains("platform"), "note was: {note}");
    assert!(
        !note.contains("already bound"),
        "this is version skew, not a declined rebind: {note}"
    );
}

#[tokio::test]
async fn deploy_does_not_rebind_an_agent_bound_elsewhere() {
    // Moving a live binding reroutes which repository's pushes deploy the
    // agent, which is ADR-0091's whole threat model. A routine deploy declines
    // and says so instead.
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => existing_agents(&agent_json(
            AGENT_ID,
            AGENT_NAME,
            "#old",
            Some("other/repo"),
        )),
        // Answered, never panicked, so a rebinding PATCH would be RECORDED and
        // `assert_no_patch` is what fails. See its doc comment.
        ("PATCH", p) if *p == format!("/agents/{AGENT_ID}") => {
            patched_agent("#old", Some("other/repo"))
        }
        (m, p) => deploy_tail(m, p).unwrap_or_else(|| panic!("unexpected request: {m} {p}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let outcome = run_deploy(&client, None, Some("acme/bundle")).await;

    assert_no_patch(&server);
    let note = outcome.repo_note.expect("a declined --repo must warn");
    assert!(note.contains("other/repo"), "note was: {note}");
    assert!(note.contains("acme/bundle"), "note was: {note}");
    // The binding CAN be changed now; a deploy just refuses to be the thing
    // that changes it. The old wording sent operators off to recreate the
    // agent, which is the false claim #1212 exists to retire.
    assert!(!note.contains("cannot be changed"), "note was: {note}");
    assert!(!note.contains("recreate"), "note was: {note}");
}

#[tokio::test]
async fn deploy_with_a_matching_repo_does_not_patch() {
    // Already bound to exactly what was asked for. A no-op PATCH would add a
    // write to every routine redeploy and buy nothing.
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => existing_agents(&agent_json(
            AGENT_ID,
            AGENT_NAME,
            "#old",
            Some("acme/bundle"),
        )),
        // Answered, never panicked, so a no-op PATCH would be RECORDED and
        // `assert_no_patch` is what fails. See its doc comment.
        ("PATCH", p) if *p == format!("/agents/{AGENT_ID}") => {
            patched_agent("#old", Some("acme/bundle"))
        }
        (m, p) => deploy_tail(m, p).unwrap_or_else(|| panic!("unexpected request: {m} {p}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let outcome = run_deploy(&client, None, Some("acme/bundle")).await;

    assert_no_patch(&server);
    assert!(
        outcome.repo_note.is_none(),
        "nothing was declined, so nothing to warn about: {:?}",
        outcome.repo_note
    );
}

#[tokio::test]
async fn deploy_without_repo_never_sends_the_field() {
    // Omission is the wire spelling for "leave the binding alone". An explicit
    // null would read as omitted at the router today, but absence is the
    // contract we actually want on the wire (#1071).
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => existing_agents(&agent_json(
            AGENT_ID,
            AGENT_NAME,
            "#old",
            Some("acme/bundle"),
        )),
        ("PATCH", p) if *p == format!("/agents/{AGENT_ID}") => {
            patched_agent("#new", Some("acme/bundle"))
        }
        (m, p) => deploy_tail(m, p).unwrap_or_else(|| panic!("unexpected request: {m} {p}")),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let outcome = run_deploy(&client, Some("#new"), None).await;

    let patches = patch_bodies(&server);
    assert_eq!(
        patches.len(),
        1,
        "expected exactly one PATCH, got {patches:?}"
    );
    // The binding travels as one object under `channel`; both sub-fields are
    // asserted so a bare string cannot pass as a channel move.
    assert_eq!(patches[0]["channel"]["kind"], "slack");
    assert_eq!(patches[0]["channel"]["address"], "#new");
    assert!(
        patches[0].get("repo_full_name").is_none(),
        "the key must be ABSENT, not null: {}",
        patches[0]
    );
    assert!(
        outcome.repo_note.is_none(),
        "no --repo was passed, so nothing to warn about: {:?}",
        outcome.repo_note
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

// --------------------------------------------------------------------------- #
// The advisory git-flow routing check (#1221)
//
// Binding a SECOND agent to a repository is legal since migration 0018
// (ADR-0091) and silently stops every push for the agent that was already
// bound. The CLI asks the API about it AFTER the deploy has landed, so the
// check must be incapable of turning a successful deploy into a failure: every
// answer that is not a decodable success is `Ok(None)`, which prints nothing.
// --------------------------------------------------------------------------- #

const UNROUTABLE: &str = r#"{
  "repo_full_name": "octo/shared-repo",
  "agent_count": 2,
  "agents": ["acme-bot", "acme-dev"],
  "resolvable": false,
  "unresolvable": [
    {"environment": "dev", "code": "deploy.no_targets", "message": "2 agents are built from this repository but the bundle has no deploy.yaml, so there is nothing to say which one this branch deploys to. Declare a target (ADR-0089)."},
    {"environment": "prod", "code": "deploy.no_targets", "message": "2 agents are built from this repository but the bundle has no deploy.yaml, so there is nothing to say which one this branch deploys to. Declare a target (ADR-0089)."}
  ]
}"#;

#[tokio::test]
async fn routing_check_decodes_an_unresolvable_repository() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("POST", "/git-flow/routing-check") => Response::json(200, UNROUTABLE),
        other => panic!("unexpected request: {other:?}"),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();

    let check = client
        .check_git_flow_routing("octo/shared-repo", None)
        .await
        .unwrap()
        .expect("a successful body must decode");

    assert!(!check.resolvable);
    assert_eq!(check.agent_count, 2);
    assert_eq!(check.agents, vec!["acme-bot", "acme-dev"]);
    // The resolver's problems survive the hop intact: the CLI prints its
    // `message` verbatim rather than restating the rule (#1212).
    assert_eq!(check.unresolvable.len(), 2);
    assert_eq!(check.unresolvable[0].environment, "dev");
    assert_eq!(check.unresolvable[0].code, "deploy.no_targets");
    assert!(check.unresolvable[0].message.contains("Declare a target"));

    // The repository and the bundle's deploy.yaml text both travel to the API,
    // which owns the parser and the resolver (ADR-0089).
    let body: serde_json::Value = serde_json::from_slice(&server.recorded()[0].body).unwrap();
    assert_eq!(body["repo_full_name"], "octo/shared-repo");
    assert!(body["content"].is_null());
}

#[tokio::test]
async fn routing_check_is_silent_against_a_platform_that_predates_it() {
    // FastAPI's bare not-found body: the CLI is newer than the platform. That
    // operator is not doing anything wrong, and an advisory check must not tell
    // them their deploy failed.
    let server = serve(|_req| Response::json(404, r#"{"detail":"Not Found"}"#));
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    assert!(client
        .check_git_flow_routing("octo/shared-repo", Some("targets: {}\n"))
        .await
        .unwrap()
        .is_none());
}

#[tokio::test]
async fn routing_check_never_fails_a_deploy_on_a_server_error() {
    // The deploy already succeeded by the time this runs; a 500 here says
    // nothing about it, so it degrades to "no answer" rather than an error.
    let server = serve(|_req| Response::json(500, r#"{"detail":"boom"}"#));
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    assert!(client
        .check_git_flow_routing("octo/shared-repo", None)
        .await
        .unwrap()
        .is_none());
}

#[tokio::test]
async fn routing_check_swallows_a_body_it_cannot_decode() {
    // Platform skew can answer 200 with a shape this CLI does not model. A
    // decode failure is still "no answer", never a failed deploy.
    let server = serve(|_req| Response::json(200, r#"["not", "an", "object"]"#));
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    assert!(client
        .check_git_flow_routing("octo/shared-repo", None)
        .await
        .unwrap()
        .is_none());
}
