//! Integration: `<tier> approvals <agent> --resolve <id> --as <actor>
//! --actor-channel <chan>` must forward `actor_channel` on the wire so a
//! channel-authorized approval gate can verify membership server-side (#704).
//! Today the CLI never sends this field, so channel-authorized gates 403 when
//! resolved from the CLI even though the API's `ApprovalResolve` schema
//! accepts it.
//!
//! Driven through the real `commands::approvals` handler and a real
//! `ApiClient` against a wire-level stub, mirroring
//! `approvals_list_truncation.rs`: the regression this guards is "the CLI
//! silently drops actor_channel," which only shows up by inspecting the
//! actual outgoing POST body, not by hand-constructing an `ApprovalRecord`.
//!
//! RED CONTRACT: this file references an `actor_channel: Option<String>`
//! field on `commands::ApprovalCmd` that does not exist yet. The intended
//! shape (mirroring the existing optional `note` field/param all the way
//! through): `ApprovalCmd.actor_channel: Option<String>`, threaded into
//! `ApiClient::resolve_approval(&self, approval_id, decision, resolved_by,
//! note: Option<&str>, actor_channel: Option<&str>)`, which conditionally
//! sets `body["actor_channel"] = json!(chan)` exactly like the existing
//! `if let Some(note) = note { body["note"] = json!(note); }` branch. Until
//! the implementer adds that field and threads it through, this file fails
//! to compile (not just fails to run) -- that is the intended RED signal.

mod support;

use curie::commands::{approvals, AgentActionOpts, ApprovalCmd, ApprovalsOutput};
use support::{serve, MockServer, Response};

const TEST_API_KEY: &str = "test-key";
const APPROVAL_ID: &str = "ap_1";

fn resolved_record_json(resolved_by: &str) -> String {
    format!(
        r#"{{"id":"{APPROVAL_ID}","author":"U1","route":null,"gate_kind":null,"granted_tool":"Bash","status":"approved","conversation_id":"C1-thread-0","summary":"do the thing","expires_at":null,"resolved_by":"{resolved_by}"}}"#
    )
}

async fn resolve(server: &MockServer, cmd: ApprovalCmd) -> ApprovalsOutput {
    approvals(
        AgentActionOpts {
            api_url: server.base_url.clone(),
            api_key: TEST_API_KEY.to_string(),
            agent: "weather".to_string(),
            dry_run: false,
        },
        vec![],
        false,
        cmd,
    )
    .await
    .expect("approvals --resolve should succeed against a well-formed mock")
}

/// (a) The core regression guard: when `--actor-channel` is supplied, the
/// outgoing POST body must include it alongside the existing `decision` and
/// `resolved_by` fields -- deleting the actor_channel wiring must fail this.
#[tokio::test]
async fn actor_channel_present_in_resolve_body_when_passed() {
    let server = serve(|req| match req.path.split('?').next().unwrap() {
        p if p == format!("/approvals/{APPROVAL_ID}/resolve") => {
            Response::json(200, &resolved_record_json("brian"))
        }
        other => panic!("unexpected request: {other}"),
    });

    let out = resolve(
        &server,
        ApprovalCmd {
            resolve: Some(APPROVAL_ID.to_string()),
            as_actor: Some("brian".to_string()),
            actor_channel: Some("C123456".to_string()),
            ..ApprovalCmd::default()
        },
    )
    .await;

    match out {
        ApprovalsOutput::Resolved { record } => {
            assert_eq!(record.resolved_by.as_deref(), Some("brian"));
        }
        _ => panic!("expected a resolved record"),
    }

    let recorded = server.recorded();
    let resolve_req = recorded
        .iter()
        .find(|r| {
            r.path
                .starts_with(&format!("/approvals/{APPROVAL_ID}/resolve"))
        })
        .expect("the resolve endpoint must have been called");
    let body: serde_json::Value =
        serde_json::from_slice(&resolve_req.body).expect("resolve body must be valid JSON");

    assert_eq!(
        body["actor_channel"], "C123456",
        "expected the POST body to carry actor_channel when --actor-channel is passed, got {body:?}"
    );
    assert_eq!(body["decision"], "approved");
    assert_eq!(body["resolved_by"], "brian");
}

/// (b) The negative/secondary path (mandatory): when no `--actor-channel` is
/// given, the POST body must have NO `actor_channel` key at all -- mirrors the
/// existing conditional `note` behavior and proves the field is optional, not
/// always-sent-as-empty-string/null.
#[tokio::test]
async fn actor_channel_absent_from_resolve_body_when_not_passed() {
    let server = serve(|req| match req.path.split('?').next().unwrap() {
        p if p == format!("/approvals/{APPROVAL_ID}/resolve") => {
            Response::json(200, &resolved_record_json("brian"))
        }
        other => panic!("unexpected request: {other}"),
    });

    let out = resolve(
        &server,
        ApprovalCmd {
            resolve: Some(APPROVAL_ID.to_string()),
            as_actor: Some("brian".to_string()),
            actor_channel: None,
            ..ApprovalCmd::default()
        },
    )
    .await;

    match out {
        ApprovalsOutput::Resolved { record } => {
            assert_eq!(record.resolved_by.as_deref(), Some("brian"));
        }
        _ => panic!("expected a resolved record"),
    }

    let recorded = server.recorded();
    let resolve_req = recorded
        .iter()
        .find(|r| {
            r.path
                .starts_with(&format!("/approvals/{APPROVAL_ID}/resolve"))
        })
        .expect("the resolve endpoint must have been called");
    let body: serde_json::Value =
        serde_json::from_slice(&resolve_req.body).expect("resolve body must be valid JSON");

    assert!(
        body.get("actor_channel").is_none(),
        "expected no actor_channel key when --actor-channel is not passed, got {body:?}"
    );
    assert_eq!(body["decision"], "approved");
    assert_eq!(body["resolved_by"], "brian");
}

/// (c) `--reject` with `--actor-channel` still forwards both the rejected
/// decision and the channel -- proves actor_channel wiring isn't accidentally
/// coupled to the approve-only branch.
#[tokio::test]
async fn actor_channel_present_alongside_reject_decision() {
    let server = serve(|req| match req.path.split('?').next().unwrap() {
        p if p == format!("/approvals/{APPROVAL_ID}/resolve") => {
            Response::json(200, &resolved_record_json("brian"))
        }
        other => panic!("unexpected request: {other}"),
    });

    let _out = resolve(
        &server,
        ApprovalCmd {
            resolve: Some(APPROVAL_ID.to_string()),
            as_actor: Some("brian".to_string()),
            actor_channel: Some("C999".to_string()),
            reject: true,
            ..ApprovalCmd::default()
        },
    )
    .await;

    let recorded = server.recorded();
    let resolve_req = recorded
        .iter()
        .find(|r| {
            r.path
                .starts_with(&format!("/approvals/{APPROVAL_ID}/resolve"))
        })
        .expect("the resolve endpoint must have been called");
    let body: serde_json::Value =
        serde_json::from_slice(&resolve_req.body).expect("resolve body must be valid JSON");

    assert_eq!(body["actor_channel"], "C999");
    assert_eq!(body["decision"], "rejected");
    assert_eq!(body["resolved_by"], "brian");
}

// ─── #1078: the recipe promises this field; --list must actually emit it ─────

/// The card channel must reach the operator through `--list --json`, because
/// `--resolve` cannot be driven without it.
///
/// #1056's recipe told an agent that `--list` reports the channel a route bound
/// the card to. It did not: `approval_record_json` projected ten fields and
/// dropped `card_channel`, while the API had carried it all along. With no
/// `approvers` block on the route, that channel's MEMBERS are the approver set
/// and `slack_approvers.py` compares `actor_channel` against it, so a guess is a
/// 403 and the value was underivable from the CLI.
///
/// Asserted end to end through the real handler rather than by constructing an
/// `ApprovalRecord`: the defect was in the PROJECTION, which a hand-built record
/// walks straight past.
#[tokio::test]
async fn list_json_reports_the_card_channel_the_recipe_promises() {
    let server = serve(|req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => Response::json(
            200,
            r##"[{"id":"11111111-1111-1111-1111-111111111111","name":"weather","channels":[{"kind":"slack","address":"CREQUEST01"}],"created_at":"2026-07-05T00:00:00Z","memory":false}]"##,
        ),
        ("GET", p) if p.starts_with("/approvals") => Response::json(
            200,
            r##"[{"id":"22222222-2222-2222-2222-222222222222","agent_id":"11111111-1111-1111-1111-111111111111","author":"U-REQUESTER","route":"finance","gate_kind":"policy","granted_tool":null,"status":"pending","conversation_id":"thread-1","summary":"approve invoice","expires_at":null,"resolved_by":null,"card_channel":"CFINANCE01","reply_channel":"CREQUEST01"}]"##,
        ),
        other => panic!("unexpected request: {other:?}"),
    });

    let out = approvals(
        AgentActionOpts {
            api_url: server.base_url.clone(),
            api_key: TEST_API_KEY.to_string(),
            agent: "weather".to_string(),
            dry_run: false,
        },
        vec![],
        false,
        ApprovalCmd {
            list: true,
            ..ApprovalCmd::default()
        },
    )
    .await
    .expect("list succeeds");

    let json = curie::ui::CliOutput::to_json(&out);
    let record = &json["pending"][0];

    assert_eq!(
        record["card_channel"], "CFINANCE01",
        "--list --json must report the card channel; without it --actor-channel \
         is underivable and every resolve on a channel-authorized route 403s. \
         Payload: {json}"
    );
    // The requesting channel is a DIFFERENT channel, which is the whole reason
    // guessing fails: an agent that assumed the two were the same would send the
    // wrong one and read the refusal as an authorization problem.
    assert_ne!(
        record["card_channel"], "CREQUEST01",
        "the fixture must keep card and reply channels distinct, or this test \
         cannot tell a correct value from a lucky one"
    );
}

// ─── #1531 finding 3 (companion): the HUMAN --list render owes the same field ──
//
// `note_approval_pending` (`cli/src/message.rs:2033-2034`) tells an operator
// that `approvals <AGENT> --list` "also reports the approval's channel if its
// route binds one". On the human path that promise is false: #1078 fixed the
// `--json` projection above and left the `ApprovalsOutput::Pending` render
// printing only summary, conversation_id, tool, route and by. That render is the
// fallback for exactly the terminals that cannot look the channel up themselves
// (the arms with no parseable approval id), so it is the one surface that closes
// the loop for them.
//
// These two drive the REAL binary. `cli/src/ui.rs` exposes only
// `pub fn ui() -> &'static Ui` over a `OnceLock`, and `kv`/`payload` write
// straight to `anstream::stdout()`, so there is no in-process capture seam and
// a hand-built `ApprovalsOutput` would walk past the render under test. The
// subprocess precedent is `cli/tests/chart_check.rs:53` and
// `cli/tests/cluster_rollback.rs:638`.

/// Run `curie local approvals weather --list` against a stub API and hand back
/// its stdout. `NO_COLOR` keeps the assertions matching plain text rather than
/// ANSI-wrapped fragments.
fn list_stdout(server: &MockServer) -> String {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_curie"))
        .args(["local", "approvals", "weather", "--list"])
        .args(["--api-url", server.base_url.as_str()])
        .args(["--api-key", TEST_API_KEY])
        .env("NO_COLOR", "1")
        .output()
        .expect("run curie local approvals --list");
    let stdout = String::from_utf8(output.stdout).expect("stdout is UTF-8");
    let stderr = String::from_utf8(output.stderr).expect("stderr is UTF-8");
    assert!(
        output.status.success(),
        "--list against a well-formed stub must succeed; stdout: {stdout:?} stderr: {stderr:?}"
    );
    stdout
}

/// A stub serving the agent lookup plus one pending approval, with the given
/// `card_channel` JSON literal spliced in (`"CFINANCE01"`, `null`, or `""`).
fn pending_list_server(card_channel: &'static str) -> MockServer {
    serve(move |req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", "/agents") => Response::json(
            200,
            r##"[{"id":"11111111-1111-1111-1111-111111111111","name":"weather","channels":[{"kind":"slack","address":"CREQUEST01"}],"created_at":"2026-07-05T00:00:00Z","memory":false}]"##,
        ),
        ("GET", p) if p.starts_with("/approvals") => Response::json(
            200,
            &format!(
                r##"[{{"id":"22222222-2222-2222-2222-222222222222","agent_id":"11111111-1111-1111-1111-111111111111","author":"U-REQUESTER","route":"finance","gate_kind":"policy","granted_tool":null,"status":"pending","conversation_id":"thread-1","summary":"approve invoice","expires_at":null,"resolved_by":null,"card_channel":{card_channel},"reply_channel":"CREQUEST01"}}]"##
            ),
        ),
        other => panic!("unexpected request: {other:?}"),
    })
}

/// B-T1. The human render must name the card channel, making
/// `note_approval_pending`'s printed promise true.
///
/// The requesting channel is a DIFFERENT channel here on purpose: an operator
/// who assumed the two were the same would pass the wrong `--actor-channel` and
/// read the resulting 403 as an authorization problem rather than a typo.
///
/// Mutation it catches: deleting the `card` local or its field from the format
/// string in `ApprovalsOutput::Pending`'s render arm.
#[test]
fn list_human_render_reports_the_card_channel_the_hint_promises() {
    let server = pending_list_server(r#""CFINANCE01""#);
    let stdout = list_stdout(&server);

    assert!(
        stdout.contains("CFINANCE01"),
        "the human --list output must report the approval's card channel; \
         without it the operator has no way to derive --actor-channel from the \
         human path. stdout: {stdout:?}"
    );
}

/// B-T2 (negative). A null `card_channel` must render its real MEANING.
///
/// #1431 settled what a null means: an older row or a direct API write, so the
/// REQUESTING channel applies -- not "the record names no route". Printing
/// `null`, `none` or `-` states the wrong fact, and an operator acting on it
/// would conclude no channel is required and omit `--actor-channel` entirely.
/// `cli/tests/guide.rs:288-315` already holds this meaning on the guide surface;
/// this extends the same invariant to the list render.
///
/// `route` is a non-null `"finance"` in the fixture so the expected
/// `(requesting channel)` placeholder can only have come from `card_channel`.
///
/// Mutation it catches: `unwrap_or("null")`, `unwrap_or("none")`, or
/// `unwrap_or("-")` on the card channel.
#[test]
fn a_null_card_channel_renders_the_requesting_channel_meaning() {
    let server = pending_list_server("null");
    let stdout = list_stdout(&server);

    let record_line = stdout
        .lines()
        .find(|line| line.contains("22222222-2222-2222-2222-222222222222"))
        .unwrap_or_else(|| panic!("the pending record must be listed; stdout: {stdout:?}"))
        .to_string();

    assert!(
        record_line.contains("(requesting channel)"),
        "a null card_channel means the requesting channel applies; the render \
         must say so. line: {record_line:?}"
    );
    for wrong in ["channel: null", "channel: none", "channel: -"] {
        assert!(
            !record_line.contains(wrong),
            "rendering a null card_channel as {wrong:?} states the wrong fact \
             (#1431): it reads as 'no channel is needed'. line: {record_line:?}"
        );
    }
}

/// B-T2's pair: an EMPTY `card_channel` is the server's own spelling of absent,
/// so the list must say the requesting channel applies too.
///
/// The server selects the approver set with
/// `approval.card_channel or approval.reply_channel`
/// (`apps/api/src/curie_api/slack_approvers.py:174`), and in Python only the
/// empty string is falsy, so `""` on the wire means "fall back to the reply
/// channel" exactly as a null does. The resolve hint already mirrors that: it
/// degrades to the turn channel for this same input
/// (`the_hint_names_the_turn_channel_when_the_card_channel_is_empty` in
/// `cli/src/message.rs`). The list render keys on `None` alone
/// (`cli/src/commands.rs:5246`), so it prints `channel: ` with NOTHING after it,
/// which is both a disagreement between the two surfaces about one wire value
/// and a line that tells the operator nothing at all.
///
/// This test and `a_null_card_channel_renders_the_requesting_channel_meaning`
/// are a PAIR covering both spellings of absent. Neither covers the other:
/// `None` and `Some("")` are different values reaching different branches.
///
/// `route` is a non-null `"finance"` in the fixture so the expected
/// `(requesting channel)` placeholder can only have come from `card_channel`.
///
/// Mutation it catches: keying the placeholder on `as_deref()` alone rather than
/// also on emptiness, which is what the render does today.
#[test]
fn an_empty_card_channel_renders_the_requesting_channel_meaning() {
    let server = pending_list_server(r#""""#);
    let stdout = list_stdout(&server);

    let record_line = stdout
        .lines()
        .find(|line| line.contains("22222222-2222-2222-2222-222222222222"))
        .unwrap_or_else(|| panic!("the pending record must be listed; stdout: {stdout:?}"))
        .to_string();

    assert!(
        record_line.contains("(requesting channel)"),
        "an empty card_channel is what the server itself treats as absent, so \
         the list must say the requesting channel applies, exactly as the \
         resolve hint already degrades to the turn channel for this same \
         value. line: {record_line:?}"
    );
    // A blank field is the specific defect: `channel: ` followed straight by the
    // next field renders nothing at all where a meaning belongs. The other three
    // are the #1431 wrong-fact spellings the null case already forbids.
    for wrong in ["channel: ,", "channel: null", "channel: none", "channel: -"] {
        assert!(
            !record_line.contains(wrong),
            "rendering an empty card_channel as {wrong:?} either states the \
             wrong fact or states nothing; the operator cannot derive \
             --actor-channel from it either way. line: {record_line:?}"
        );
    }
}
