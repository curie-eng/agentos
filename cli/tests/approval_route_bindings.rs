//! Integration: `<tier> approvals <agent>` writes and reads the agent's
//! approval ROUTE bindings (#1052).
//!
//! Until this verb existed, binding a route to a channel was a hand-written
//! `PATCH /agents/{id}`, against the repo's own "one entry point: `curie
//! <command>`" rule, and neither the CLI nor the console could do it.
//!
//! Driven through the real `commands::approvals` handler and a real `ApiClient`
//! against a wire-level stub, mirroring `approvals_resolve_actor_channel.rs`.
//! The regressions worth catching only show up in the actual outgoing PATCH
//! body, or in the ABSENCE of one:
//!
//! - a write must send the whole map (the field replaces, it does not merge);
//! - `channel` and `approvers` must stay separate on the wire, since ADR-0034
//!   exists to keep WHERE a card posts unfused from WHO may resolve it;
//! - and a malformed input must send NOTHING. A half-written binding map is a
//!   silently widened or narrowed approver set, which is exactly the failure the
//!   approval gate exists to prevent, so validation runs before the connection
//!   is opened and the negative tests assert on `recorded()` being empty.

mod support;

use curie::commands::{approvals, AgentActionOpts, ApprovalCmd, ApprovalsOutput};
use support::{serve, MockServer, Response};

const TEST_API_KEY: &str = "test-key";
const AGENT_ID: &str = "ag_1";

/// `GET /agents` payload: the handler resolves the agent by name first.
fn agents_list(routes_json: &str) -> String {
    format!(
        r#"[{{"id":"{AGENT_ID}","name":"deal-desk","slack_channel":"C0INTAKE00","approval_required_tools":null,"approval_routes":{routes_json}}}]"#
    )
}

/// `PATCH /agents/{id}` echo: the updated agent the CLI renders from.
fn patched_agent(routes_json: &str) -> String {
    format!(
        r#"{{"id":"{AGENT_ID}","name":"deal-desk","slack_channel":"C0INTAKE00","approval_required_tools":null,"approval_routes":{routes_json}}}"#
    )
}

/// A stub answering the two endpoints this verb touches.
fn stub(list_routes: &'static str, patch_echo: &'static str) -> MockServer {
    serve(move |req| match req.path.split('?').next().unwrap() {
        "/agents" => Response::json(200, &agents_list(list_routes)),
        p if p == format!("/agents/{AGENT_ID}") => Response::json(200, &patched_agent(patch_echo)),
        other => panic!("unexpected request: {other}"),
    })
}

async fn run(server: &MockServer, cmd: ApprovalCmd) -> anyhow::Result<ApprovalsOutput> {
    approvals(
        AgentActionOpts {
            api_url: server.base_url.clone(),
            api_key: TEST_API_KEY.to_string(),
            agent: "deal-desk".to_string(),
            dry_run: false,
        },
        vec![],
        false,
        cmd,
    )
    .await
}

fn patch_body(server: &MockServer) -> serde_json::Value {
    let recorded = server.recorded();
    let patch = recorded
        .iter()
        .find(|r| r.path.starts_with(&format!("/agents/{AGENT_ID}")))
        .expect("the PATCH endpoint must have been called");
    serde_json::from_slice(&patch.body).expect("PATCH body must be valid JSON")
}

/// No PATCH reached the server. The assertion for every rejected input: the
/// point is not only that the command failed, but that it failed before writing.
fn assert_no_write(server: &MockServer) {
    let wrote = server
        .recorded()
        .iter()
        .any(|r| r.path.starts_with(&format!("/agents/{AGENT_ID}")));
    assert!(
        !wrote,
        "a rejected invocation must send no PATCH; recorded: {:?}",
        server
            .recorded()
            .iter()
            .map(|r| r.path.clone())
            .collect::<Vec<_>>()
    );
}

#[tokio::test]
async fn route_flag_writes_the_channel_binding() {
    let bound = r#"{"deal_desk":{"channel":"C0MANAGERS"}}"#;
    let server = stub("null", bound);

    let out = run(
        &server,
        ApprovalCmd {
            route: vec!["deal_desk=C0MANAGERS".to_string()],
            ..ApprovalCmd::default()
        },
    )
    .await
    .expect("a well-formed --route should succeed");

    let body = patch_body(&server);
    assert_eq!(
        body["approval_routes"]["deal_desk"]["channel"], "C0MANAGERS",
        "the PATCH must carry the binding, got {body:?}"
    );
    // No approvers block was asked for, so none may be sent: an explicit null is
    // a different statement from an omitted key against a model that forbids
    // extras, and it would read as "an approvers block that declares nobody".
    assert!(
        body["approval_routes"]["deal_desk"]
            .get("approvers")
            .is_none(),
        "a channel-only write must send no approvers key, got {body:?}"
    );

    match out {
        ApprovalsOutput::Routes { routes, .. } => {
            assert_eq!(routes["deal_desk"].channel, "C0MANAGERS");
            assert!(routes["deal_desk"].approvers.is_none());
        }
        _ => panic!("expected the Routes output"),
    }
}

#[tokio::test]
async fn route_approvers_narrows_who_without_moving_where() {
    // The ADR-0034 property, asserted on the wire: `channel` and `approvers` are
    // independent axes, so narrowing WHO must leave WHERE untouched and must not
    // collapse the two into one field.
    let bound = r#"{"finance":{"channel":"C0FINANCE0","approvers":{"group":"S0FINGRP0"}}}"#;
    let server = stub("null", bound);

    let out = run(
        &server,
        ApprovalCmd {
            route: vec!["finance=C0FINANCE0".to_string()],
            route_approvers: vec!["finance=group:S0FINGRP0".to_string()],
            ..ApprovalCmd::default()
        },
    )
    .await
    .expect("a well-formed group binding should succeed");

    let body = patch_body(&server);
    assert_eq!(body["approval_routes"]["finance"]["channel"], "C0FINANCE0");
    assert_eq!(
        body["approval_routes"]["finance"]["approvers"]["group"],
        "S0FINGRP0"
    );
    assert!(
        body["approval_routes"]["finance"]["approvers"]
            .get("users")
            .is_none(),
        "a group binding must not also send a users key, got {body:?}"
    );

    match out {
        ApprovalsOutput::Routes { routes, .. } => {
            let binding = &routes["finance"];
            assert_eq!(binding.channel, "C0FINANCE0");
            assert_eq!(
                binding.approvers.as_ref().and_then(|a| a.group.as_deref()),
                Some("S0FINGRP0")
            );
        }
        _ => panic!("expected the Routes output"),
    }
}

#[tokio::test]
async fn route_approvers_users_forwards_the_whole_list() {
    let bound =
        r#"{"cro_cfo":{"channel":"C0EXEC0000","approvers":{"users":["U0CRO00000","W0CFO00000"]}}}"#;
    let server = stub("null", bound);

    run(
        &server,
        ApprovalCmd {
            route: vec!["cro_cfo=C0EXEC0000".to_string()],
            // A W-prefixed id is a valid enterprise-grid user, so the shape check
            // must accept it rather than assuming every user id starts with U.
            route_approvers: vec!["cro_cfo=users:U0CRO00000, W0CFO00000".to_string()],
            ..ApprovalCmd::default()
        },
    )
    .await
    .expect("a well-formed user-list binding should succeed");

    let body = patch_body(&server);
    assert_eq!(
        body["approval_routes"]["cro_cfo"]["approvers"]["users"],
        serde_json::json!(["U0CRO00000", "W0CFO00000"]),
        "both user ids must reach the wire, whitespace trimmed, got {body:?}"
    );
}

#[tokio::test]
async fn list_routes_reads_without_writing() {
    let bound = r#"{"deal_desk":{"channel":"C0MANAGERS"}}"#;
    let server = stub(bound, bound);

    let out = run(
        &server,
        ApprovalCmd {
            list_routes: true,
            ..ApprovalCmd::default()
        },
    )
    .await
    .expect("--list-routes should succeed");

    assert_no_write(&server);
    match out {
        ApprovalsOutput::Routes { agent, routes } => {
            assert_eq!(agent, "deal-desk");
            assert_eq!(routes["deal_desk"].channel, "C0MANAGERS");
        }
        _ => panic!("expected the Routes output"),
    }
}

#[tokio::test]
async fn clear_routes_sends_null_not_an_empty_object() {
    // `crud.update_agent_approval_routes` stores `routes or None`, so null is how
    // the API spells "no bindings". Sending `{}` would be a second spelling of the
    // same state and would round-trip back as null anyway.
    let server = stub(r#"{"deal_desk":{"channel":"C0MANAGERS"}}"#, "null");

    run(
        &server,
        ApprovalCmd {
            clear_routes: true,
            ..ApprovalCmd::default()
        },
    )
    .await
    .expect("--clear-routes should succeed");

    let body = patch_body(&server);
    assert!(
        body["approval_routes"].is_null(),
        "clear must send an explicit null, got {body:?}"
    );
}

#[tokio::test]
async fn a_malformed_channel_writes_nothing() {
    let server = stub("null", "null");

    let Err(err) = run(
        &server,
        ApprovalCmd {
            // The second route is well-formed; the first is not. Neither may be
            // written, or the resulting map is one an operator never asked for.
            route: vec![
                "deal_desk=#managers".to_string(),
                "finance=C0FINANCE0".to_string(),
            ],
            ..ApprovalCmd::default()
        },
    )
    .await
    else {
        panic!("a #name channel must be refused");
    };

    assert!(
        err.to_string().contains("not a Slack channel ID"),
        "unexpected error: {err}"
    );
    assert_no_write(&server);
}

#[tokio::test]
async fn a_channel_id_where_a_usergroup_belongs_writes_nothing() {
    let server = stub("null", "null");

    let Err(err) = run(
        &server,
        ApprovalCmd {
            route: vec!["finance=C0FINANCE0".to_string()],
            route_approvers: vec!["finance=group:C0FINANCE0".to_string()],
            ..ApprovalCmd::default()
        },
    )
    .await
    else {
        panic!("a C-prefixed id is a channel, not a user group");
    };

    assert!(
        err.to_string().contains("not a Slack user-group ID"),
        "unexpected error: {err}"
    );
    assert_no_write(&server);
}

#[tokio::test]
async fn approvers_without_a_channel_writes_nothing() {
    // A write replaces the whole map, so narrowing a route the invocation never
    // gives a channel would produce a binding with nowhere to post.
    let server = stub("null", "null");

    let Err(err) = run(
        &server,
        ApprovalCmd {
            route_approvers: vec!["finance=group:S0FINGRP0".to_string()],
            ..ApprovalCmd::default()
        },
    )
    .await
    else {
        panic!("approvers with no channel must be refused");
    };

    assert!(
        err.to_string().contains("no channel"),
        "unexpected error: {err}"
    );
    assert_no_write(&server);
}

#[tokio::test]
async fn route_flags_cannot_be_mixed_with_gate_or_record_flags() {
    // Three different objects behind one verb (tool gates, pending records, route
    // bindings). Mixing them in one invocation makes the write's
    // replace-the-whole-map semantics ambiguous, so it is refused, not guessed.
    let server = stub("null", "null");

    let Err(err) = run(
        &server,
        ApprovalCmd {
            route: vec!["finance=C0FINANCE0".to_string()],
            list: true,
            ..ApprovalCmd::default()
        },
    )
    .await
    else {
        panic!("route flags and --list address different objects");
    };

    assert!(
        err.to_string().contains("cannot be combined"),
        "unexpected error: {err}"
    );
    assert_no_write(&server);
}

#[tokio::test]
async fn routes_from_seeds_the_map_and_flags_override_it() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("routes.json");
    std::fs::write(
        &path,
        r#"{"deal_desk":{"channel":"C0OLDROOM0"},"finance":{"channel":"C0FINANCE0","approvers":{"group":"S0FINGRP0"}}}"#,
    )
    .expect("write routes file");

    let bound = r#"{"deal_desk":{"channel":"C0MANAGERS"},"finance":{"channel":"C0FINANCE0","approvers":{"group":"S0FINGRP0"}}}"#;
    let server = stub("null", bound);

    run(
        &server,
        ApprovalCmd {
            routes_from: Some(path),
            route: vec!["deal_desk=C0MANAGERS".to_string()],
            ..ApprovalCmd::default()
        },
    )
    .await
    .expect("a committed file plus a command-line override should succeed");

    let body = patch_body(&server);
    assert_eq!(
        body["approval_routes"]["deal_desk"]["channel"], "C0MANAGERS",
        "the flag must win over the file's value, got {body:?}"
    );
    assert_eq!(
        body["approval_routes"]["finance"]["approvers"]["group"], "S0FINGRP0",
        "a route only the file names must survive, got {body:?}"
    );
}

#[tokio::test]
async fn a_malformed_routes_file_writes_nothing() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("routes.json");
    // Valid JSON, invalid binding: the file path must be validated with the same
    // rules the flag path uses, or the two input forms disagree about what a
    // legal binding is.
    std::fs::write(&path, r#"{"finance":{"channel":"finance-room"}}"#).expect("write");

    let server = stub("null", "null");

    let Err(err) = run(
        &server,
        ApprovalCmd {
            routes_from: Some(path),
            ..ApprovalCmd::default()
        },
    )
    .await
    else {
        panic!("a bad channel in the file must be refused");
    };

    assert!(
        err.to_string().contains("not a Slack channel ID"),
        "unexpected error: {err}"
    );
    assert_no_write(&server);
}
