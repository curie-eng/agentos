//! A minimal Slack Web API client for the connected-transport `message` path
//! (#770/ADR-0078).
//!
//! `local`/`cluster message` normally mint a throwaway in-process reply stub.
//! When a real workspace is connected, we instead post a real placeholder
//! message to the channel over the workspace bot token and enqueue the turn
//! against its real `ts`, so the worker edits that message in place and the
//! approval card threads under it -- the card and the resumed reply ride the
//! connected transport with no stub. This module owns just that one outbound
//! call.

use anyhow::{Context, Result};

/// The Slack Web API base. `SLACK_API_BASE_URL` overrides it (a test/stub base,
/// the same env the worker's own sink honours), else real Slack.
fn api_base() -> String {
    std::env::var("SLACK_API_BASE_URL")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "https://slack.com/api".to_string())
}

/// Post `text` to `channel` as the bot, returning the created message `ts` (the
/// placeholder the worker then edits in place). `thread_ts` posts the placeholder
/// INTO that thread; `None` posts it at the top level, where the created message's
/// own `ts` is the thread root. The token is sent only in the Authorization
/// header; it is never logged.
pub async fn post_placeholder(
    bot_token: &str,
    channel: &str,
    text: &str,
    thread_ts: Option<&str>,
) -> Result<String> {
    let base = api_base();
    let resp = reqwest::Client::new()
        .post(format!("{base}/chat.postMessage"))
        .bearer_auth(bot_token)
        .json(&post_body(channel, text, thread_ts))
        .send()
        .await
        .context("posting the approval-turn placeholder to Slack")?
        .json::<serde_json::Value>()
        .await
        .context("decoding the Slack chat.postMessage response")?;
    parse_ts(&resp)
}

/// The `chat.postMessage` request body. `thread_ts` is an OPTIONAL Slack
/// parameter: sending it threads the new message under that parent, omitting it
/// posts at the top level. It must be OMITTED rather than sent as null when we
/// have no parent -- a null would be a malformed value, not "no thread". Pure, so
/// both branches are unit tested without a network round trip.
fn post_body(channel: &str, text: &str, thread_ts: Option<&str>) -> serde_json::Value {
    let mut body = serde_json::Map::new();
    body.insert("channel".into(), channel.into());
    body.insert("text".into(), text.into());
    if let Some(thread_ts) = thread_ts {
        body.insert("thread_ts".into(), thread_ts.into());
    }
    serde_json::Value::Object(body)
}

/// Extract the created message `ts` from a `chat.postMessage` response, turning
/// an `{"ok": false, "error": ...}` body into an actionable error. Pure, so it
/// is unit tested without a network round trip.
fn parse_ts(resp: &serde_json::Value) -> Result<String> {
    if resp.get("ok").and_then(serde_json::Value::as_bool) != Some(true) {
        let err = resp
            .get("error")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("unknown error");
        anyhow::bail!("Slack chat.postMessage failed: {err}");
    }
    resp.get("ts")
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
        .context("Slack chat.postMessage returned ok but no message ts")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parse_ts_returns_the_ts_on_ok() {
        assert_eq!(
            parse_ts(&json!({"ok": true, "ts": "1717171717.001900"})).unwrap(),
            "1717171717.001900"
        );
    }

    #[test]
    fn parse_ts_surfaces_the_slack_error_on_not_ok() {
        let err = parse_ts(&json!({"ok": false, "error": "channel_not_found"})).unwrap_err();
        assert!(err.to_string().contains("channel_not_found"), "{err}");
    }

    #[test]
    fn parse_ts_errors_when_ok_but_no_ts() {
        assert!(parse_ts(&json!({"ok": true})).is_err());
    }

    #[test]
    fn post_body_threads_the_placeholder_when_a_parent_is_given() {
        // Observed behavior of the real dispatcher against real Slack
        // (apps/dispatcher/src/curie_dispatcher/handlers.py:90-98,113): it passes
        // `thread_ts=` to chat.postMessage so the placeholder lands IN the thread,
        // and enqueues that same value as the turn's conversation_id.
        let body = post_body("C-real", "\u{2026}", Some("1717171717.000100"));
        assert_eq!(body["channel"], "C-real");
        assert_eq!(body["thread_ts"], "1717171717.000100");
    }

    #[test]
    fn post_body_omits_the_thread_key_entirely_at_top_level() {
        // Same source: a root message carries no thread_ts at all, and its OWN ts
        // is the thread root. The key must be absent, not null -- Slack would
        // reject a null as a malformed thread_ts.
        let body = post_body("C-real", "\u{2026}", None);
        assert_eq!(body["channel"], "C-real");
        assert!(
            body.get("thread_ts").is_none(),
            "thread_ts must be omitted, not null: {body}"
        );
    }

    #[test]
    fn api_base_defaults_to_real_slack_and_honours_the_override() {
        // No override -> real Slack. (Set/removed in-process; keep the assertions
        // tolerant of a pre-set env by asserting the shape, not a fixed value.)
        std::env::remove_var("SLACK_API_BASE_URL");
        assert_eq!(api_base(), "https://slack.com/api");
        std::env::set_var("SLACK_API_BASE_URL", "http://stub:9/api");
        assert_eq!(api_base(), "http://stub:9/api");
        std::env::remove_var("SLACK_API_BASE_URL");
    }
}
