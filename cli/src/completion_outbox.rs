//! Read-only completion-outbox diagnosis for status and doctor (#2422).
//!
//! The worker owns the Valkey keys and the snapshot. The CLI executes that
//! reader inside a running worker and accepts only the closed JSON shape.
//! Run and session identifiers never enter the report.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::ops::{plain, run_capture, OpsCommand};

const OBSERVATION_TIMEOUT: Duration = Duration::from_secs(10);

const STATUS_ARGS: [&str; 4] = ["python", "-m", "curie_worker.completion_health", "--json"];

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum State {
    Empty,
    Inflight,
    Retry,
    Unknown,
}

#[derive(Debug, Clone, Serialize)]
pub struct Report {
    pub count: u64,
    pub oldest_age_s: f64,
    pub inflight: u64,
    pub retry: u64,
    pub terminal: u64,
    pub state: State,
    pub degraded: bool,
    pub detail: String,
}

impl Report {
    pub fn unknown() -> Self {
        Self {
            count: 0,
            oldest_age_s: 0.0,
            inflight: 0,
            retry: 0,
            terminal: 0,
            state: State::Unknown,
            degraded: false,
            detail: "completion outbox: unknown; worker status could not be read".to_string(),
        }
    }

    pub fn known(&self) -> bool {
        self.state != State::Unknown
    }

    pub fn healthy(&self) -> bool {
        self.known() && !self.degraded
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StatusDocument {
    count: u64,
    oldest_age_s: f64,
    inflight: u64,
    retry: u64,
    terminal: u64,
    state: String,
    degraded: bool,
}

fn describe(document: &StatusDocument) -> String {
    format!(
        "completion outbox: state {}; count {}; inflight {}; retry {}; terminal {}; oldest age {:.0}s",
        document.state, document.count, document.inflight, document.retry, document.terminal,
        document.oldest_age_s
    )
}

fn parse_status(stdout: &str) -> Report {
    let Ok(document) = serde_json::from_str::<StatusDocument>(stdout) else {
        return Report::unknown();
    };
    if document.oldest_age_s.is_nan() || document.oldest_age_s.is_sign_negative() {
        return Report::unknown();
    }
    let state = match document.state.as_str() {
        "empty" => State::Empty,
        "inflight" => State::Inflight,
        "retry" => State::Retry,
        _ => return Report::unknown(),
    };
    let degraded = document.degraded || document.retry > 0;
    if (state == State::Retry) != degraded {
        return Report::unknown();
    }
    Report {
        count: document.count,
        oldest_age_s: document.oldest_age_s,
        inflight: document.inflight,
        retry: document.retry,
        terminal: document.terminal,
        state,
        degraded,
        detail: describe(&document),
    }
}

fn running_worker_pod(stdout: &str, release: &str) -> Option<String> {
    let document = serde_json::from_str::<Value>(stdout).ok()?;
    let items = document.get("items")?.as_array()?;
    let mut matches: Vec<String> = items
        .iter()
        .filter_map(|pod| {
            let metadata = pod.get("metadata")?;
            let labels = metadata.get("labels")?.as_object()?;
            let correct_release = labels
                .get("app.kubernetes.io/instance")
                .and_then(Value::as_str)
                == Some(release);
            let worker_component = labels
                .get("app.kubernetes.io/component")
                .and_then(Value::as_str)
                == Some("worker");
            let running = pod.pointer("/status/phase").and_then(Value::as_str) == Some("Running");
            let terminating = metadata
                .get("deletionTimestamp")
                .is_some_and(|timestamp| !timestamp.is_null());
            if !correct_release || !worker_component || !running || terminating {
                return None;
            }
            metadata
                .get("name")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|name| !name.is_empty())
                .map(str::to_string)
        })
        .collect();
    matches.sort_unstable();
    matches.into_iter().next()
}

fn pods_command(namespace: &str) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("get"),
            plain("pods"),
            plain("-n"),
            plain(namespace),
            plain("-o"),
            plain("json"),
            plain("--request-timeout=5s"),
        ],
    )
}

fn exec_command(namespace: &str, pod: &str) -> OpsCommand {
    let mut args = vec![
        plain("exec"),
        plain("-n"),
        plain(namespace),
        plain(pod),
        plain("--"),
    ];
    args.extend(STATUS_ARGS.into_iter().map(plain));
    OpsCommand::new("kubectl", args)
}

async fn capture(command: &OpsCommand) -> Option<String> {
    let (ok, out, _) = tokio::time::timeout(OBSERVATION_TIMEOUT, run_capture(command))
        .await
        .ok()?
        .ok()?;
    ok.then_some(out)
}

pub async fn observe_exec(namespace: &str, pod: &str) -> Report {
    let Some(raw) = capture(&exec_command(namespace, pod)).await else {
        return Report::unknown();
    };
    parse_status(&raw)
}

pub async fn observe_pods(namespace: &str, release: &str, pods: &[Value]) -> Option<Report> {
    let wrapped = serde_json::json!({ "items": pods });
    let pod = running_worker_pod(&wrapped.to_string(), release)?;
    Some(observe_exec(namespace, &pod).await)
}

pub async fn observe(namespace: &str, release: &str) -> Report {
    let Some(raw) = capture(&pods_command(namespace)).await else {
        return Report::unknown();
    };
    let Ok(value) = serde_json::from_str::<Value>(&raw) else {
        return Report::unknown();
    };
    let Some(items) = value.get("items").and_then(Value::as_array) else {
        return Report::unknown();
    };
    match observe_pods(namespace, release, items).await {
        Some(report) => report,
        None => Report::unknown(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(serde::Deserialize)]
    #[serde(deny_unknown_fields)]
    struct CompletionOutboxHealthVector {
        #[serde(rename = "comment")]
        _comment: String,
        default_key_prefix: String,
        pending_key_suffix: String,
        completion_key_infix: String,
        default_grace_s: f64,
        dl_source: String,
        status_module: String,
        status_args: Vec<String>,
        states: Vec<String>,
        metric_names: Vec<String>,
        metric_outcomes: Vec<String>,
    }

    #[test]
    fn completion_outbox_health_matches_the_frozen_vector() {
        let raw = include_str!("../../tests/vectors/completion-outbox-health.json");
        let parsed: CompletionOutboxHealthVector =
            serde_json::from_str(raw).unwrap_or_else(|err| {
                panic!(
                    "parse tests/vectors/completion-outbox-health.json: {err}\n\
                 An unknown field is rejected on purpose: a key this lane cannot see \
                 would pass vacuously. Teach the new key to CompletionOutboxHealthVector \
                 here and to _EXPECTED_KEYS in the worker vector test."
                )
            });
        assert_eq!(parsed.status_args, STATUS_ARGS);
        assert_eq!(parsed.status_module, "curie_worker.completion_health");
        assert_eq!(parsed.default_key_prefix, "curie:worker");
        assert_eq!(parsed.pending_key_suffix, "completions:pending");
        assert_eq!(parsed.completion_key_infix, "completion:");
        assert_eq!(parsed.default_grace_s, 60.0);
        assert_eq!(parsed.dl_source, "completion-outbox");
        assert_eq!(parsed.states, ["empty", "inflight", "retry", "unknown"]);
        assert_eq!(
            parsed.metric_names,
            ["curie.completion.outbox", "curie.completion.outbox.age"]
        );
        assert_eq!(parsed.metric_outcomes, ["inflight", "retry", "terminal"]);
    }

    #[test]
    fn parser_rejects_identifiers_and_unknown_state() {
        let report = parse_status(
            r#"{"count":1,"oldest_age_s":9.0,"inflight":0,"retry":1,"terminal":0,"state":"retry","degraded":true,"event_id":"secret"}"#,
        );
        assert_eq!(report.state, State::Unknown);
        let retry = parse_status(
            r#"{"count":1,"oldest_age_s":90.0,"inflight":0,"retry":1,"terminal":0,"state":"retry","degraded":true}"#,
        );
        assert!(retry.degraded);
        assert!(retry.detail.contains("retry"));
        assert!(!retry.detail.contains("secret"));
        let empty = parse_status(
            r#"{"count":0,"oldest_age_s":0.0,"inflight":0,"retry":0,"terminal":0,"state":"empty","degraded":false}"#,
        );
        assert!(empty.healthy());
        let inflight = parse_status(
            r#"{"count":1,"oldest_age_s":5.0,"inflight":1,"retry":0,"terminal":0,"state":"inflight","degraded":false}"#,
        );
        assert!(inflight.healthy());
        assert!(!inflight.degraded);
    }
}
