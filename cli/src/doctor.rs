//! `curie doctor`: what is set up, what is missing, and the command that fixes it.
//!
//! A first-time user learns the required inputs one failure at a time --
//! `skill up` succeeds and `skill message` fails on a credential; a deploy works
//! and the next `git push` does nothing because no ingress exists. The list is
//! about ten items long, and nothing states it up front.
//!
//! Documentation is the obvious answer and the wrong one: a checklist in a
//! README goes stale the moment a flag changes, and it cannot tell an operator
//! which items THEY are missing. This can, and it reports what is actually
//! observable rather than what a doc claims.
//!
//! Two rules the checks follow:
//!
//! - **Names, never values.** A credential is reported by the variable that
//!   holds it. This output is pasted into issues and chat.
//! - **Absent is not broken.** Someone on the laptop rung has no cluster and is
//!   not misconfigured. Cluster checks report `NotApplicable` rather than
//!   failing, so the output stays readable at every rung.

use serde::Serialize;

/// What one check found.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum State {
    /// Configured and usable.
    Ok,
    /// Genuinely missing, and something the user is trying to do needs it.
    Missing,
    /// Not needed at the rung this install is on.
    NotApplicable,
}

impl State {
    pub fn glyph(self) -> &'static str {
        match self {
            State::Ok => "ok  ",
            State::Missing => "MISS",
            State::NotApplicable => "--  ",
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Check {
    /// Stable identifier, for a consumer gating on a specific check.
    pub id: &'static str,
    pub title: &'static str,
    pub state: State,
    /// What was observed. Never a credential value.
    pub detail: String,
    /// The exact command that fixes it, when it is fixable.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fix: Option<String>,
}

/// Everything the checks reason about, gathered once.
///
/// Separating observation from judgement is what makes the judgement testable:
/// every check below is a pure function of this struct, so the interesting
/// cases -- half-configured, laptop-only, fully wired -- are unit tests rather
/// than cluster fixtures.
#[derive(Debug, Clone, Default)]
pub struct Facts {
    /// NAME of the model credential found, never its value.
    pub model_credential: Option<String>,
    /// Where it came from, for the detail line.
    pub model_credential_source: Option<String>,
    pub docker_ok: bool,
    /// Plugin name from `.claude-plugin/plugin.json` in the working directory.
    pub bundle_name: Option<String>,
    pub kube_context: Option<String>,
    /// `(release, chart)` when a Curie release is installed.
    pub release: Option<(String, String)>,
    /// Whether the release has non-empty Slack tokens recorded.
    pub slack_configured: bool,
    /// Which clone credential the release carries, if any.
    pub clone_credential: Option<String>,
    /// How the API is reachable from outside, if it is. `None` means neither
    /// mechanism the chart knows about is in place -- which is NOT proof it is
    /// unreachable, since a load balancer or tunnel in front is invisible here.
    pub api_exposure: Option<String>,
}

fn ok(id: &'static str, title: &'static str, detail: impl Into<String>) -> Check {
    Check {
        id,
        title,
        state: State::Ok,
        detail: detail.into(),
        fix: None,
    }
}

fn missing(
    id: &'static str,
    title: &'static str,
    detail: impl Into<String>,
    fix: impl Into<String>,
) -> Check {
    Check {
        id,
        title,
        state: State::Missing,
        detail: detail.into(),
        fix: Some(fix.into()),
    }
}

fn skipped(id: &'static str, title: &'static str, detail: impl Into<String>) -> Check {
    Check {
        id,
        title,
        state: State::NotApplicable,
        detail: detail.into(),
        fix: None,
    }
}

/// Judge the gathered facts. Pure.
pub fn evaluate(f: &Facts) -> Vec<Check> {
    let mut out = Vec::new();

    out.push(match (&f.model_credential, &f.model_credential_source) {
        (Some(name), Some(src)) => ok(
            "model-credential",
            "Model credential",
            format!("{name} ({src})"),
        ),
        (Some(name), None) => ok("model-credential", "Model credential", name.clone()),
        _ => missing(
            "model-credential",
            "Model credential",
            "none found",
            "export CURIE_CREDENTIALS=sk-ant-...   (or `curie secrets set CURIE_CREDENTIALS`; \
             `curie skill up --fake-model` needs none)",
        ),
    });

    out.push(if f.docker_ok {
        ok("docker", "Docker", "running")
    } else {
        missing(
            "docker",
            "Docker",
            "not reachable",
            "start Docker Desktop, or install it: https://docs.docker.com/get-docker/",
        )
    });

    out.push(match &f.bundle_name {
        Some(name) => ok("bundle", "Bundle in this directory", name.clone()),
        None => missing(
            "bundle",
            "Bundle in this directory",
            "no .claude-plugin/plugin.json",
            "curie init my-agent && cd my-agent",
        ),
    });

    // Everything below needs a cluster. Without one this install is on the
    // laptop rung, which is a complete way to use Curie -- so these report as
    // not-applicable rather than as failures.
    let Some(context) = &f.kube_context else {
        out.push(skipped(
            "cluster",
            "Cluster",
            "no kube context — laptop rung only, which is fine",
        ));
        for (id, title) in [
            ("release", "Curie release"),
            ("slack", "Slack"),
            ("clone-credential", "Clone credential"),
            ("webhook", "Webhook exposure"),
        ] {
            out.push(skipped(id, title, "needs a cluster"));
        }
        return out;
    };
    out.push(ok("cluster", "Cluster", context.clone()));

    let Some((release, chart)) = &f.release else {
        out.push(missing(
            "release",
            "Curie release",
            "not installed in this namespace",
            "curie cluster up --namespace <ns> --release <name> --allow-egress-host anthropic",
        ));
        for (id, title) in [
            ("slack", "Slack"),
            ("clone-credential", "Clone credential"),
            ("webhook", "Webhook exposure"),
        ] {
            out.push(skipped(id, title, "needs a release"));
        }
        return out;
    };
    out.push(ok(
        "release",
        "Curie release",
        format!("{release} ({chart})"),
    ));

    out.push(if f.slack_configured {
        ok("slack", "Slack", "app and bot tokens recorded")
    } else {
        missing(
            "slack",
            "Slack",
            "no tokens recorded",
            "curie cluster comms --slack --app-token xapp-... --bot-token xoxb-...",
        )
    });

    out.push(match &f.clone_credential {
        Some(which) => ok("clone-credential", "Clone credential", which.clone()),
        None => missing(
            "clone-credential",
            "Clone credential",
            "none — a private repo cannot be cloned, so git-push deploys will fail",
            "curie cluster github-app --app-id <id> --private-key ./key.pem",
        ),
    });

    // Exposure has more than one shape, and asserting otherwise cries wolf on a
    // working install: sre-bot serves its webhook on a NodePort with no ingress
    // at all, and an early version of this check called that broken. A doctor
    // that is wrong about a working setup is worse than no doctor, because it
    // teaches people to ignore it.
    out.push(match &f.api_exposure {
        Some(how) => ok("webhook", "Webhook exposure", how.clone()),
        None => missing(
            "webhook",
            "Webhook exposure",
            "no ingress and no NodePort — if a load balancer or tunnel fronts the API, \
             this check cannot see it and you can ignore this",
            "curie cluster up --set api.ingress.enabled=true --set api.ingress.host=<host>",
        ),
    });

    out
}

/// The one-line verdict: what this install can do right now.
///
/// Deliberately capability-shaped rather than a count. "3 of 8 checks passed"
/// tells an operator nothing; "you can run locally but not deploy" tells them
/// where they are.
pub fn summary(checks: &[Check]) -> String {
    let state = |id: &str| checks.iter().find(|c| c.id == id).map(|c| c.state);
    let has = |id: &str| state(id) == Some(State::Ok);

    if !has("bundle") {
        return "No bundle here. Start with `curie init my-agent`.".to_string();
    }
    if !has("docker") {
        return "Docker is not reachable, so nothing can run locally yet.".to_string();
    }
    if !has("model-credential") {
        return "Ready to run offline (`curie skill up --fake-model`). A model \
                credential is needed for real replies."
            .to_string();
    }
    if !has("release") {
        return "Ready to run locally. No cluster release yet, so no Slack and no \
                deploys."
            .to_string();
    }
    if !has("slack") {
        return "Deployable to the cluster. Slack is not wired, so the agent has no \
                way to be reached."
            .to_string();
    }
    if !has("clone-credential") || !has("webhook") {
        return "Answering in Slack. Git-push deploys are not wired yet -- see the \
                missing items above."
            .to_string();
    }
    "Fully wired: local runs, Slack, and git-push deploys.".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn laptop() -> Facts {
        Facts {
            docker_ok: true,
            bundle_name: Some("my-agent".into()),
            ..Default::default()
        }
    }

    fn find<'a>(checks: &'a [Check], id: &str) -> &'a Check {
        checks.iter().find(|c| c.id == id).expect(id)
    }

    /// The laptop rung is a complete way to use Curie. Reporting five failures
    /// at someone who has not asked for a cluster is how a doctor command
    /// becomes noise people stop reading.
    #[test]
    fn no_cluster_is_not_a_failure() {
        let checks = evaluate(&laptop());
        for id in ["cluster", "release", "slack", "clone-credential", "webhook"] {
            assert_eq!(
                find(&checks, id).state,
                State::NotApplicable,
                "{id} must not read as broken on the laptop rung"
            );
        }
        assert!(
            find(&checks, "cluster").detail.contains("which is fine"),
            "the detail should reassure, not accuse"
        );
    }

    /// The failure a first-time user actually hits: boot succeeds, the next
    /// command fails. The fix has to name the variable AND the offline escape.
    #[test]
    fn a_missing_credential_names_both_ways_forward() {
        let checks = evaluate(&laptop());
        let c = find(&checks, "model-credential");
        assert_eq!(c.state, State::Missing);
        let fix = c.fix.as_deref().expect("must offer a fix");
        assert!(fix.contains("CURIE_CREDENTIALS"), "{fix}");
        assert!(
            fix.contains("--fake-model"),
            "the offline path matters: {fix}"
        );
    }

    /// This output gets pasted into issues and chat.
    #[test]
    fn no_check_can_carry_a_credential_value() {
        let f = Facts {
            model_credential: Some("CURIE_CREDENTIALS".into()),
            model_credential_source: Some("environment".into()),
            clone_credential: Some("github app (app_id=4475970)".into()),
            slack_configured: true,
            ..laptop()
        };
        let rendered = format!("{:?}", evaluate(&f));
        for leaked in ["sk-ant-", "xoxb-", "xapp-", "ghp_", "-----BEGIN"] {
            assert!(!rendered.contains(leaked), "{leaked} leaked: {rendered}");
        }
    }

    /// Each rung's summary has to say what you can DO, not how many checks
    /// passed -- a count tells an operator nothing about where they are.
    #[test]
    fn the_summary_reports_capability_at_each_rung() {
        let cases = [
            (Facts::default(), "curie init"),
            (laptop(), "fake-model"),
            (
                Facts {
                    model_credential: Some("CURIE_CREDENTIALS".into()),
                    ..laptop()
                },
                "No cluster release",
            ),
            (
                Facts {
                    model_credential: Some("CURIE_CREDENTIALS".into()),
                    kube_context: Some("minikube".into()),
                    release: Some(("acme".into(), "curie-0.6.0".into())),
                    ..laptop()
                },
                "Slack is not wired",
            ),
            (
                Facts {
                    model_credential: Some("CURIE_CREDENTIALS".into()),
                    kube_context: Some("minikube".into()),
                    release: Some(("acme".into(), "curie-0.6.0".into())),
                    slack_configured: true,
                    ..laptop()
                },
                "Git-push deploys are not wired",
            ),
            (
                Facts {
                    model_credential: Some("CURIE_CREDENTIALS".into()),
                    kube_context: Some("minikube".into()),
                    release: Some(("acme".into(), "curie-0.6.0".into())),
                    slack_configured: true,
                    clone_credential: Some("github app".into()),
                    api_exposure: Some("NodePort 30799".into()),
                    ..laptop()
                },
                "Fully wired",
            ),
        ];
        for (facts, expected) in cases {
            let s = summary(&evaluate(&facts));
            assert!(s.contains(expected), "expected {expected:?} in {s:?}");
        }
    }

    /// Every failing check must be actionable. A report that says "missing"
    /// without saying what to run is the checklist problem restated.
    #[test]
    fn every_missing_check_carries_a_command() {
        let facts = [Facts::default(), laptop()];
        for f in facts {
            for c in evaluate(&f).iter().filter(|c| c.state == State::Missing) {
                let fix = c.fix.as_deref().unwrap_or("");
                assert!(
                    fix.contains("curie ") || fix.contains("export ") || fix.contains("http"),
                    "{} must name a command, got {fix:?}",
                    c.id
                );
            }
        }
    }

    /// Found by running this against a real install. sre-bot serves its webhook
    /// on a NodePort with no ingress, and the first version of this check called
    /// that broken -- on a cluster where git-push deploys demonstrably work.
    #[test]
    fn a_nodeport_counts_as_exposure() {
        let f = Facts {
            model_credential: Some("CURIE_CREDENTIALS".into()),
            kube_context: Some("default".into()),
            release: Some(("sre-bot".into(), "curie-0.6.0".into())),
            slack_configured: true,
            clone_credential: Some("github app".into()),
            api_exposure: Some("NodePort 30799".into()),
            ..laptop()
        };
        let checks = evaluate(&f);
        assert_eq!(find(&checks, "webhook").state, State::Ok);
        assert!(
            summary(&checks).contains("Fully wired"),
            "{}",
            summary(&checks)
        );
    }

    /// And when nothing is found, it must not claim the API is unreachable --
    /// a load balancer or tunnel in front is invisible to this check.
    #[test]
    fn no_known_exposure_is_hedged_not_asserted() {
        let f = Facts {
            model_credential: Some("CURIE_CREDENTIALS".into()),
            kube_context: Some("minikube".into()),
            release: Some(("acme".into(), "curie-0.6.0".into())),
            slack_configured: true,
            clone_credential: Some("pat".into()),
            ..laptop()
        };
        let c = find(&evaluate(&f), "webhook").clone();
        assert_eq!(c.state, State::Missing);
        assert!(c.detail.contains("you can ignore this"), "{}", c.detail);
    }

    /// A half-wired release is the state that looks fine and is not: the bot
    /// answers, and every push silently does nothing.
    #[test]
    fn slack_without_deploy_wiring_is_reported_precisely() {
        let f = Facts {
            model_credential: Some("CURIE_CREDENTIALS".into()),
            kube_context: Some("minikube".into()),
            release: Some(("acme".into(), "curie-0.6.0".into())),
            slack_configured: true,
            ..laptop()
        };
        let checks = evaluate(&f);
        assert_eq!(find(&checks, "slack").state, State::Ok);
        assert_eq!(find(&checks, "webhook").state, State::Missing);
        assert!(
            find(&checks, "webhook")
                .detail
                .contains("no ingress and no NodePort"),
            "must name what it looked for: {}",
            find(&checks, "webhook").detail
        );
    }
}

// -- observation --------------------------------------------------------------

/// Gather the facts. Every probe is read-only and failure-tolerant: a missing
/// tool or an unreachable cluster is a fact to report, never an error to raise.
pub async fn gather(namespace: &str, release: &str) -> Facts {
    let mut f = Facts {
        docker_ok: probe_ok("docker", &["info"]).await,
        bundle_name: bundle_name(),
        ..Default::default()
    };

    for name in crate::commands::MODEL_CREDENTIAL_ENV_NAMES {
        if std::env::var(name).map(|v| !v.is_empty()).unwrap_or(false) {
            f.model_credential = Some(name.to_string());
            f.model_credential_source = Some("environment".into());
            break;
        }
        if crate::secrets::is_saved(name).unwrap_or(false) {
            f.model_credential = Some(name.to_string());
            f.model_credential_source = Some("curie secrets".into());
            break;
        }
    }

    let (ok, ctx, _) = capture("kubectl", &["config", "current-context"]).await;
    if !ok || ctx.trim().is_empty() {
        return f;
    }
    f.kube_context = Some(ctx.trim().to_string());

    let common = crate::ops::CommonOpts {
        namespace: namespace.to_string(),
        release: release.to_string(),
        dry_run: false,
    };
    let Ok(Some(chart)) = crate::ops::fetch_release_chart(&common).await else {
        return f;
    };
    f.release = Some((release.to_string(), chart));

    if let Ok(Some(values)) = crate::ops::fetch_release_values(&common).await {
        let at = |path: &[&str]| -> Option<String> {
            let mut node = &values;
            for k in path {
                node = node.get(k)?;
            }
            node.as_str().map(str::to_string).filter(|s| !s.is_empty())
        };
        f.slack_configured = at(&["dispatcher", "slack", "botToken"]).is_some();
        let ingress_on = values
            .get("api")
            .and_then(|a| a.get("ingress"))
            .and_then(|i| i.get("enabled"))
            .and_then(serde_json::Value::as_bool)
            .unwrap_or(false);
        f.api_exposure = if ingress_on {
            Some(match at(&["api", "ingress", "host"]) {
                Some(host) => format!("ingress ({host})"),
                None => "ingress".to_string(),
            })
        } else {
            api_nodeport(namespace, release)
                .await
                .map(|p| format!("NodePort {p}"))
        };
        // NAMES and shapes only -- never the key or token itself.
        f.clone_credential = match at(&["api", "githubAppId"]) {
            Some(id) => Some(format!("github app (app_id={id})")),
            None => at(&["api", "githubToken"]).map(|_| "personal access token".to_string()),
        };
    }
    f
}

/// The API Service's nodePort, when it is exposed that way.
async fn api_nodeport(namespace: &str, release: &str) -> Option<String> {
    let (ok, out, _) = capture(
        "kubectl",
        &[
            "get",
            "svc",
            &format!("{release}-api"),
            "-n",
            namespace,
            "-o",
            "jsonpath={.spec.ports[?(@.nodePort)].nodePort}",
        ],
    )
    .await;
    let port = out.trim().to_string();
    (ok && !port.is_empty()).then_some(port)
}

fn bundle_name() -> Option<String> {
    let raw = std::fs::read_to_string(".claude-plugin/plugin.json").ok()?;
    let v: serde_json::Value = serde_json::from_str(&raw).ok()?;
    v.get("name")?.as_str().map(str::to_string)
}

async fn probe_ok(program: &str, args: &[&str]) -> bool {
    capture(program, args).await.0
}

async fn capture(program: &str, args: &[&str]) -> (bool, String, String) {
    let cmd = crate::ops::OpsCommand::new(
        program,
        args.iter().map(|a| crate::ops::plain(*a)).collect(),
    );
    crate::ops::run_capture(&cmd)
        .await
        .unwrap_or((false, String::new(), String::new()))
}

/// What `curie doctor` reports.
#[derive(Debug)]
pub struct DoctorOutput {
    pub checks: Vec<Check>,
    pub summary: String,
}

impl crate::ui::CliOutput for DoctorOutput {
    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "summary": self.summary,
            "ready": self.checks.iter().all(|c| c.state != State::Missing),
            "checks": self.checks,
        })
    }

    fn render(&self, ui: &crate::ui::Ui) {
        for c in &self.checks {
            ui.payload_plain(&format!(
                "{}  {:<26} {}",
                c.state.glyph(),
                c.title,
                c.detail
            ));
            if let Some(fix) = &c.fix {
                ui.payload_plain(&format!("      → {fix}"));
            }
        }
        ui.payload_plain("");
        ui.payload_plain(&self.summary);
    }
}

pub async fn doctor(namespace: &str, release: &str) -> DoctorOutput {
    let checks = evaluate(&gather(namespace, release).await);
    let summary = summary(&checks);
    DoctorOutput { checks, summary }
}
