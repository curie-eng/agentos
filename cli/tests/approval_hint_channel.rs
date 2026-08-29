//! Integration: the pre-wait resolve hint must name the channel the approval's
//! card was actually posted to, and only `GET /approvals/{approval_id}` can
//! answer that (#1531 finding 3).
//!
//! Today `approval_resolve_command` is handed the channel THIS TURN routed to
//! (`--channel`, or the sole agent's bound channel). When a route binding put
//! the card somewhere else, the printed `--actor-channel` names the wrong
//! channel and the copy-pasted command 403s with "resolve this from the
//! approval's channel". The correct value is `ApprovalRecord.card_channel`,
//! which the API has always returned and the CLI has always parsed; nothing
//! ever fetched it for a single approval, because the only approvals client
//! method is the truncatable `list_pending_approvals`.
//!
//! Driven through a real `ApiClient` against the wire-level stub rather than by
//! hand-building an `ApprovalRecord`: the defect class here is the REQUEST (the
//! wrong path, a missing API key, a list scan instead of a single-record read),
//! which a constructed record walks straight past.
//!
//! RED CONTRACT: this file calls a method that does not exist yet, so it fails
//! to COMPILE rather than merely failing to run -- that is the intended RED
//! signal, and it is the same idiom this file's sibling uses at
//! `approvals_resolve_actor_channel.rs:14-24`. The intended shapes, which the
//! implementer should treat as the target:
//!
//! - `pub async fn get_approval(&self, approval_id: &str) -> Result<ApprovalRecord>`
//!   on `ApiClient` in `cli/src/api.rs`, mirroring `list_pending_approvals`
//!   exactly: `send_request(self.http.get(format!("{}/approvals/{approval_id}",
//!   self.base_url)).header("X-API-Key", &self.api_key), "GET /approvals/{id}")`
//!   then `expect_ok(resp, "reading an approval")` and `.json()`.
//! - `async fn hint_channel(opts: &MessageOpts, verb: TurnVerb,
//!   turn_channel: &str, id: &str) -> String`, PRIVATE in `cli/src/message.rs`.
//!   Its own tests live in that file's `#[cfg(test)]` block, because an
//!   integration test cannot reach a private item and making it `pub` would
//!   widen the crate's public API for a test.
//! - `const HINT_CHANNEL_LOOKUP_BUDGET: Duration = Duration::from_secs(10);`,
//!   also PRIVATE in `cli/src/message.rs`: the single bound wrapped around the
//!   whole lookup, port-forward startup included. Named rather than inlined so
//!   the stalling-peer test in that file's `#[cfg(test)]` block tracks the
//!   budget if it is ever retuned.

mod support;

use curie::api::ApiClient;
use support::{serve, MockServer, Response};

const TEST_API_KEY: &str = "test-key";
/// A real UUID: both `parse_approval_id` (`cli/src/chat.rs:400`) and the API
/// route (`approvals.py`, path param typed `uuid.UUID`) parse this as one, so a
/// placeholder like `ap_1` could never reach the endpoint under test.
const APPROVAL_ID: &str = "22222222-2222-2222-2222-222222222222";

/// One pending approval whose card landed in a channel OTHER than the one the
/// requester spoke in. The two channels must stay distinct, per the sibling
/// test's own comment: with a single channel the assertion cannot tell a
/// correct value from a lucky one.
fn route_bound_record() -> String {
    format!(
        r#"{{"id":"{APPROVAL_ID}","agent_id":"11111111-1111-1111-1111-111111111111","author":"U-REQUESTER","route":"finance","gate_kind":"policy","granted_tool":null,"status":"pending","conversation_id":"thread-1","summary":"approve invoice","expires_at":null,"resolved_by":null,"card_channel":"CFINANCE01","reply_channel":"CREQUEST01"}}"#
    )
}

fn single_approval_server(status: u16, body: String) -> MockServer {
    serve(move |req| match (req.method.as_str(), req.path.as_str()) {
        ("GET", p) if p == format!("/approvals/{APPROVAL_ID}") => {
            Response::json(status, body.as_str())
        }
        other => panic!("unexpected request: {other:?}"),
    })
}

/// A-T1. The read the hint is built on: the single-record endpoint, carrying
/// the API key, returning the card channel.
///
/// The list endpoint is deliberately NOT an acceptable substitute and this test
/// is what pins that: `list_pending_approvals` caps at the server max and
/// signals truncation (#670), so the approval being waited on can simply not be
/// in the page. The hint must read the one record by id.
///
/// Mutation it catches: wiring the lookup to `/approvals?agent_id=...` instead
/// of `/approvals/{id}`, or dropping the `X-API-Key` header (which the API
/// answers 401 to, and an advisory caller would silently absorb into the wrong
/// channel).
#[tokio::test]
async fn get_approval_reads_the_card_channel_the_hint_needs() {
    let server = single_approval_server(200, route_bound_record());
    let api = ApiClient::new(&server.base_url, TEST_API_KEY).expect("build client");

    let record = api
        .get_approval(APPROVAL_ID)
        .await
        .expect("a 200 with a well-formed record must decode");

    assert_eq!(
        record.card_channel.as_deref(),
        Some("CFINANCE01"),
        "the card channel is the value `--actor-channel` must carry; without it \
         the printed hint names the requesting channel and the resolve 403s"
    );
    assert_ne!(
        record.card_channel.as_deref(),
        Some("CREQUEST01"),
        "the fixture must keep the card and reply channels distinct, or this \
         test cannot tell a correct value from a lucky one"
    );

    let recorded = server.recorded();
    let request = recorded
        .iter()
        .find(|r| r.method == "GET")
        .expect("the client must have issued a GET");
    assert_eq!(
        request.path,
        format!("/approvals/{APPROVAL_ID}"),
        "the hint must read the single record by id, not scan the truncatable \
         pending list; recorded path: {}",
        request.path
    );
    assert_eq!(
        request.header("X-API-Key"),
        Some(TEST_API_KEY),
        "every platform API call authenticates; a missing key is a 401 the \
         advisory caller would absorb into a silently wrong channel"
    );
}

/// A-T2. A null `card_channel` must PARSE, not error.
///
/// It is a real wire value: a row predating route bindings, or a direct API
/// write that omitted the field. `#[serde(default)]` on the field already makes
/// this work; the test exists so a future tightening (removing the default, or
/// adding `deny_unknown_fields`-style strictness) turns an older row into a
/// visible failure here instead of a decode error inside an advisory lookup
/// that swallows every error it sees.
///
/// Mutation it catches: making `card_channel` non-optional on `ApprovalRecord`.
#[tokio::test]
async fn a_null_card_channel_yields_no_override() {
    let body = format!(
        r#"{{"id":"{APPROVAL_ID}","agent_id":"11111111-1111-1111-1111-111111111111","author":"U-REQUESTER","route":"finance","gate_kind":"policy","granted_tool":null,"status":"pending","conversation_id":"thread-1","summary":"approve invoice","expires_at":null,"resolved_by":null,"card_channel":null,"reply_channel":"CREQUEST01"}}"#
    );
    let server = single_approval_server(200, body);
    let api = ApiClient::new(&server.base_url, TEST_API_KEY).expect("build client");

    let record = api
        .get_approval(APPROVAL_ID)
        .await
        .expect("a null card_channel is a valid record, not a decode failure");

    assert!(
        record.card_channel.is_none(),
        "a null card_channel means 'an older row, so the requesting channel \
         applies' (#1431), which the caller expresses by falling back to the \
         turn channel; got {:?}",
        record.card_channel
    );
}

/// A-T3. The client method PROPAGATES a 404; absorbing it is the caller's job.
///
/// A 404 here is real rather than theoretical: another operator can resolve or
/// expire the approval between the pending notice and the hint. The advisory
/// wrapper in `message.rs` is where that becomes "print the turn channel"; if
/// `get_approval` absorbed it instead, a future non-advisory caller would read
/// a missing approval as a present one.
///
/// The second assertion pins the boundary `expect_ok`'s `is_unrouted` branch
/// draws: FastAPI answers an UNROUTED path with exactly `{"detail":"Not Found"}`
/// and the CLI turns that into "this platform release is older than this CLI".
/// A handler that ran and found nothing sets its own detail, and telling that
/// operator to upgrade their release would be a fabricated diagnosis.
///
/// Mutation it catches: implementing the absorb inside `get_approval` (returning
/// `Ok(None)` or a default record), or letting the not-found detail fall into
/// the unrouted branch.
#[tokio::test]
async fn a_404_is_absorbed_not_propagated() {
    let server = single_approval_server(404, r#"{"detail":"approval not found"}"#.to_string());
    let api = ApiClient::new(&server.base_url, TEST_API_KEY).expect("build client");

    let err = api
        .get_approval(APPROVAL_ID)
        .await
        .expect_err("a 404 must reach the caller as an Err, not be swallowed here")
        .to_string();

    assert!(
        !err.contains("older than this CLI"),
        "a genuine approval-not-found must not be misreported as an unrouted \
         endpoint; that sends the operator to upgrade a release that is fine. \
         Error: {err}"
    );
    assert!(
        !err.contains("Upgrade the release"),
        "same misdiagnosis, remedy half. Error: {err}"
    );
}
