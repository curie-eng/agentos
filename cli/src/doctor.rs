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

use crate::modelpin::{classify, PinStatus};

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
    /// Verbatim `CURIE_MODEL` from the invoking shell. Lowest precedence: the
    /// shell is not a declared producer of this variable (#1950). A value here
    /// is not a claim that the id is valid, only that something set it.
    pub model_shell: Option<String>,
    /// The model the release's sandboxes boot, read from its COMPUTED helm
    /// values, so a chart default the operator never supplied is still
    /// observed. See [`runner_model_from_values`] for which key that is.
    pub model_release_default: Option<String>,
    /// WHICH chart key [`Facts::model_release_default`] was read from. The
    /// chart branches between two of them, and a report that names the wrong
    /// one hands the operator a `--set` the chart ignores. `None` means the key
    /// was not observed -- `gather` always sets both together -- and falls back
    /// to the key a default install boots from.
    pub model_release_key: Option<ReleaseModelKey>,
    /// Whether the release currently boots the runner's SCRIPTED FAKE model,
    /// which makes [`Facts::model_release_default`] configured-but-unused. See
    /// [`release_fake_model`]: the chart's fake-model flag and the model id are
    /// independent template arms, so a default install renders BOTH
    /// `CURIE_FAKE_MODEL=1` and `CURIE_MODEL=claude-sonnet-5` and the id alone
    /// is not proof the pod boots it -- the same "names a model the pod does
    /// not boot" defect class as #1950 itself.
    pub model_release_fake: bool,
    /// `(agent name, model)` for every agent carrying a per-agent override,
    /// forwarded as `CURIE_MODEL` at sandbox boot. Highest precedence.
    pub model_agent_overrides: Vec<(String, String)>,
    /// The `(namespace, release)` this run was invoked with. An observed fact
    /// about the run, kept on `Facts` so `evaluate` stays pure and the fix
    /// string can name the release actually diagnosed rather than `curie/curie`
    /// (#1358 item 1).
    pub target: Option<(String, String)>,
    /// Non-secret provider inferred from the bound `CURIE_CREDENTIALS` value.
    /// The credential itself is deliberately discarded during observation.
    pub model_credential_provider: Option<&'static str>,
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
    /// Every agent and its repository binding. `None` means the platform API
    /// was not reached, which is a fact to report rather than a failure -- the
    /// other checks need only kubectl and helm.
    pub agents: Option<Vec<(String, Option<String>)>>,
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

/// The recovery command for a missing cluster release. Provider egress follows
/// the same credential-prefix map as `cluster up`; an absent or unrecognized
/// credential leaves egress sealed rather than guessing Anthropic.
fn missing_release_recovery(provider: Option<&str>) -> String {
    let mut command = "curie cluster up --namespace <ns> --release <name>".to_string();
    if let Some(provider) = provider {
        command.push_str(&format!(" --allow-egress-host {provider}"));
    }
    command
}

/// Which chart key the release's model came from.
///
/// `charts/curie/templates/agent-sandbox.yaml` renders exactly one
/// `CURIE_MODEL` entry and picks its value on a branch, so the release default
/// has two possible homes and only one of them is live on any given install.
/// Carrying the key is what keeps the report and the fix honest: while the
/// in-cluster inference service is deployed the chart IGNORES
/// `agentSandbox.runner.model` entirely, so naming that key on a
/// `curie cluster up --local-model` install both mislabels the source and
/// prints a `--set` that cannot change the model in force.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum ReleaseModelKey {
    /// `agentSandbox.runner.model` -- what a default install boots.
    #[default]
    Runner,
    /// `inference.model` -- live only while `inference.deploy` is truthy.
    Inference,
}

impl ReleaseModelKey {
    /// The dotted key exactly as the chart and `--set` spell it.
    fn chart_key(self) -> &'static str {
        match self {
            ReleaseModelKey::Runner => "agentSandbox.runner.model",
            ReleaseModelKey::Inference => "inference.model",
        }
    }
}

/// Where a model id was observed. Three sources, and they can disagree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModelSource {
    /// A per-agent override, carrying the agent's real name. The worker
    /// forwards it as `CURIE_MODEL` at sandbox boot, so it wins outright.
    Agent(String),
    /// The release's own default, from its computed helm values, carrying the
    /// chart key it was read from -- the two keys are not interchangeable, see
    /// [`ReleaseModelKey`].
    ReleaseDefault(ReleaseModelKey),
    /// The invoking shell's `CURIE_MODEL`. Lowest, because the shell is not a
    /// declared producer of that variable.
    Shell,
}

impl ModelSource {
    /// How the source reads in the detail line. An id with no label is not
    /// something an operator can go and change.
    fn label(&self) -> String {
        match self {
            ModelSource::Agent(name) => format!("agent \"{name}\""),
            ModelSource::ReleaseDefault(key) => {
                format!("release default {}", key.chart_key())
            }
            ModelSource::Shell => "CURIE_MODEL".to_string(),
        }
    }
}

/// The model this install will actually boot, and what else claimed otherwise.
#[derive(Debug, Clone)]
pub struct InForceModel {
    /// Which source won precedence.
    pub source: ModelSource,
    /// The id that source carries, trimmed and non-empty.
    pub id: String,
    /// Every other source whose id DIFFERS from the one in force. An identical
    /// id is not a disagreement.
    pub disagreeing: Vec<(ModelSource, String)>,
}

/// Which model the install boots, out of the three places one can be set. Pure.
///
/// Precedence is the boot path, not a preference: a per-agent override beats
/// the release default, which beats the invoking shell. `None` only when no
/// source yields a model at all.
///
/// When several agents set DIFFERENT models there is no single answer, so the
/// tie-break is chosen to make the check safe rather than pretty: rank the
/// WEAKEST pin first (`Floating`, then `Unrecognized`, then `Pinned`), so the
/// check can never report clean while one agent floats, then break the
/// remaining ties on agent name ascending so the report is stable across runs.
fn resolve_model(f: &Facts) -> Option<InForceModel> {
    /// #229's footgun at three sources instead of one: an exported-but-empty
    /// value is not a configured model, and letting one win precedence would
    /// resolve to `Unset` and report not-applicable on an install that boots
    /// something.
    fn cleaned(id: &str) -> Option<String> {
        let id = id.trim();
        (!id.is_empty()).then(|| id.to_string())
    }

    fn weakness(id: &str) -> u8 {
        match classify(Some(id)) {
            PinStatus::Floating { .. } => 0,
            PinStatus::Unrecognized { .. } => 1,
            _ => 2,
        }
    }

    let mut agents: Vec<(String, String)> = f
        .model_agent_overrides
        .iter()
        .filter_map(|(name, id)| cleaned(id).map(|id| (name.clone(), id)))
        .collect();
    agents.sort_by(|a, b| a.0.cmp(&b.0));
    let release = f.model_release_default.as_deref().and_then(cleaned);
    let shell = f.model_shell.as_deref().and_then(cleaned);

    // `min_by_key` keeps the first of several equal minima, and the list is
    // already name-ascending, so this IS the documented tie-break.
    let (source, id) = match agents.iter().min_by_key(|(_, id)| weakness(id)) {
        Some((name, id)) => (ModelSource::Agent(name.clone()), id.clone()),
        None => match &release {
            Some(id) => (
                ModelSource::ReleaseDefault(f.model_release_key.unwrap_or_default()),
                id.clone(),
            ),
            None => (ModelSource::Shell, shell.clone()?),
        },
    };

    let mut disagreeing: Vec<(ModelSource, String)> = agents
        .iter()
        .filter(|(_, other)| *other != id)
        .map(|(name, other)| (ModelSource::Agent(name.clone()), other.clone()))
        .collect();
    for (candidate, other) in [
        (
            ModelSource::ReleaseDefault(f.model_release_key.unwrap_or_default()),
            &release,
        ),
        (ModelSource::Shell, &shell),
    ] {
        if let Some(other) = other {
            if *other != id {
                disagreeing.push((candidate, other.clone()));
            }
        }
    }

    Some(InForceModel {
        source,
        id,
        disagreeing,
    })
}

/// The model the release's sandboxes boot, out of its computed helm values.
///
/// It is NOT a single path. `charts/curie/templates/agent-sandbox.yaml` renders
/// exactly one `CURIE_MODEL` env entry and picks its value on a branch:
/// `.Values.inference.model` when `.Values.inference.deploy` is TRUTHY by Go's
/// rule (the `curie cluster up --local-model` shape; see [`helm_truthy`], which
/// is not the same test as "is the boolean true"), otherwise
/// `.Values.agentSandbox.runner.model`. Reproducing that branch is what keeps
/// this from naming a model the pod never boots.
///
/// It returns the key alongside the id: the label and the fix both have to
/// name the key that is actually live, or the operator is told to `--set` one
/// the chart ignores.
///
/// An empty or whitespace-only value at either key is not a configured model
/// (#229) and falls through. The read ends in `as_str`, so a non-string at the
/// path reads as absent -- safe here because the key is a string in the chart
/// and in every `--set` form, and deliberately not widened (#1358 item 4).
fn runner_model_from_values(values: &serde_json::Value) -> Option<(String, ReleaseModelKey)> {
    fn at(values: &serde_json::Value, path: &[&str]) -> Option<String> {
        let mut node = values;
        for key in path {
            node = node.get(key)?;
        }
        let id = node.as_str()?.trim();
        (!id.is_empty()).then(|| id.to_string())
    }

    if helm_truthy(values.get("inference").and_then(|i| i.get("deploy"))) {
        if let Some(model) = at(values, &["inference", "model"]) {
            return Some((model, ReleaseModelKey::Inference));
        }
    }
    at(values, &["agentSandbox", "runner", "model"]).map(|id| (id, ReleaseModelKey::Runner))
}

/// Whether the release's sandboxes boot the runner's SCRIPTED FAKE model.
///
/// `charts/curie/templates/agent-sandbox.yaml` renders `CURIE_FAKE_MODEL=1` on
/// `and $runner.fakeModel (not .Values.inference.deploy)`, and picks
/// `CURIE_MODEL` on a SEPARATE arm. The two are independent, so on the chart's
/// shipped defaults (`agentSandbox.runner.fakeModel: true`) a release renders
/// BOTH `CURIE_FAKE_MODEL=1` and `CURIE_MODEL=claude-sonnet-5` -- the id is
/// configured and the pod boots the scripted fake instead. Reporting that id
/// as the model in force with no caveat is the very defect #1950 exists to
/// kill, one level down.
///
/// Both legs go through [`helm_truthy`] rather than `as_bool`, for the reason
/// spelled out there: a value that reaches Helm as a string still takes the
/// template branch, and disagreeing with Helm is how doctor names a model the
/// pod does not boot.
fn release_fake_model(values: &serde_json::Value) -> bool {
    let fake = values
        .get("agentSandbox")
        .and_then(|a| a.get("runner"))
        .and_then(|r| r.get("fakeModel"));
    let inference = values.get("inference").and_then(|i| i.get("deploy"));
    helm_truthy(fake) && !helm_truthy(inference)
}

/// Go template truthiness, mirrored for the one value the chart branches on.
///
/// `agent-sandbox.yaml` gates on `if .Values.inference.deploy`, and Go's
/// `empty` is false only for `false`, a zero number, an empty string and nil.
/// Reading it with `as_bool` instead made a value that arrives as a STRING --
/// a generic `--set-string inference.deploy=true`, say -- send Helm down the
/// inference branch while doctor went down the runner one, and doctor then
/// reported a model the pod does not boot.
///
/// Yes, this means the string `"false"` is truthy. That is Helm's rule, not a
/// bug here: a non-empty string is non-empty whatever it spells.
///
/// This ladder exists TWICE in the crate: see
/// `classify_existing_secret_field` in `cli/src/github_app.rs`, which mirrors
/// the same Go rule for `api.githubAppExistingSecret`. They must agree, so a
/// change to one belongs in both.
fn helm_truthy(value: Option<&serde_json::Value>) -> bool {
    match value {
        None | Some(serde_json::Value::Null) => false,
        Some(serde_json::Value::Bool(b)) => *b,
        Some(serde_json::Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(serde_json::Value::String(s)) => !s.is_empty(),
        // Go calls an EMPTY list or map empty exactly as it does `""`, so both
        // are falsy here and only a populated one is true. A scalar key like
        // this one should never carry either, but agreeing with Go costs
        // nothing and disagreeing with the copy in `github_app.rs` does.
        Some(serde_json::Value::Array(a)) => !a.is_empty(),
        Some(serde_json::Value::Object(o)) => !o.is_empty(),
    }
}

/// The command that pins the model at the source it is actually in force from.
///
/// Bare and runnable, with angle-bracket placeholders only: a fix string that
/// names a flag which does not exist fails for whoever pastes it (#1813). Note
/// `curie cluster up` has NO `--model` -- that flag belongs to `skill up` -- so
/// the release default is set through `--set <key>=`, where the key is the one
/// the release actually reads ([`ReleaseModelKey`]) rather than always
/// `agentSandbox.runner.model`, which a local-inference install ignores. The
/// namespace and release come from the run itself rather than defaulting to
/// `curie/curie` (#1358 item 1).
fn model_pin_fix(f: &Facts, source: &ModelSource) -> String {
    match source {
        ModelSource::Agent(name) => {
            format!("curie cluster overrides {name} --model <dated-snapshot-id>")
        }
        ModelSource::ReleaseDefault(key) => {
            let (namespace, release) = match &f.target {
                Some((namespace, release)) => (namespace.as_str(), release.as_str()),
                None => ("<ns>", "<release>"),
            };
            let key = key.chart_key();
            format!(
                "curie cluster up --namespace {namespace} --release {release} \
                 --set {key}=<dated-snapshot-id>"
            )
        }
        ModelSource::Shell => "export CURIE_MODEL=<dated-snapshot-id>".to_string(),
    }
}

/// The one not-applicable detail: no source yields a model at all.
///
/// It has to say WHICH sources were looked at, or it is indistinguishable from
/// the check being blind again -- and it must not claim there is no per-agent
/// override when the platform API was never reached to look.
fn no_model_determined(f: &Facts) -> String {
    let overrides = if f.agents.is_some() {
        "no per-agent override"
    } else {
        "per-agent overrides could not be read because the platform API was not reached"
    };
    format!(
        "no model determined: {overrides}, no release default \
         agentSandbox.runner.model or inference.model, and no CURIE_MODEL"
    )
}

/// The caveat for a report that could not see the highest-precedence source.
///
/// `Facts::agents` is `None` only when the platform API was not reached at all,
/// which is NOT the same fact as "reached, and no agent sets a model" -- and a
/// per-agent override outranks every other source. Reporting a clean pinned
/// release default while an agent quietly carries a floating one is the exact
/// failure #1950 exists to kill, so the check says what it could not see. The
/// state stays as it is (absent is not broken); the honesty is in the detail.
fn unread_agent_overrides(f: &Facts, in_force: &ModelSource) -> &'static str {
    if f.agents.is_none() && !matches!(in_force, ModelSource::Agent(_)) {
        "; per-agent model overrides could not be read because the platform \
         API was not reached, so an agent may boot a different model"
    } else {
        ""
    }
}

/// The caveat for an id the release has configured but does NOT currently boot.
///
/// See [`release_fake_model`] for why a real id and the fake model coexist. The
/// id is NOT suppressed and the state does NOT change: it is exactly what
/// applies the moment fake model is turned off, and the credential story has
/// its own `model-credential` check. Only the release source can be in this
/// position -- an agent override or a shell value is forwarded as `CURIE_MODEL`
/// regardless of what the chart's fake-model arm renders.
fn release_fake_model_clause(f: &Facts, in_force: &ModelSource) -> &'static str {
    if f.model_release_fake && matches!(in_force, ModelSource::ReleaseDefault(_)) {
        "; the sandbox currently boots the runner's scripted fake model, so \
         this id is configured but not in use"
    } else {
        ""
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

    // The model the install actually BOOTS, not the one the invoking shell
    // happens to name (#1950). `not_applicable` is reserved for the single case
    // where no source yields a model at all, and says which three were looked
    // at -- otherwise it is indistinguishable from the check being blind again.
    out.push(match resolve_model(f) {
        None => skipped("model-pin", "Model pin", no_model_determined(f)),
        Some(m) => {
            let source = m.source.label();
            // Nothing appended when nothing disagrees: a detail ending on a
            // dangling "other sources disagree:" reads as a truncated report.
            let disagreement = if m.disagreeing.is_empty() {
                String::new()
            } else {
                let named: Vec<String> = m
                    .disagreeing
                    .iter()
                    .map(|(source, id)| format!("{} = {id}", source.label()))
                    .collect();
                format!("; other sources disagree: {}", named.join(", "))
            };
            // What the report could NOT see, appended to every state: a
            // silently-unread override outranks whatever is named above.
            let unread = unread_agent_overrides(f, &m.source);
            // The id is configured but not what the pod boots. Placed after
            // the source label and before the disagreement/unread clauses, so
            // it qualifies the id it is about and nothing else.
            let fake = release_fake_model_clause(f, &m.source);
            match classify(Some(m.id.as_str())) {
                PinStatus::Pinned { id, date } => ok(
                    "model-pin",
                    "Model pin",
                    format!(
                        "{id} (snapshot {date}), in force from \
                         {source}{fake}{disagreement}{unread}"
                    ),
                ),
                // Ok rather than Missing, deliberately: a floating name works,
                // and a working install must not report as unready. What is at
                // risk is reproducibility, so this carries a fix without
                // failing the check.
                PinStatus::Floating { id } => Check {
                    id: "model-pin",
                    title: "Model pin",
                    state: State::Ok,
                    detail: format!(
                        "{id} is a floating name, in force from {source}; the \
                         provider can repoint it at new weights with no change \
                         here, and no gate would see it{fake}{disagreement}{unread}"
                    ),
                    fix: Some(model_pin_fix(f, &m.source)),
                },
                // No fix, and no claim about whether the id moves: the shape
                // rule cannot read this one, and a wrong fix string is worse
                // than none (#1813).
                PinStatus::Unrecognized { id } => ok(
                    "model-pin",
                    "Model pin",
                    format!(
                        "{id}, in force from {source}; this check reads a model \
                         id by shape alone and does not recognise this one, so \
                         it cannot say whether the id moves{fake}{disagreement}{unread}"
                    ),
                ),
                // Unreachable: `resolve_model` yields only trimmed, non-empty
                // ids. Report it as no model rather than assert it away.
                PinStatus::Unset => skipped("model-pin", "Model pin", no_model_determined(f)),
            }
        }
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
            ("repo-binding", "Repo binding"),
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
            missing_release_recovery(f.model_credential_provider),
        ));
        for (id, title) in [
            ("slack", "Slack"),
            ("clone-credential", "Clone credential"),
            ("webhook", "Webhook exposure"),
            ("repo-binding", "Repo binding"),
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

    // The binding decides whether a push reaches this agent at all. A push for
    // an agent with none matches nothing and is answered `ignored` -- nothing is
    // logged, so the only symptom is a green delivery in GitHub and no deploy.
    out.push(match &f.agents {
        None => skipped(
            "repo-binding",
            "Repo binding",
            "platform API not reached — pass --api-url/--api-key to include this",
        ),
        Some(agents) if agents.is_empty() => {
            skipped("repo-binding", "Repo binding", "no agents deployed yet")
        }
        Some(agents) => {
            let unbound: Vec<&str> = agents
                .iter()
                .filter(|(_, repo)| repo.is_none())
                .map(|(name, _)| name.as_str())
                .collect();
            if unbound.is_empty() {
                ok(
                    "repo-binding",
                    "Repo binding",
                    format!("{} agent(s), all bound", agents.len()),
                )
            } else {
                missing(
                    "repo-binding",
                    "Repo binding",
                    format!(
                        "unbound: {} — a push for these matches no agent and is \
                         silently ignored",
                        unbound.join(", ")
                    ),
                    "curie cluster deploy --plugin-dir . --repo <owner>/<name>   \
                     (binds an agent that has none; it will NOT rebind one already \
                     pointing at a different repository)",
                )
            }
        }
    });

    out
}

/// Point at the guided path when there is more than one thing to fix.
///
/// A single missing item needs no signpost: the `→ fix` line beside it is the
/// whole answer. Several is different -- the fixes have an order, some depend
/// on values the operator has not collected yet, and reading eight lines and
/// sequencing them is exactly the work the guided workflow already does.
///
/// `curie` with no arguments opens that workflow. It is discoverable in
/// principle and invisible in practice, because a first-time user reaching for
/// help types `curie --help` and gets an alphabetical list of eighteen verbs
/// with `interactive` eleventh.
pub fn guidance(checks: &[Check]) -> Option<String> {
    let missing = checks.iter().filter(|c| c.state == State::Missing).count();
    if missing < 2 {
        return None;
    }
    Some(format!(
        "{missing} things to set up. Run `curie` with no arguments for a guided \
         walkthrough, or fix them one at a time with the commands above."
    ))
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
    if !has("clone-credential") || !has("webhook") || state("repo-binding") == Some(State::Missing)
    {
        return "Answering in Slack. Git-push deploys are not wired yet -- see the \
                missing items above."
            .to_string();
    }
    // repo-binding reads NotApplicable in two situations that both reach this
    // point: the platform API was never consulted (no --api-url/--api-key, an
    // unreachable API, or a rejected key -- indistinguishable from here) or it
    // was reached and found no agents deployed yet. Neither is evidence a git
    // push deploys anything, so claiming "Fully wired" here asserted the one
    // capability the run did not check (#1354).
    if !has("repo-binding") {
        return "Answering in Slack. Git-push deploys are unverified -- see the \
                Repo binding line above."
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

    fn checks_with(missing: usize) -> Vec<Check> {
        (0..missing)
            .map(|i| Check {
                id: "x",
                title: "t",
                state: State::Missing,
                detail: format!("{i}"),
                fix: None,
            })
            .collect()
    }

    /// One gap needs no signpost -- the `-> fix` beside it is the whole answer.
    /// Sending someone to a TUI to set one environment variable is worse than
    /// telling them the variable.
    #[test]
    fn a_single_gap_does_not_advertise_the_walkthrough() {
        assert_eq!(guidance(&checks_with(0)), None);
        assert_eq!(guidance(&checks_with(1)), None);
    }

    /// #1533 (S17). `api_nodeport` reads the API Service to report how the API
    /// is exposed. It spawns kubectl, so the NAME it asks for is extracted into
    /// a pure command builder and asserted here -- no cluster, no child
    /// process.
    ///
    /// A wrong name makes the read return nothing, which `cluster_facts`
    /// renders as "API not exposed": a FALSE readiness verdict, the precise
    /// failure mode PR #1348 built `curie doctor` to prevent.
    ///
    /// Required signature (`cli/src/doctor.rs`):
    ///   fn api_nodeport_command(
    ///       namespace: &str,
    ///       fullname: &crate::ops::ReleaseFullname,
    ///   ) -> crate::ops::OpsCommand
    /// with `api_nodeport` calling it and running it through
    /// `crate::ops::run_capture`.
    #[test]
    fn api_nodeport_reads_the_chart_rendered_service() {
        let argv =
            api_nodeport_command("acme-system", &crate::ops::chart_fullname("platform")).argv();
        assert!(
            argv.iter().any(|a| a == "platform-curie-api"),
            "doctor must read the chart-rendered API service: {argv:?}"
        );
        assert!(
            !argv.iter().any(|a| a == "platform-api"),
            "doctor must not compute `{{release}}-api`: {argv:?}"
        );
        assert!(
            argv.iter().any(|a| a == "acme-system"),
            "the namespace must still be passed through: {argv:?}"
        );
        assert!(
            argv.iter()
                .any(|a| a.contains("jsonpath={.spec.ports[?(@.nodePort)].nodePort}")),
            "the nodePort jsonpath must be preserved: {argv:?}"
        );

        // Negative control: byte-identical for the default release.
        let control = api_nodeport_command("curie", &crate::ops::chart_fullname("curie")).argv();
        assert!(
            control.iter().any(|a| a == "curie-api"),
            "the default release must be unchanged: {control:?}"
        );
    }

    /// Several gaps have an ORDER and depend on values not yet collected, which
    /// is the work the guided workflow exists to do.
    #[test]
    fn several_gaps_name_the_guided_path() {
        let hint = guidance(&checks_with(3)).expect("should advertise");
        assert!(hint.contains("3 things"), "{hint}");
        assert!(hint.contains("`curie`"), "must name the command: {hint}");
        assert!(
            hint.contains("one at a time"),
            "must leave the manual path open: {hint}"
        );
    }

    /// A check that is NotApplicable is not a gap. Counting it would advertise
    /// a walkthrough for a cluster the operator does not have.
    #[test]
    fn checks_that_do_not_apply_are_not_counted_as_gaps() {
        let mut checks = checks_with(1);
        for i in 0..4 {
            checks.push(Check {
                id: "n",
                title: "t",
                state: State::NotApplicable,
                detail: format!("{i}"),
                fix: None,
            });
        }
        assert_eq!(
            guidance(&checks),
            None,
            "one real gap plus four n/a is one gap"
        );
    }

    /// Cluster, release, Slack and clone credential all in place.
    fn wired() -> Facts {
        Facts {
            model_credential: Some("CURIE_CREDENTIALS".into()),
            kube_context: Some("minikube".into()),
            release: Some(("acme".into(), "curie-0.6.0".into())),
            slack_configured: true,
            clone_credential: Some("github app".into()),
            api_exposure: Some("NodePort 30799".into()),
            ..laptop()
        }
    }

    fn find<'a>(checks: &'a [Check], id: &str) -> &'a Check {
        checks.iter().find(|c| c.id == id).expect(id)
    }

    /// The one check this issue is about, pulled out of a full `evaluate`.
    fn model_pin(f: &Facts) -> Check {
        find(&evaluate(f), "model-pin").clone()
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
        let credential = "sk-or-PLACEHOLDER";
        let f = Facts {
            model_credential: Some("CURIE_CREDENTIALS".into()),
            model_credential_source: Some("environment".into()),
            model_credential_provider: crate::ops::provider_from_credential_prefix(credential),
            clone_credential: Some("github app (app_id=4475970)".into()),
            slack_configured: true,
            ..laptop()
        };
        let rendered = format!("{:?}", evaluate(&f));
        for leaked in [
            credential,
            "sk-or-",
            "sk-ant-",
            "xoxb-",
            "xapp-",
            "ghp_",
            "-----BEGIN",
        ] {
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
                    agents: Some(vec![("bot".into(), Some("acme/bot".into()))]),
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

    /// A floating model name is reported without failing the install: it works
    /// today, so `Missing` would make a usable setup look broken. The fix is
    /// what carries the advice.
    #[test]
    fn a_floating_model_reports_ok_with_a_fix() {
        let f = Facts {
            model_shell: Some("gpt-4o".into()),
            ..Default::default()
        };
        let c = evaluate(&f)
            .into_iter()
            .find(|c| c.id == "model-pin")
            .expect("model-pin check");
        assert_eq!(c.state, State::Ok);
        assert!(c.detail.contains("gpt-4o"), "{}", c.detail);
        assert!(
            c.fix.as_deref().unwrap_or("").contains("CURIE_MODEL"),
            "the fix must name the variable to set"
        );
    }

    /// A dated snapshot is clean: no advice, nothing to do.
    #[test]
    fn a_pinned_snapshot_carries_no_fix() {
        let f = Facts {
            model_shell: Some("claude-haiku-4-5-20251001".into()),
            ..Default::default()
        };
        let c = evaluate(&f)
            .into_iter()
            .find(|c| c.id == "model-pin")
            .expect("model-pin check");
        assert_eq!(c.state, State::Ok);
        assert!(c.fix.is_none(), "a pinned snapshot needs no fix");
        assert!(c.detail.contains("20251001"), "{}", c.detail);
    }

    /// No model configured anywhere is not a gap: the platform default is a
    /// valid way to run, so this must not count against readiness. What #1950
    /// narrowed is what "unset" MEANS -- it is no longer "the invoking shell
    /// did not export CURIE_MODEL", it is "no source yields a model at all",
    /// and the detail has to say which three sources were looked at or the
    /// operator cannot tell this apart from the check being blind again.
    #[test]
    fn an_unset_model_is_not_applicable() {
        let c = evaluate(&Facts::default())
            .into_iter()
            .find(|c| c.id == "model-pin")
            .expect("model-pin check");
        assert_eq!(c.state, State::NotApplicable);
        let detail = &c.detail;
        assert!(
            detail.contains("override"),
            "must name the per-agent override as one of the sources looked at: {detail}"
        );
        assert!(
            detail.contains("agentSandbox.runner.model"),
            "must name the release default by its real key: {detail}"
        );
        assert!(
            detail.contains("CURIE_MODEL"),
            "must name the shell variable: {detail}"
        );
    }

    /// The exact scenario #1950 reports, and the single most important
    /// assertion in this change. An operator runs `curie cluster up`, never
    /// exports CURIE_MODEL, and the chart's own default `claude-sonnet-5` --
    /// which this repo's own `an_undated_name_floats` calls a floating alias --
    /// is what every sandbox boots. Before this, the shell was the only source
    /// read, so the check reported `not_applicable` with no fix: the
    /// diagnostic built to catch a floating alias reported clean on the
    /// shipped default.
    #[test]
    fn a_floating_release_default_on_a_live_release_reports_floating() {
        let f = Facts {
            model_release_default: Some("claude-sonnet-5".into()),
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let c = model_pin(&f);
        assert_ne!(
            c.state,
            State::NotApplicable,
            "a floating model on a live release is not 'not applicable': {}",
            c.detail
        );
        assert_eq!(c.state, State::Ok, "{}", c.detail);
        assert!(
            c.detail.contains("claude-sonnet-5"),
            "must name the id actually in force: {}",
            c.detail
        );
        assert!(
            c.detail.contains("release default"),
            "must say WHERE it is in force from, or the operator cannot act on \
             it: {}",
            c.detail
        );
        assert!(
            c.fix.is_some(),
            "a floating name in force is exactly the case that carries advice"
        );
    }

    /// AC1's precedence, exercised as a ladder: the per-agent override is what
    /// the worker forwards as CURIE_MODEL at sandbox boot, so it beats the
    /// release default, which in turn beats the invoking shell -- a value the
    /// boot-env contract does not even declare the CLI as a producer of. Each
    /// rung is asserted by removing the one above it, so a precedence order
    /// written backwards fails here rather than in production.
    #[test]
    fn an_agent_override_beats_the_release_default_beats_the_shell() {
        let all = Facts {
            model_shell: Some("shell-model-20250101".into()),
            model_release_default: Some("claude-sonnet-5".into()),
            model_agent_overrides: vec![("bot".into(), "gpt-4o-mini".into())],
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };

        let r = resolve_model(&all).expect("a model is in force");
        assert_eq!(r.id, "gpt-4o-mini");
        assert!(
            matches!(&r.source, ModelSource::Agent(name) if name == "bot"),
            "the agent override must win, and must carry the agent's real name"
        );
        let c = model_pin(&all);
        assert!(c.detail.contains("gpt-4o-mini"), "{}", c.detail);
        assert!(
            c.detail.contains("bot"),
            "the operator has to know WHICH agent is in force: {}",
            c.detail
        );

        let no_agent = Facts {
            model_agent_overrides: vec![],
            ..all.clone()
        };
        let r = resolve_model(&no_agent).expect("a model is in force");
        assert_eq!(r.id, "claude-sonnet-5");
        assert!(matches!(
            r.source,
            ModelSource::ReleaseDefault(ReleaseModelKey::Runner)
        ));

        let shell_only = Facts {
            model_agent_overrides: vec![],
            model_release_default: None,
            ..all
        };
        let r = resolve_model(&shell_only).expect("a model is in force");
        assert_eq!(r.id, "shell-model-20250101");
        assert!(matches!(r.source, ModelSource::Shell));
    }

    /// The inverse failure the issue names: a shell that happens to export a
    /// dated snapshot while the release still floats. Reporting only the
    /// winner would hide the disagreement entirely, so the detail has to carry
    /// both ids AND both source labels -- an id with no label is not something
    /// an operator can go and change.
    #[test]
    fn disagreement_is_named() {
        let f = Facts {
            model_shell: Some("claude-haiku-4-5-20251001".into()),
            model_release_default: Some("claude-sonnet-5".into()),
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let c = model_pin(&f);
        assert_eq!(c.state, State::Ok, "{}", c.detail);
        assert!(
            c.detail.contains("claude-sonnet-5"),
            "the in-force id: {}",
            c.detail
        );
        assert!(
            c.detail.contains("release default"),
            "the in-force source: {}",
            c.detail
        );
        assert!(
            c.detail.contains("claude-haiku-4-5-20251001"),
            "the disagreeing id: {}",
            c.detail
        );
        assert!(
            c.detail.contains("CURIE_MODEL"),
            "the disagreeing source: {}",
            c.detail
        );
    }

    /// Several agents, several models. The check must never read clean because
    /// it happened to pick the pinned one: the weakest pin is the risk the
    /// install actually carries, so `Floating` outranks `Unrecognized`
    /// outranks `Pinned`, and ties break on agent name so the report is
    /// deterministic across runs.
    #[test]
    fn the_weakest_agent_pin_is_the_one_reported() {
        let f = Facts {
            model_agent_overrides: vec![
                ("alpha".into(), "claude-haiku-4-5-20251001".into()),
                ("beta".into(), "claude-sonnet-5".into()),
            ],
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let r = resolve_model(&f).expect("a model is in force");
        assert_eq!(r.id, "claude-sonnet-5");
        assert!(
            matches!(&r.source, ModelSource::Agent(name) if name == "beta"),
            "the floating agent must be the one reported in force"
        );
        let c = model_pin(&f);
        assert_eq!(c.state, State::Ok, "{}", c.detail);
        assert!(c.fix.is_some(), "a floating name in force carries advice");
        assert!(c.detail.contains("beta"), "must name it: {}", c.detail);
        assert!(
            c.detail.contains("alpha") && c.detail.contains("claude-haiku-4-5-20251001"),
            "the other agent still disagrees and must be listed: {}",
            c.detail
        );
    }

    /// AC2's other half. `not_applicable` used to mean "the invoking shell did
    /// not export CURIE_MODEL", which is why the shipped default reported
    /// clean. It now means exactly one thing -- no source yields a model at
    /// all -- so any single source present must move the check off it.
    #[test]
    fn only_a_total_absence_is_not_applicable() {
        let one_source_each = [
            Facts {
                model_shell: Some("claude-sonnet-5".into()),
                ..Default::default()
            },
            Facts {
                model_release_default: Some("claude-sonnet-5".into()),
                ..Default::default()
            },
            Facts {
                model_agent_overrides: vec![("bot".into(), "claude-sonnet-5".into())],
                ..Default::default()
            },
        ];
        for f in one_source_each {
            let c = model_pin(&f);
            assert_ne!(
                c.state,
                State::NotApplicable,
                "a model IS determinable here, so this is not 'not applicable': {}",
                c.detail
            );
        }

        let c = model_pin(&Facts::default());
        assert_eq!(c.state, State::NotApplicable);
        for source in ["override", "agentSandbox.runner.model", "CURIE_MODEL"] {
            assert!(
                c.detail.contains(source),
                "the one not-applicable branch must say which sources were \
                 looked at, and name {source}: {}",
                c.detail
            );
        }
    }

    /// AC3. `glm-4-0520` is a fully pinned zhipu snapshot; the old catch-all
    /// asserted it floats and offered `export CURIE_MODEL=<id>-YYYYMMDD`, which
    /// produces an id that provider will reject. The honest report makes no
    /// claim about whether the id moves and carries no fix at all -- a wrong
    /// fix string is worse than none (#1813).
    #[test]
    fn an_unrecognized_id_carries_no_fix() {
        let f = Facts {
            model_release_default: Some("glm-4-0520".into()),
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let c = model_pin(&f);
        assert_eq!(c.state, State::Ok, "{}", c.detail);
        assert!(
            c.fix.is_none(),
            "an unrecognised shape must not be told to pin a date it may not \
             accept: {:?}",
            c.fix
        );
        assert!(c.detail.contains("glm-4-0520"), "{}", c.detail);
        assert!(
            c.detail.contains("does not recognise"),
            "must say plainly that the rule cannot read this shape: {}",
            c.detail
        );
        assert!(
            !c.detail.contains("is a floating name"),
            "must not assert the claim it is declining to make: {}",
            c.detail
        );
    }

    /// AC4, and the guard against re-inventing a flag. `curie cluster up` has
    /// no `--model` (that flag belongs to `skill up`); the release default is
    /// set with `--set agentSandbox.runner.model=`. A fix string naming a flag
    /// that does not exist fails for whoever pastes it, which is the exact
    /// defect #1813 was filed for. Every shape that can emit a fix is swept,
    /// so a new branch cannot slip past this.
    #[test]
    fn every_model_pin_fix_is_a_bare_runnable_command() {
        // Flags `ClusterAction` really declares for each verb. Anything else in
        // an emitted fix is invented.
        let declared = |verb: &str| -> &'static [&'static str] {
            match verb {
                "up" => &["--namespace", "--release", "--set"],
                "overrides" => &["--model"],
                other => panic!("fix names an unknown `curie cluster` verb: {other}"),
            }
        };

        let mut shapes: Vec<Facts> = Vec::new();
        for id in [
            "claude-sonnet-5",           // floating -- emits a fix
            "claude-haiku-4-5-20251001", // pinned -- must not
            "glm-4-0520",                // unrecognised -- must not
        ] {
            for target in [None, Some(("acme".to_string(), "acme-bot".to_string()))] {
                shapes.push(Facts {
                    model_agent_overrides: vec![("bot".into(), id.into())],
                    target: target.clone(),
                    ..wired()
                });
                shapes.push(Facts {
                    model_release_default: Some(id.into()),
                    target: target.clone(),
                    ..wired()
                });
                // The same source on the branch a --local-model install takes.
                // Its fix names a different key and must still be runnable.
                shapes.push(Facts {
                    model_release_default: Some(id.into()),
                    model_release_key: Some(ReleaseModelKey::Inference),
                    target: target.clone(),
                    ..wired()
                });
                shapes.push(Facts {
                    model_shell: Some(id.into()),
                    target,
                    ..wired()
                });
            }
        }

        let mut saw_a_fix = false;
        for f in shapes {
            let c = model_pin(&f);
            let Some(fix) = c.fix.as_deref() else {
                continue;
            };
            saw_a_fix = true;
            assert!(
                fix.starts_with("curie ") || fix.starts_with("export "),
                "a fix must be a command someone can paste, got {fix:?}"
            );
            assert!(
                !fix.contains('('),
                "prose in a fix string makes it unrunnable -- move it to the \
                 detail: {fix:?}"
            );
            if let Some(rest) = fix.strip_prefix("curie cluster ") {
                let verb = rest.split_whitespace().next().unwrap_or_default();
                let allowed = declared(verb);
                for flag in fix.split_whitespace().filter(|t| t.starts_with("--")) {
                    assert!(
                        allowed.contains(&flag),
                        "`curie cluster {verb}` does not declare {flag}: {fix:?}"
                    );
                }
            }
        }
        assert!(
            saw_a_fix,
            "the sweep exercised no fix-emitting shape at all"
        );
    }

    /// #1358 item 1: every other doctor fix omits the --namespace/--release the
    /// run was invoked with, so pasting one operates on curie/curie instead of
    /// the release just diagnosed. This check's fix must not repeat that.
    #[test]
    fn the_release_fix_names_the_real_namespace_and_release() {
        let f = Facts {
            model_release_default: Some("claude-sonnet-5".into()),
            target: Some(("acme".into(), "acme".into())),
            ..wired()
        };
        let fix = model_pin(&f).fix.expect("a floating default carries a fix");
        assert!(
            fix.contains("--namespace acme --release acme"),
            "the fix must target the release doctor actually looked at: {fix}"
        );
        assert!(
            fix.contains("agentSandbox.runner.model="),
            "must name the key that actually sets the release default: {fix}"
        );
    }

    /// A `curie cluster up --local-model` install. The chart IGNORES
    /// `agentSandbox.runner.model` while the in-cluster inference service is
    /// deployed -- `helm template --set inference.deploy=true --set
    /// inference.model=qwen3:4b` renders exactly one `CURIE_MODEL`, and its
    /// value is `qwen3:4b`. So the label naming the runner key would name a key
    /// that is not in force, and the fix would print a `--set` that CANNOT
    /// change the model the sandboxes boot: a command that does nothing is not
    /// a fix (AC1, AC4).
    #[test]
    fn a_local_inference_release_names_the_key_that_is_in_force() {
        let f = Facts {
            model_release_default: Some("qwen3:4b".into()),
            model_release_key: Some(ReleaseModelKey::Inference),
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let c = model_pin(&f);
        assert_eq!(c.state, State::Ok, "{}", c.detail);
        assert!(c.detail.contains("qwen3:4b"), "{}", c.detail);
        assert!(
            c.detail.contains("release default inference.model"),
            "the label must name the key the chart actually reads: {}",
            c.detail
        );
        assert!(
            !c.detail.contains("agentSandbox.runner.model"),
            "the runner key is not in force on this install and naming it \
             sends the operator to a value the chart ignores: {}",
            c.detail
        );
        let fix = c
            .fix
            .expect("qwen3:4b is a floating name and carries a fix");
        assert!(
            fix.contains("--set inference.model=<dated-snapshot-id>"),
            "the fix has to set the key in force: {fix}"
        );
        assert!(
            !fix.contains("agentSandbox.runner.model"),
            "this --set changes nothing while inference is deployed: {fix}"
        );
    }

    /// The other side of the same branch, so the fix for the ordinary install
    /// cannot regress into naming the inference key.
    #[test]
    fn a_default_release_still_names_the_runner_key() {
        let f = Facts {
            model_release_default: Some("claude-sonnet-5".into()),
            model_release_key: Some(ReleaseModelKey::Runner),
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let c = model_pin(&f);
        assert!(
            c.detail
                .contains("release default agentSandbox.runner.model"),
            "{}",
            c.detail
        );
        assert!(!c.detail.contains("inference.model"), "{}", c.detail);
        let fix = c.fix.expect("a floating default carries a fix");
        assert!(
            fix.contains("--set agentSandbox.runner.model=<dated-snapshot-id>"),
            "{fix}"
        );
        assert!(!fix.contains("--set inference.model"), "{fix}");
    }

    /// Helm decides which key is live by Go template truthiness, not by
    /// `as_bool`. A value that arrives as a string -- a generic
    /// `--set-string inference.deploy=true`, or anything that round-trips
    /// through a values file as one -- sends Helm down the inference branch,
    /// and a doctor that demanded a real boolean went down the other one and
    /// reported a model the pod does not boot.
    ///
    /// The string `"false"` being truthy looks like a bug and is not: Go calls
    /// a non-empty string non-empty whatever it spells, so the chart takes the
    /// inference branch there too. Mirroring the wrong-looking half is the
    /// whole point -- doctor has to agree with Helm, not with intuition.
    #[test]
    fn inference_deploy_follows_helm_truthiness() {
        let with_deploy = |deploy: serde_json::Value| {
            runner_model_from_values(&serde_json::json!({
                "inference": { "deploy": deploy, "model": "qwen3:4b" },
                "agentSandbox": { "runner": { "model": "claude-sonnet-5" } }
            }))
        };

        for truthy in [
            serde_json::json!(true),
            serde_json::json!("true"),
            serde_json::json!("false"),
            serde_json::json!("0"),
            serde_json::json!(1),
        ] {
            assert_eq!(
                with_deploy(truthy.clone()),
                Some(("qwen3:4b".to_string(), ReleaseModelKey::Inference)),
                "helm renders the inference branch for {truthy}, so this must too"
            );
        }

        for falsey in [
            serde_json::json!(false),
            serde_json::json!(0),
            serde_json::json!(""),
            serde_json::json!(null),
            // Go's `empty` calls an empty list and an empty map empty too, and
            // `classify_existing_secret_field` in `github_app.rs` already
            // reads them that way. The two copies of this ladder must agree.
            serde_json::json!([]),
            serde_json::json!({}),
        ] {
            assert_eq!(
                with_deploy(falsey.clone()),
                Some(("claude-sonnet-5".to_string(), ReleaseModelKey::Runner)),
                "helm skips the inference branch for {falsey}, so this must too"
            );
        }

        // Absent entirely -- the shape of every install that never asked for
        // local inference.
        assert_eq!(
            runner_model_from_values(&serde_json::json!({
                "agentSandbox": { "runner": { "model": "claude-sonnet-5" } }
            })),
            Some(("claude-sonnet-5".to_string(), ReleaseModelKey::Runner))
        );
    }

    /// The chart's SHIPPED DEFAULTS render `CURIE_FAKE_MODEL=1` and
    /// `CURIE_MODEL=claude-sonnet-5` at once, off two independent template
    /// arms (`charts/curie/templates/agent-sandbox.yaml`, verified with `helm
    /// template`). Before this, doctor read the id and announced it as the
    /// model in force from the release default, on an install whose sandboxes
    /// never send it anywhere -- the same "names a model the pod does not
    /// boot" defect #1950 exists to kill.
    ///
    /// The id stays in the detail and the state stays `Ok`: the id IS what
    /// applies the moment fake model is turned off, and the credential story
    /// belongs to the separate `model-credential` check.
    #[test]
    fn a_fake_model_release_says_the_configured_id_is_not_in_use() {
        let f = Facts {
            model_release_default: Some("claude-sonnet-5".into()),
            model_release_key: Some(ReleaseModelKey::Runner),
            model_release_fake: true,
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let c = model_pin(&f);
        assert_eq!(
            c.state,
            State::Ok,
            "a fake-model install is not broken: {}",
            c.detail
        );
        assert!(
            c.detail.contains("scripted fake model") && c.detail.contains("not in use"),
            "the detail must say the pod boots the fake model instead: {}",
            c.detail
        );
        assert!(
            c.detail.contains("claude-sonnet-5"),
            "the id is not suppressed -- it applies the moment fake model is \
             turned off: {}",
            c.detail
        );
        assert_eq!(
            c.fix.as_deref(),
            Some(
                "curie cluster up --namespace curie --release curie \
                 --set agentSandbox.runner.model=<dated-snapshot-id>"
            ),
            "the fix is unchanged by the fake-model caveat"
        );
    }

    /// The negative control for the test above: the ordinary release, boots
    /// what it names. A caveat that shows up here would tell every real
    /// install its model is not in use.
    #[test]
    fn a_real_model_release_carries_no_fake_model_clause() {
        let f = Facts {
            model_release_default: Some("claude-sonnet-5".into()),
            model_release_key: Some(ReleaseModelKey::Runner),
            model_release_fake: false,
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        assert!(
            !model_pin(&f).detail.contains("scripted fake model"),
            "{}",
            model_pin(&f).detail
        );
    }

    /// `agent-sandbox.yaml:482` gates `CURIE_FAKE_MODEL` on
    /// `and $runner.fakeModel (not .Values.inference.deploy)`, so a release
    /// deploying in-cluster inference boots the real local model even with
    /// `fakeModel: true` still sitting in its values. Reading the flag alone
    /// would caveat an install that has nothing to caveat.
    ///
    /// Asserted at the values level, because that is where the two-legged
    /// branch lives; `gather` does nothing but hand this its computed values.
    #[test]
    fn local_inference_beats_the_fake_model_flag() {
        let with = |fake: serde_json::Value, deploy: serde_json::Value| {
            release_fake_model(&serde_json::json!({
                "inference": { "deploy": deploy, "model": "qwen3:4b" },
                "agentSandbox": { "runner": { "fakeModel": fake } }
            }))
        };
        assert!(
            with(serde_json::json!(true), serde_json::json!(false)),
            "fakeModel with no inference deployed IS the fake-model shape"
        );
        assert!(
            !with(serde_json::json!(true), serde_json::json!(true)),
            "the chart omits CURIE_FAKE_MODEL when inference is deployed"
        );
        assert!(
            !with(serde_json::json!(false), serde_json::json!(false)),
            "no fakeModel, no caveat"
        );
        // Both legs are Go-truthy, not `as_bool`: a `--set-string` round trip
        // stores either as a string and Helm still takes the branch.
        assert!(
            with(serde_json::json!("true"), serde_json::json!("")),
            "a string fakeModel is truthy to Helm, so it must be here too"
        );
        // Neither key present at all -- an older release, or a values file
        // that never mentioned the runner.
        assert!(!release_fake_model(&serde_json::json!({})));
    }

    /// The caveat is about the RELEASE source only. A shell `CURIE_MODEL` is
    /// forwarded at sandbox boot whatever the chart's fake-model arm renders,
    /// so attaching the caveat to it would be a claim about a value the chart
    /// never produced.
    #[test]
    fn a_shell_model_carries_no_fake_model_clause() {
        let f = Facts {
            model_shell: Some("claude-sonnet-5".into()),
            model_release_default: None,
            model_release_key: None,
            model_release_fake: true,
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let c = model_pin(&f);
        assert!(
            !c.detail.contains("scripted fake model"),
            "the release's fake-model flag says nothing about a shell value: {}",
            c.detail
        );
    }

    /// The per-agent override outranks every other source, and `doctor` run
    /// without --api-url/--api-key cannot see it. Before this, an install with
    /// a pinned release default and an agent quietly carrying a floating
    /// override reported clean with no fix -- the exact failure this check
    /// exists to catch. `Facts::agents` already distinguishes "not reached"
    /// from "reached, none set", so the report says which one it is instead of
    /// claiming an absence it never looked for.
    #[test]
    fn an_unreadable_platform_api_is_said_out_loud() {
        let unreachable = Facts {
            model_release_default: Some("claude-haiku-4-5-20251001".into()),
            model_release_key: Some(ReleaseModelKey::Runner),
            target: Some(("curie".into(), "curie".into())),
            agents: None,
            ..wired()
        };
        let c = model_pin(&unreachable);
        assert_eq!(
            c.state,
            State::Ok,
            "absent is not broken -- the honesty belongs in the detail: {}",
            c.detail
        );
        assert!(
            c.detail.contains("could not be read"),
            "a pinned report that never looked at the highest-precedence \
             source has to say so: {}",
            c.detail
        );
        assert!(
            c.detail.contains("platform API"),
            "and say WHY it could not look: {}",
            c.detail
        );

        // Reached, and genuinely nothing set. Repeating the caveat here would
        // train the operator to ignore it on the runs where it is true.
        let reached = Facts {
            agents: Some(vec![]),
            ..unreachable.clone()
        };
        assert!(
            !model_pin(&reached).detail.contains("could not be read"),
            "the API WAS reached: {}",
            model_pin(&reached).detail
        );

        // An override in force IS the highest-precedence source, so there is
        // nothing unread to warn about.
        let from_an_agent = Facts {
            model_agent_overrides: vec![("bot".into(), "claude-sonnet-5".into())],
            ..unreachable
        };
        assert!(
            !model_pin(&from_an_agent)
                .detail
                .contains("could not be read"),
            "{}",
            model_pin(&from_an_agent).detail
        );
    }

    /// The same blindness on the not-applicable branch. "no per-agent
    /// override" is a claim, and it is false whenever the API was never
    /// reached to look.
    #[test]
    fn a_total_absence_does_not_claim_there_are_no_overrides() {
        let c = model_pin(&Facts::default());
        assert_eq!(c.state, State::NotApplicable);
        assert!(
            c.detail.contains("could not be read"),
            "must not assert an absence it never looked for: {}",
            c.detail
        );

        let reached = Facts {
            agents: Some(vec![]),
            ..Default::default()
        };
        let c = model_pin(&reached);
        assert_eq!(c.state, State::NotApplicable);
        assert!(
            c.detail.contains("no per-agent override"),
            "the API was reached, so the absence is a real observation: {}",
            c.detail
        );
    }

    /// #229's footgun, now at three sources instead of one. An agent row with
    /// `model: ""` would otherwise win precedence and resolve to Unset,
    /// producing exactly the `not_applicable` AC2 forbids -- on an install
    /// whose release default is a floating alias.
    #[test]
    fn an_empty_value_at_any_source_never_wins_precedence() {
        let f = Facts {
            model_agent_overrides: vec![("bot".into(), "   ".into())],
            model_release_default: Some(String::new()),
            model_shell: Some("claude-sonnet-5".into()),
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let r = resolve_model(&f).expect("the shell value is a real model");
        assert_eq!(r.id, "claude-sonnet-5");
        assert!(matches!(r.source, ModelSource::Shell));
        let c = model_pin(&f);
        assert_eq!(c.state, State::Ok, "{}", c.detail);
        assert!(
            !c.detail.contains("bot"),
            "an empty override is not an override and must not be reported as \
             one: {}",
            c.detail
        );

        let all_blank = Facts {
            model_agent_overrides: vec![("bot".into(), String::new())],
            model_release_default: Some("  ".into()),
            model_shell: Some("".into()),
            ..Default::default()
        };
        assert_eq!(model_pin(&all_blank).state, State::NotApplicable);
    }

    /// Two agents on the same model is the ordinary shape of a multi-agent
    /// install, not a disagreement. Listing the second one would train the
    /// operator to ignore the disagreement clause on the runs where it matters.
    #[test]
    fn several_agents_on_the_same_model_are_not_a_disagreement() {
        let f = Facts {
            model_agent_overrides: vec![
                ("beta".into(), "claude-sonnet-5".into()),
                ("alpha".into(), "claude-sonnet-5".into()),
            ],
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let r = resolve_model(&f).expect("a model is in force");
        assert_eq!(r.id, "claude-sonnet-5");
        assert!(
            matches!(&r.source, ModelSource::Agent(name) if name == "alpha"),
            "ties break on name ascending so the report is stable across runs"
        );
        assert!(
            r.disagreeing.is_empty(),
            "an identical id is not a disagreement: {:?}",
            r.disagreeing
        );
        assert!(
            !model_pin(&f).detail.contains("disagree"),
            "{}",
            model_pin(&f).detail
        );
    }

    /// When the override, the release default and the shell all agree there is
    /// nothing to append -- and a detail ending in a dangling
    /// "other sources disagree:" with nothing after it reads as a truncated
    /// report and sends someone looking for a problem that is not there.
    #[test]
    fn agreement_across_sources_leaves_no_dangling_disagreement_clause() {
        let f = Facts {
            model_agent_overrides: vec![("bot".into(), "claude-sonnet-5".into())],
            model_release_default: Some("claude-sonnet-5".into()),
            model_shell: Some("claude-sonnet-5".into()),
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let r = resolve_model(&f).expect("a model is in force");
        assert!(
            r.disagreeing.is_empty(),
            "nothing disagrees: {:?}",
            r.disagreeing
        );
        let detail = model_pin(&f).detail;
        assert!(
            !detail.contains("disagree"),
            "no disagreement clause at all when nothing disagrees: {detail}"
        );
        assert!(
            !detail.trim_end().ends_with(':'),
            "a detail must never end on a dangling clause: {detail}"
        );
    }

    /// The direction this check must NOT fail in. A correctly pinned snapshot
    /// in force is not a problem, even while a floating alias sits at a source
    /// that loses precedence: the alias is worth surfacing in the disagreement
    /// list, and emitting a fix for an install that is already pinned is how a
    /// doctor teaches people to ignore it.
    #[test]
    fn a_pinned_id_in_force_emits_no_fix_even_beside_a_floating_alias() {
        let f = Facts {
            model_agent_overrides: vec![("bot".into(), "claude-haiku-4-5-20251001".into())],
            model_release_default: Some("claude-sonnet-5".into()),
            target: Some(("curie".into(), "curie".into())),
            ..wired()
        };
        let c = model_pin(&f);
        assert_eq!(c.state, State::Ok, "{}", c.detail);
        assert!(
            c.fix.is_none(),
            "the model in force is already a dated snapshot: {:?}",
            c.fix
        );
        assert!(
            c.detail.contains("snapshot 20251001"),
            "the pinned spelling is load-bearing: {}",
            c.detail
        );
        assert!(
            c.detail.contains("claude-sonnet-5"),
            "the floating alias still has to be visible: {}",
            c.detail
        );
    }

    /// The pure half of the release-default probe: `gather()` cannot reach it
    /// without a live cluster, so the path walk is unit-tested against a
    /// hand-built computed-values document instead. Empty and absent both read
    /// as "no default observed" -- see the blank-value case above for why.
    ///
    /// It is NOT a single path. `charts/curie/templates/agent-sandbox.yaml`
    /// renders exactly one `CURIE_MODEL` env entry and picks its value on a
    /// branch: `inference.model` when `inference.deploy` is true, otherwise
    /// `agentSandbox.runner.model`. This function has to reproduce that branch
    /// or it names a model the pod never boots.
    #[test]
    fn runner_model_from_values_reads_the_chart_path() {
        let values = serde_json::json!({
            "agentSandbox": { "runner": { "model": "claude-sonnet-5" } }
        });
        assert_eq!(
            runner_model_from_values(&values),
            Some(("claude-sonnet-5".to_string(), ReleaseModelKey::Runner))
        );

        // `curie cluster up --local-model` produces exactly this shape, and the
        // sandbox then boots `qwen3:4b` against the in-cluster inference
        // service -- `agentSandbox.runner.model` is not used for the boot env
        // on this branch at all. Reporting `claude-sonnet-5` here would name a
        // model the install never runs, which is the AC1 failure.
        assert_eq!(
            runner_model_from_values(&serde_json::json!({
                "inference": { "deploy": true, "model": "qwen3:4b" },
                "agentSandbox": { "runner": { "model": "claude-sonnet-5" } }
            })),
            Some(("qwen3:4b".to_string(), ReleaseModelKey::Inference)),
            "the in-cluster inference model wins when inference.deploy is true"
        );

        // The chart's own default. `inference.model` is populated in
        // values.yaml whether or not inference is deployed, so a read that
        // ignored `deploy` would report `qwen3:4b` on every ordinary install.
        assert_eq!(
            runner_model_from_values(&serde_json::json!({
                "inference": { "deploy": false, "model": "qwen3:4b" },
                "agentSandbox": { "runner": { "model": "claude-sonnet-5" } }
            })),
            Some(("claude-sonnet-5".to_string(), ReleaseModelKey::Runner)),
            "deploy:false must not let inference.model win"
        );

        // #229's empty-value footgun on the inference branch: an empty string
        // is not a configured model anywhere else in this code, so it falls
        // through rather than resolving to Unset and reporting not-applicable
        // on an install that boots something.
        assert_eq!(
            runner_model_from_values(&serde_json::json!({
                "inference": { "deploy": true, "model": "" },
                "agentSandbox": { "runner": { "model": "claude-sonnet-5" } }
            })),
            Some(("claude-sonnet-5".to_string(), ReleaseModelKey::Runner)),
            "an empty inference.model is not a configured model"
        );

        // A local-model install need not carry an agentSandbox block at all in
        // the computed values; the inference branch stands on its own.
        assert_eq!(
            runner_model_from_values(&serde_json::json!({
                "inference": { "deploy": true, "model": "qwen3:4b" }
            })),
            Some(("qwen3:4b".to_string(), ReleaseModelKey::Inference)),
            "the inference branch must not require agentSandbox to be present"
        );

        assert_eq!(runner_model_from_values(&serde_json::json!({})), None);
        assert_eq!(
            runner_model_from_values(&serde_json::json!({
                "agentSandbox": { "runner": { "model": "" } }
            })),
            None
        );
        assert_eq!(
            runner_model_from_values(&serde_json::json!({
                "agentSandbox": { "runner": {} }
            })),
            None
        );
    }

    /// The coupling this check depends on and cannot see: doctor reads the
    /// model out of the release's computed values, and if the chart ever
    /// renames that path the check goes quietly blind again -- reporting "no
    /// model determined" on an install that boots one. Reading the shipped
    /// values.yaml means a rename breaks this test instead.
    ///
    /// The `inference.deploy` assertion is the other half: it is what makes
    /// `agentSandbox.runner.model` the live source on a default install today.
    /// If the chart ever flips that default, this branch of
    /// `runner_model_from_values` changes which key is authoritative, and the
    /// test should say so rather than the check silently reporting the wrong
    /// source.
    #[test]
    fn the_chart_still_ships_a_model_at_the_path_doctor_reads() {
        let path =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../charts/curie/values.yaml");
        let raw = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("the shipped chart values must be readable: {e}"));
        let values: serde_json::Value =
            serde_norway::from_str(&raw).expect("the shipped chart values must parse");

        assert_eq!(
            values
                .get("inference")
                .and_then(|i| i.get("deploy"))
                .and_then(serde_json::Value::as_bool),
            Some(false),
            "the chart must still default inference.deploy to false -- that is \
             what makes agentSandbox.runner.model the model a default install \
             actually boots"
        );

        let (model, key) = runner_model_from_values(&values).unwrap_or_else(|| {
            panic!(
                "the chart no longer ships a model at either path curie doctor \
                 reads: inference.model when inference.deploy, else \
                 agentSandbox.runner.model"
            )
        });
        assert!(!model.trim().is_empty(), "{model:?}");
        assert_eq!(
            key,
            ReleaseModelKey::Runner,
            "with inference.deploy false the shipped default must come from \
             agentSandbox.runner.model, which is the key the fix names"
        );
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

    /// The binding is what makes a push reach an agent. An unbound one fails
    /// SILENTLY: the webhook returns 200, GitHub shows a green delivery, and
    /// nothing is logged -- so the check has to say that out loud. This also
    /// pins the Missing versus NotApplicable distinction the verdict turns
    /// on: a KNOWN unbound agent must reach the actionable "not wired yet"
    /// summary, not the "unverified" hedge reserved for never having checked.
    #[test]
    fn an_unbound_agent_is_reported_with_what_it_costs() {
        let f = Facts {
            agents: Some(vec![
                ("bot".into(), Some("acme/bot".into())),
                ("bot-dev".into(), None),
            ]),
            ..wired()
        };
        let checks = evaluate(&f);
        let c = find(&checks, "repo-binding").clone();
        assert_eq!(c.state, State::Missing);
        assert!(c.detail.contains("bot-dev"), "must name it: {}", c.detail);
        assert!(
            c.detail.contains("silently ignored"),
            "must say the failure is silent: {}",
            c.detail
        );
        assert!(
            summary(&checks).contains("Git-push deploys are not wired yet"),
            "a KNOWN unbound agent must route to the actionable verdict, not \
             the unverified hedge: {}",
            summary(&checks)
        );
    }

    /// The advice has to be right about what CAN be fixed. An agent with no
    /// binding is bindable by a later deploy (#1194); only one already pointing
    /// at a DIFFERENT repository is left alone. Telling someone to delete and
    /// recreate an unbound agent would destroy its version history for nothing.
    #[test]
    fn the_fix_distinguishes_unbound_from_misbound() {
        let f = Facts {
            agents: Some(vec![("bot".into(), None)]),
            ..wired()
        };
        let fix = find(&evaluate(&f), "repo-binding")
            .fix
            .clone()
            .expect("must offer a fix");
        assert!(fix.contains("--repo"), "{fix}");
        assert!(
            fix.contains("NOT rebind"),
            "must be explicit that a wrong binding is not fixed this way: {fix}"
        );
    }

    /// Not reaching the API is a fact, not a failure -- doctor needs only
    /// kubectl and helm for everything else.
    #[test]
    fn an_unreachable_api_is_not_a_failure() {
        let c = find(&evaluate(&wired()), "repo-binding").clone();
        assert_eq!(c.state, State::NotApplicable);
        assert!(c.detail.contains("--api-url"), "{}", c.detail);
    }

    #[test]
    fn all_bound_agents_pass() {
        let f = Facts {
            agents: Some(vec![("bot".into(), Some("acme/bot".into()))]),
            ..wired()
        };
        assert_eq!(find(&evaluate(&f), "repo-binding").state, State::Ok);
    }

    /// Found on a real install (#1354): every other check passed, the platform
    /// API was never reached (no --api-url/--api-key), and the summary still
    /// said "Fully wired" -- asserting the one capability the run did not
    /// check. `wired()` has no `agents` set, so repo-binding is NotApplicable
    /// here, not Ok.
    #[test]
    fn unreached_api_does_not_claim_fully_wired() {
        let checks = evaluate(&wired());
        let s = summary(&checks);
        assert!(!s.contains("Fully wired"), "{s}");
        assert!(s.contains("Git-push deploys are unverified"), "{s}");
    }

    /// The sibling NotApplicable path: the platform API WAS reached but no
    /// agents are deployed yet. A different reason, the same lack of evidence
    /// that a git push deploys anything, so both paths must land on the same
    /// hedge rather than one of them slipping through to "Fully wired".
    #[test]
    fn no_agents_deployed_does_not_claim_fully_wired() {
        let f = Facts {
            agents: Some(vec![]),
            ..wired()
        };
        let checks = evaluate(&f);
        let s = summary(&checks);
        assert!(!s.contains("Fully wired"), "{s}");
        assert!(s.contains("Git-push deploys are unverified"), "{s}");
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
            agents: Some(vec![("sre-bot".into(), Some("acme/sre-bot".into()))]),
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

    // -- gather(), driven ------------------------------------------------
    //
    // Env mutation is process-global, so this reuses the save/clear/restore
    // idiom already in this crate (`cli/src/installation.rs`'s `diff_tests`)
    // rather than inventing a second one. `CURIE_MODEL` is read by
    // `installation.rs`'s planner as well as by `gather()`, so a test that
    // sets it must serialise against every other env-mutating test in the
    // crate, not just its own file's -- hence the crate-wide
    // `crate::PROCESS_ENV_LOCK` rather than a lock private to this file. The
    // lock is held for the whole test so a parallel test cannot observe the
    // mutated variable.

    struct ModelEnvRestore(Vec<(&'static str, Option<std::ffi::OsString>)>);

    impl ModelEnvRestore {
        fn clear(names: &[&'static str]) -> Self {
            let saved = names
                .iter()
                .map(|name| (*name, std::env::var_os(*name)))
                .collect();
            for name in names {
                std::env::remove_var(*name);
            }
            Self(saved)
        }

        fn set(&self, name: &str, value: &str) {
            std::env::set_var(name, value);
        }
    }

    impl Drop for ModelEnvRestore {
        fn drop(&mut self) {
            for (name, value) in &self.0 {
                match value {
                    Some(value) => std::env::set_var(*name, value),
                    None => std::env::remove_var(*name),
                }
            }
        }
    }

    /// AC5. Before this, deleting the entire model wiring from `gather()` left
    /// 20 doctor tests, 7 modelpin tests and both contract tests green --
    /// `gather()` had no coverage anywhere in the repo, so the check was
    /// judged only on facts a test handed it. This drives the real function.
    ///
    /// No cluster is needed: `gather()` returns early when
    /// `kubectl config current-context` is unavailable or empty, and both the
    /// shell read and the target record happen before that point. Setting
    /// `f.model_shell = None` or dropping the `f.target` assignment at the
    /// probe site must fail this test.
    #[tokio::test]
    async fn gather_reads_the_shell_model_and_records_the_target() {
        let _lock = crate::PROCESS_ENV_LOCK.lock().await;
        let names = [curie_aci_protocol::env_keys::CURIE_MODEL];
        let env = ModelEnvRestore::clear(&names);
        env.set(
            curie_aci_protocol::env_keys::CURIE_MODEL,
            "some-model-20250101",
        );

        let f = gather("ns", "rel", None).await;

        assert_eq!(
            f.model_shell.as_deref(),
            Some("some-model-20250101"),
            "gather() must actually read CURIE_MODEL from the environment"
        );
        assert_eq!(
            f.target,
            Some(("ns".to_string(), "rel".to_string())),
            "the namespace and release doctor was invoked with are facts about \
             the run, and the model-pin fix string is built from them"
        );
    }

    // -- gather(), driven against a stubbed cluster ----------------------
    //
    // The test above returns early at `kubectl config current-context`, so it
    // cannot see a single helm read. The release default is read from the
    // COMPUTED values (`helm get values --all`), which is the source #1950 was
    // blind to, so it needs a cluster that answers. Rather than a real one,
    // fake `kubectl`/`helm`/`docker` executables go on `PATH` -- the same
    // harness shape `cli/src/ops.rs`'s cluster-diagnosis tests already use.

    /// `kubectl` for a reachable cluster: a context name, and nothing else.
    /// The NodePort probe is deliberately left failing -- `gather()` reads an
    /// unavailable Service as "not exposed that way", which keeps this harness
    /// about the model reads.
    const KUBECTL_STUB: &str = r#"#!/bin/sh
case "$*" in
  "config current-context") printf '%s\n' 'doctor-stub-context' ;;
  *) printf 'unexpected kubectl invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#;

    /// `helm` for an installed release. The `--all` arm is the load-bearing
    /// one: those are the COMPUTED values, the only read in which a chart
    /// default the operator never supplied is visible at all.
    const HELM_STUB: &str = r#"#!/bin/sh
case "$*" in
  list*) printf '%s\n' "$CURIE_TEST_DOCTOR_HELM_LIST" ;;
  *"--all"*) printf '%s\n' "$CURIE_TEST_DOCTOR_HELM_COMPUTED" ;;
  "get values"*) printf '%s\n' "$CURIE_TEST_DOCTOR_HELM_VALUES" ;;
  *) printf 'unexpected helm invocation: %s\n' "$*" >&2; exit 64 ;;
esac
"#;

    fn write_executable(path: &std::path::Path, body: &str) {
        std::fs::write(path, body).expect("write fake cluster executable");
        use std::os::unix::fs::PermissionsExt;
        let mut permissions = std::fs::metadata(path)
            .expect("read fake cluster executable metadata")
            .permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(path, permissions).expect("make fake cluster executable runnable");
    }

    /// Fake `kubectl`, `helm` and `docker` on `PATH`, plus the variables their
    /// bodies read, so `gather()` can be driven all the way through its
    /// cluster reads without a cluster.
    ///
    /// `PATH` and those variables are process-global, so a caller must hold
    /// `crate::PROCESS_ENV_LOCK` for as long as this guard lives. Every
    /// variable is saved and restored in `Drop`, which runs on the unwind of a
    /// failed assertion exactly as it does on a clean return -- a panicking
    /// test therefore cannot leak a fake `helm` into the rest of the suite.
    /// Each variable is overwritten in place rather than cleared first, so
    /// `PATH` is never momentarily absent.
    struct StubbedCluster {
        restore: Vec<(&'static str, Option<std::ffi::OsString>)>,
        // Declared last so the stub directory is removed only after `Drop`
        // above has already taken the stubs back off `PATH`.
        _tools: tempfile::TempDir,
    }

    impl StubbedCluster {
        /// `computed` is what `helm get values --all` returns: the chart's own
        /// defaults merged with the operator's overrides, which is the read
        /// `Facts::model_release_default` is fed from.
        fn install(computed: &str) -> Self {
            let tools = tempfile::tempdir().expect("create fake cluster tool directory");
            write_executable(&tools.path().join("docker"), "#!/bin/sh\nexit 0\n");
            write_executable(&tools.path().join("kubectl"), KUBECTL_STUB);
            write_executable(&tools.path().join("helm"), HELM_STUB);

            let mut entries = vec![tools.path().to_path_buf()];
            entries.extend(std::env::split_paths(
                &std::env::var_os("PATH").unwrap_or_default(),
            ));
            let path = std::env::join_paths(entries).expect("join the stub directory onto PATH");

            let assignments: [(&'static str, std::ffi::OsString); 4] = [
                ("PATH", path),
                (
                    "CURIE_TEST_DOCTOR_HELM_LIST",
                    r#"[{"name":"rel","chart":"curie-0.6.0"}]"#.into(),
                ),
                ("CURIE_TEST_DOCTOR_HELM_COMPUTED", computed.into()),
                // What the OPERATOR supplied. Empty on purpose: the whole
                // point of the computed read is that a release whose operator
                // supplied nothing still boots a model.
                ("CURIE_TEST_DOCTOR_HELM_VALUES", "{}".into()),
            ];
            let restore = assignments
                .iter()
                .map(|(name, _)| (*name, std::env::var_os(*name)))
                .collect();
            for (name, value) in &assignments {
                std::env::set_var(name, value);
            }
            Self {
                restore,
                _tools: tools,
            }
        }
    }

    impl Drop for StubbedCluster {
        fn drop(&mut self) {
            for (name, value) in &self.restore {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }

    /// AC5, the release leg. Pins the paired
    /// `f.model_release_default` / `f.model_release_key` assignment in
    /// `gather()` that `crate::ops::fetch_release_computed_values` feeds.
    /// Deleting either half of that assignment, or switching the read back to
    /// `fetch_release_values` (the operator-supplied values, where a chart
    /// default nobody set is invisible -- the #1950 defect), must fail this
    /// test. The shell-model test above cannot catch any of that: it returns
    /// early at the kubectl context probe, before a single helm read.
    #[tokio::test]
    async fn gather_reads_the_release_default_model_from_computed_helm_values() {
        let _lock = crate::PROCESS_ENV_LOCK.lock().await;
        let _cluster = StubbedCluster::install(
            r#"{"agentSandbox":{"runner":{"model":"chart-default-model-20250101"}}}"#,
        );

        let f = gather("ns", "rel", None).await;

        assert_eq!(
            f.release,
            Some(("rel".to_string(), "curie-0.6.0".to_string())),
            "the stubbed release was never read, so gather() bailed out before \
             the model reads and the assertions below are judging nothing"
        );
        assert_eq!(
            f.model_release_default.as_deref(),
            Some("chart-default-model-20250101"),
            "gather() must record the model from the release's COMPUTED helm \
             values -- an operator who ran `curie cluster up` and never set a \
             model supplied nothing, and that default is what the sandboxes boot"
        );
        assert_eq!(
            f.model_release_key,
            Some(ReleaseModelKey::Runner),
            "the key must travel with the id: the chart reads one of two, and \
             a fix naming the other one is a command that changes nothing"
        );
    }

    /// AC5, the branch a `curie cluster up --local-model` install actually
    /// renders. `agent-sandbox.yaml` ignores `agentSandbox.runner.model` while
    /// `inference.deploy` is truthy, so both values are present in these
    /// computed values and only the inference one is in force. Pins the same
    /// `gather()` assignment as the test above, on the branch where a
    /// hard-coded `ReleaseModelKey::Runner`, a dropped key assignment, or a
    /// read that ignored `runner_model_from_values`'s precedence would all
    /// report a model the pods do not boot.
    #[tokio::test]
    async fn gather_records_the_inference_model_and_key_on_a_local_model_install() {
        let _lock = crate::PROCESS_ENV_LOCK.lock().await;
        let _cluster = StubbedCluster::install(
            r#"{"inference":{"deploy":true,"model":"local-inference-model"},"agentSandbox":{"runner":{"model":"chart-default-model-20250101"}}}"#,
        );

        let f = gather("ns", "rel", None).await;

        assert_eq!(
            f.release,
            Some(("rel".to_string(), "curie-0.6.0".to_string())),
            "the stubbed release was never read, so gather() bailed out before \
             the model reads and the assertions below are judging nothing"
        );
        assert_eq!(
            f.model_release_default.as_deref(),
            Some("local-inference-model"),
            "while inference.deploy is truthy the chart renders inference.model \
             and ignores the runner key, so reporting the runner value would \
             name a model no sandbox boots"
        );
        assert_eq!(
            f.model_release_key,
            Some(ReleaseModelKey::Inference),
            "the fix string is built from this key, and `--set \
             agentSandbox.runner.model=` on a --local-model install changes nothing"
        );
    }
}

// -- observation --------------------------------------------------------------

/// Gather the facts. Every probe is read-only and failure-tolerant: a missing
/// tool or an unreachable cluster is a fact to report, never an error to raise.
pub async fn gather(namespace: &str, release: &str, api: Option<(&str, &str)>) -> Facts {
    let mut f = Facts {
        docker_ok: probe_ok("docker", &["info"]).await,
        bundle_name: bundle_name(),
        // What this run was pointed at, so the model-pin fix names the release
        // just diagnosed rather than curie/curie (#1358 item 1).
        target: Some((namespace.to_string(), release.to_string())),
        ..Default::default()
    };

    // Names, never values: the id is the configuration, not a secret. Blank is
    // not a configured model (#229), and the filter is applied at every one of
    // the three sources so an empty value cannot win precedence.
    f.model_shell = std::env::var(curie_aci_protocol::env_keys::CURIE_MODEL)
        .ok()
        .map(|id| id.trim().to_string())
        .filter(|id| !id.is_empty());

    for name in crate::commands::MODEL_CREDENTIAL_ENV_NAMES {
        if let Ok(value) = std::env::var(name) {
            if value.is_empty() {
                continue;
            }
            f.model_credential = Some(name.to_string());
            f.model_credential_source = Some("environment".into());
            // `cluster up` binds its real-model credential from the canonical
            // CURIE_CREDENTIALS variable. Derive only its safe provider name
            // for the recovery command, then drop the credential value.
            f.model_credential_provider = (name == "CURIE_CREDENTIALS")
                .then(|| crate::ops::provider_from_credential_prefix(&value))
                .flatten();
            break;
        }
        if crate::secrets::is_saved(name).unwrap_or(false) {
            f.model_credential = Some(name.to_string());
            f.model_credential_source = Some("curie secrets".into());
            break;
        }
    }

    // Optional: everything else needs only kubectl and helm, so an absent or
    // unreachable API narrows the report rather than failing it.
    if let Some((url, key)) = api {
        if let Ok(client) = crate::api::ApiClient::new(url, key) {
            if let Ok(agents) = client.list_agents().await {
                // One pass, two facts. The `(name, repo_full_name)` collection
                // is the repo-binding check's input and stays exactly as it
                // was; the per-agent model was previously discarded here, which
                // is why the highest-precedence source was invisible (#1950).
                let mut bindings = Vec::with_capacity(agents.len());
                for a in agents {
                    if let Some(model) = a
                        .model
                        .as_deref()
                        .map(str::trim)
                        .filter(|model| !model.is_empty())
                    {
                        f.model_agent_overrides
                            .push((a.name.clone(), model.to_string()));
                    }
                    bindings.push((a.name, a.repo_full_name));
                }
                f.agents = Some(bindings);
            }
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

    // Two SEPARATE reads, deliberately, issued CONCURRENTLY.
    //
    // Separate: `fetch_release_values` reports only what the operator supplied,
    // so an operator who never set a model has nothing to read there and the
    // chart default the sandboxes boot is invisible -- the #1950 defect.
    // `--all` returns the computed values. The two stay apart because switching
    // slack_configured, api.ingress.* or clone_credential onto computed values
    // would silently change three unrelated checks.
    //
    // Concurrent: neither consumes the other's output and both depend only on
    // `common`, so awaiting them in turn made every cluster run pay two helm
    // spawns and two API-server round trips back to back for nothing. Each
    // result keeps its own failure-tolerant handling below.
    let (computed, values) = tokio::join!(
        crate::ops::fetch_release_computed_values(&common),
        crate::ops::fetch_release_values(&common),
    );

    if let Ok(Some(computed)) = computed {
        // The key travels with the id: the chart reads one of two, and a fix
        // naming the other one is a command that changes nothing.
        if let Some((model, key)) = runner_model_from_values(&computed) {
            f.model_release_default = Some(model);
            f.model_release_key = Some(key);
        }
        // Off the SAME read, never a third helm call: whether that id is the
        // one the pod actually boots.
        f.model_release_fake = release_fake_model(&computed);
    }

    if let Ok(Some(values)) = values {
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

/// The kubectl read behind `api_nodeport`, extracted pure so the Service NAME
/// it asks for is unit-testable without a cluster or a child process (#1533).
///
/// The chart renders the API Service as `{{ include "curie.fullname" . }}-api`,
/// so a `{release}-api` guess reads nothing and `cluster_facts` renders "API
/// not exposed" -- a FALSE readiness verdict from a doctor that exists to
/// prevent exactly that.
fn api_nodeport_command(
    namespace: &str,
    fullname: &crate::ops::ReleaseFullname,
) -> crate::ops::OpsCommand {
    crate::ops::OpsCommand::new(
        "kubectl",
        vec![
            crate::ops::plain("get"),
            crate::ops::plain("svc"),
            crate::ops::plain(fullname.resource("api")),
            crate::ops::plain("-n"),
            crate::ops::plain(namespace),
            crate::ops::plain("-o"),
            crate::ops::plain("jsonpath={.spec.ports[?(@.nodePort)].nodePort}"),
        ],
    )
}

/// The API Service's nodePort, when it is exposed that way.
///
/// Resolves the rendered fullname here rather than at `cluster_facts`' entry:
/// this is the only branch that needs it (the ingress branch returns before
/// reaching it), so an ingress install pays no extra kubectl round-trip.
async fn api_nodeport(namespace: &str, release: &str) -> Option<String> {
    let fullname = crate::ops::release_fullname(namespace, release).await;
    let (ok, out, _) = capture_cmd(&api_nodeport_command(namespace, &fullname)).await;
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
    capture_cmd(&cmd).await
}

/// Run an already-built [`crate::ops::OpsCommand`] and read it the way `doctor`
/// reads everything: a spawn failure is indistinguishable from the command
/// failing, because either way the fact being probed could not be established.
/// One spelling of that fallback, so a caller cannot accidentally treat a spawn
/// failure as a real answer.
async fn capture_cmd(cmd: &crate::ops::OpsCommand) -> (bool, String, String) {
    crate::ops::run_capture(cmd)
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
            "guidance": guidance(&self.checks),
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
        if let Some(hint) = guidance(&self.checks) {
            ui.payload_plain(&hint);
        }
    }
}

pub async fn doctor(namespace: &str, release: &str, api: Option<(&str, &str)>) -> DoctorOutput {
    let checks = evaluate(&gather(namespace, release, api).await);
    let summary = summary(&checks);
    DoctorOutput { checks, summary }
}
