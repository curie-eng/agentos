//! Shared read-only mail credential diagnosis for status and doctor.
//! Read the running adapter through the pod proxy, which remains reachable when
//! readiness removes that pod from Service endpoints. No Secret value is read.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::ops::{plain, run_capture, OpsCommand};

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TokenState {
    Ok,
    Expiring,
    Expired,
    Rejected,
    Missing,
    Invalid,
    Disabled,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Token {
    pub present: bool,
    pub exp: Option<i64>,
    pub state: TokenState,
}

#[derive(Deserialize)]
struct Status {
    channel_token: Token,
    last_ingress_status: Option<u16>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Report {
    pub pod: String,
    pub channel_token: Option<Token>,
    pub last_ingress_status: Option<u16>,
    pub detail: String,
    pub fix: Option<String>,
}

impl Report {
    /// Whether the adapter actually answered with a token state.
    ///
    /// `unavailable` is a report about the READ, not about the release: the
    /// proxy was unreachable, the adapter predates `/statusz`, the pod is
    /// restarting, the body did not parse. Callers that turn a report into a
    /// verdict must separate that from a token we read and found bad --
    /// otherwise an admitted "we could not tell" becomes a failure claim about
    /// a release that is running fine.
    pub fn known(&self) -> bool {
        self.channel_token.is_some()
    }

    pub fn healthy(&self) -> bool {
        self.channel_token.as_ref().is_some_and(|token| {
            matches!(
                token.state,
                TokenState::Ok | TokenState::Expiring | TokenState::Disabled
            )
        })
    }
}

fn expiry(exp: Option<i64>) -> String {
    exp.and_then(|value| time::OffsetDateTime::from_unix_timestamp(value).ok())
        .and_then(|value| {
            value
                .format(&time::format_description::well_known::Rfc3339)
                .ok()
        })
        .unwrap_or_else(|| "unknown".to_string())
}

fn recovery(namespace: &str, release: &str) -> String {
    format!(
        r#"curie cluster channel-token <agent> --kind email --address <inbox> --namespace {namespace} --release {release}"#
    )
}

fn describe(token: &Token, last: Option<u16>) -> String {
    let expires = expiry(token.exp);
    let state = match token.state {
        TokenState::Ok => format!("ok; expires at {expires}"),
        TokenState::Expiring => format!("expiring at {expires}"),
        TokenState::Expired => format!("expired at {expires}"),
        TokenState::Rejected => format!("rejected by platform; exp {expires}"),
        TokenState::Missing => "missing".to_string(),
        TokenState::Invalid => "invalid or unreadable expiry".to_string(),
        TokenState::Disabled => "ingress disabled".to_string(),
    };
    let last = last.map_or_else(|| "none".to_string(), |code| code.to_string());
    format!(
        "mail channel token: {state}; present: {}; last ingress status: {last}",
        token.present
    )
}

async fn capture(command: &OpsCommand) -> Option<String> {
    let (ok, out, _) = tokio::time::timeout(Duration::from_secs(6), run_capture(command))
        .await
        .ok()?
        .ok()?;
    ok.then_some(out)
}

pub fn pods_command(namespace: &str, release: &str) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("get"),
            plain("pods"),
            plain("-n"),
            plain(namespace),
            plain("-l"),
            plain(format!(
                "app.kubernetes.io/instance={release},app.kubernetes.io/component=mail-adapter"
            )),
            plain("-o"),
            plain("json"),
            plain("--request-timeout=5s"),
        ],
    )
}

fn proxy_command(namespace: &str, pod: &str) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("get"),
            plain("--raw"),
            plain(format!(
                "/api/v1/namespaces/{namespace}/pods/{pod}:8080/proxy/statusz"
            )),
            plain("--request-timeout=5s"),
        ],
    )
}

pub fn unavailable(namespace: &str, release: &str) -> Report {
    Report {
        pod: String::new(), channel_token: None, last_ingress_status: None,
        detail: "mail channel token: unknown; adapter status could not be read".to_string(),
        fix: Some(format!(
            "kubectl -n {namespace} get pods -l app.kubernetes.io/instance={release},app.kubernetes.io/component=mail-adapter; check pod proxy access and that the adapter version provides /statusz"
        )),
    }
}

pub async fn observe(namespace: &str, release: &str) -> Vec<Report> {
    let Some(raw) = capture(&pods_command(namespace, release)).await else {
        return vec![unavailable(namespace, release)];
    };
    let Some(pods) = serde_json::from_str::<Value>(&raw)
        .ok()
        .and_then(|v| v["items"].as_array().cloned())
    else {
        return vec![unavailable(namespace, release)];
    };
    observe_pods(namespace, release, &pods).await
}

pub async fn observe_pods(namespace: &str, release: &str, pods: &[Value]) -> Vec<Report> {
    let mut reports = Vec::new();
    for pod in pods {
        if pod
            .pointer("/metadata/labels/app.kubernetes.io~1component")
            .and_then(Value::as_str)
            != Some("mail-adapter")
            || pod
                .pointer("/metadata/labels/app.kubernetes.io~1instance")
                .and_then(Value::as_str)
                != Some(release)
            || pod.pointer("/metadata/deletionTimestamp").is_some()
        {
            continue;
        }
        let name = pod
            .pointer("/metadata/name")
            .and_then(Value::as_str)
            .unwrap_or("");
        let mut report = unavailable(namespace, release);
        report.pod = name.to_string();
        if let Some(raw) = capture(&proxy_command(namespace, name)).await {
            if let Ok(status) = serde_json::from_str::<Status>(&raw) {
                report.detail = describe(&status.channel_token, status.last_ingress_status);
                report.last_ingress_status = status.last_ingress_status;
                report.channel_token = Some(status.channel_token);
                report.fix = (!report.healthy()).then(|| recovery(namespace, release));
            }
        }
        reports.push(report);
    }
    reports
}
