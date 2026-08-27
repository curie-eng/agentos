//! Integration: `curie <tier> memory --add` posts through the memory surface
//! (`POST /agents/{id}/memory`, #1904), not the reserved state-append path.

mod support;

use curie::api::ApiClient;
use curie::commands::{self, AgentActionOpts, MemoryOutput};
use curie::ui::CliOutput;
use support::{serve, Response};

const AGENT_ID: &str = "11111111-1111-1111-1111-111111111111";

fn agent_list() -> Response {
    Response::json(
        200,
        &format!(
            r##"[{{"id":"{AGENT_ID}","name":"translation-bot","channel":{{"kind":"slack","address":"#x"}},"created_at":"2026-07-05T00:00:00Z"}}]"##
        ),
    )
}

fn created_entry() -> Response {
    Response::json(
        201,
        r#"{
            "index": 0,
            "content": "ask before translating to French",
            "provenance": {
                "learned_from_session_id": null,
                "source_trace_ids": [],
                "recorded_at": "2026-08-27T00:00:00+00:00",
                "source": "operator"
            },
            "version": 1
        }"#,
    )
}

fn opts(base_url: &str, dry_run: bool) -> AgentActionOpts {
    AgentActionOpts {
        api_url: base_url.to_string(),
        api_key: "k".to_string(),
        agent: "translation-bot".to_string(),
        dry_run,
    }
}

#[tokio::test]
async fn create_memory_posts_content_to_the_memory_surface() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("POST", p) if *p == format!("/agents/{AGENT_ID}/memory") => created_entry(),
        other => panic!("unexpected request: {other:?}"),
    });
    let client = ApiClient::new(&server.base_url, "k").unwrap();
    let entry = client
        .create_memory(AGENT_ID, "ask before translating to French")
        .await
        .unwrap();
    assert_eq!(entry.index, 0);
    assert_eq!(entry.content, "ask before translating to French");

    let rec = server.recorded();
    assert_eq!(rec.len(), 1);
    assert_eq!(rec[0].method, "POST");
    assert_eq!(rec[0].path, format!("/agents/{AGENT_ID}/memory"));
    let body = String::from_utf8_lossy(&rec[0].body);
    assert!(
        body.contains("\"content\":\"ask before translating to French\""),
        "body: {body}"
    );
    assert!(
        !body.contains("provenance"),
        "CLI must not send caller-supplied provenance: {body}"
    );
    assert_eq!(rec[0].header("x-api-key"), Some("k"));
}

#[tokio::test]
async fn memory_add_handler_resolves_by_name_then_posts() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => agent_list(),
        ("POST", p) if *p == format!("/agents/{AGENT_ID}/memory") => created_entry(),
        other => panic!("unexpected request: {other:?}"),
    });
    let output = commands::memory_add(
        opts(&server.base_url, false),
        "ask before translating to French".to_string(),
    )
    .await
    .unwrap();
    match output {
        MemoryOutput::Added {
            agent,
            index,
            content,
            source,
            fresh_session_required,
        } => {
            assert_eq!(agent, "translation-bot");
            assert_eq!(index, 0);
            assert_eq!(content, "ask before translating to French");
            assert_eq!(source, "operator");
            assert!(fresh_session_required);
        }
        other => panic!("expected Added, got {other:?}"),
    }

    let flow: Vec<(String, String)> = server
        .recorded()
        .iter()
        .map(|r| (r.method.clone(), r.path.clone()))
        .collect();
    assert_eq!(
        flow,
        vec![
            ("GET".to_string(), "/agents".to_string()),
            ("POST".to_string(), format!("/agents/{AGENT_ID}/memory")),
        ]
    );
}

#[tokio::test]
async fn memory_add_dry_run_makes_no_request() {
    let server = serve(|req| panic!("dry-run must not request: {req:?}"));
    let output = commands::memory_add(
        opts(&server.base_url, true),
        "ask before translating to French".to_string(),
    )
    .await
    .unwrap();
    match output {
        MemoryOutput::DryRun(plan) => {
            let joined = plan.lines.join("\n");
            assert!(
                joined.contains("POST"),
                "dry-run plan must name the POST: {joined}"
            );
            assert!(
                joined.contains("/memory"),
                "dry-run plan must name the memory surface: {joined}"
            );
            assert!(
                !joined.contains("/state/"),
                "dry-run must not advertise the reserved state-append path: {joined}"
            );
        }
        _ => panic!("expected DryRun"),
    }
    assert!(server.recorded().is_empty());
}

#[tokio::test]
async fn memory_add_rejects_blank_content_without_calling_the_api() {
    let server = serve(|req| panic!("blank content must not request: {req:?}"));
    let err = commands::memory_add(opts(&server.base_url, false), "  \n".to_string())
        .await
        .unwrap_err();
    let message = err.to_string();
    assert!(
        message.to_lowercase().contains("content"),
        "error should name content: {message}"
    );
    assert!(server.recorded().is_empty());
}

#[test]
fn memory_add_json_documents_a_fresh_session() {
    let json = MemoryOutput::Added {
        agent: "translation-bot".to_string(),
        index: 0,
        content: "ask first".to_string(),
        source: "operator".to_string(),
        fresh_session_required: true,
    }
    .to_json();
    assert_eq!(
        json,
        serde_json::json!({
            "agent": "translation-bot",
            "index": 0,
            "content": "ask first",
            "source": "operator",
            "fresh_session_required": true,
        })
    );
}
