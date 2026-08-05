//! `curie.yaml`: one file declares an installation (ADR-0097).
//!
//! This module is the ONLY parser for that file, mirroring the rule ADR-0089
//! set for `deploy.yaml`. The difference is which side owns it: `deploy.yaml`
//! and `connectors.yaml` describe a *bundle* and are read by the API and the
//! worker, while `curie.yaml` describes an *installation* -- something only the
//! CLI performs. It must not become a second thing the API also reads.
//!
//! Two properties are load-bearing rather than stylistic:
//!
//! - **Secret NAMES only, never values.** The file is committed. A `curie.yaml`
//!   that could carry a token would be strictly worse than the flags it
//!   replaces, so every credential field here names an environment variable or
//!   a `curie secrets` entry, and resolution happens at apply time.
//! - **Unknown keys are an error.** A config file that silently ignores a typo
//!   is a config file that lies about what it applied. `deny_unknown_fields`
//!   everywhere is the whole reason to prefer a schema over a `--set` bag.

use std::collections::BTreeMap;
use std::path::Path;

use anyhow::{bail, Context, Result};
use serde::Deserialize;

/// The parsed file. `version` is required and checked, so a future incompatible
/// schema can be rejected by an old binary rather than half-applied by it.
#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Installation {
    pub version: u32,
    pub install: Install,
    #[serde(default)]
    pub platform: Platform,
    #[serde(default)]
    pub credentials: Credentials,
    #[serde(default)]
    pub comms: Comms,
    /// Verbatim `helm --set key=value` escape hatch, for anything this schema
    /// does not model yet. Present deliberately: without it, adopting the file
    /// would mean giving up settings that flags can express, and nobody would
    /// adopt it.
    #[serde(default)]
    pub set: BTreeMap<String, String>,
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Install {
    pub namespace: String,
    pub release: String,
}

#[derive(Debug, Clone, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Platform {
    /// `None` leaves the chart default alone; `Some(false)` turns the component
    /// off. Tri-state on purpose -- a plain `bool` would make "unmentioned"
    /// indistinguishable from "explicitly false" and quietly rewrite defaults.
    #[serde(default)]
    pub ui: Option<bool>,
    #[serde(default)]
    pub inference: Option<bool>,
    /// Named providers, resolved to narrow host CIDRs at install time. Named
    /// hosts rather than hand-written CIDRs because the allowlist is a security
    /// control, and a hand-copied range is how it silently goes wrong.
    #[serde(default)]
    pub egress: Vec<Egress>,
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Egress {
    pub host: String,
}

#[derive(Debug, Clone, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Credentials {
    /// NAME of the env var / secret holding the model credential.
    #[serde(default)]
    pub model: Option<String>,
    /// NAME of the env var / secret holding a GitHub token.
    #[serde(default)]
    pub github_token: Option<String>,
    /// Declared but not yet applied by `curie apply` -- see [`Installation::validate`].
    #[serde(default)]
    pub github_app: Option<GithubApp>,
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct GithubApp {
    pub id: String,
    pub private_key: String,
}

#[derive(Debug, Clone, Default, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Comms {
    #[serde(default)]
    pub slack: Option<Slack>,
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Slack {
    /// NAME of the env var / secret holding the `xapp-` app token.
    pub app_token: String,
    /// NAME of the env var / secret holding the `xoxb-` bot token.
    pub bot_token: String,
}

/// The only schema version this binary understands.
pub const SUPPORTED_VERSION: u32 = 1;

impl Installation {
    /// Parse and validate, naming the file in every error so a schema mistake
    /// reads like a compiler message rather than a serde dump.
    pub fn load(path: &Path) -> Result<Self> {
        let raw =
            std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        Self::parse(&raw).with_context(|| format!("in {}", path.display()))
    }

    pub fn parse(raw: &str) -> Result<Self> {
        let parsed: Self = serde_norway::from_str(raw)?;
        parsed.validate()?;
        Ok(parsed)
    }

    fn validate(&self) -> Result<()> {
        if self.version != SUPPORTED_VERSION {
            bail!(
                "unsupported version {}: this curie understands version {}. \
                 Upgrade the CLI, or pin the file to the version it was written for.",
                self.version,
                SUPPORTED_VERSION
            );
        }
        if self.install.namespace.trim().is_empty() {
            bail!("install.namespace must not be empty");
        }
        if self.install.release.trim().is_empty() {
            bail!("install.release must not be empty");
        }
        for e in &self.platform.egress {
            if e.host.trim().is_empty() {
                bail!("platform.egress[].host must not be empty");
            }
        }
        // Refuse rather than ignore. A declared-but-unapplied credential is the
        // failure this whole ADR exists to prevent: the file would say the App
        // is configured while the cluster disagreed, which is exactly the
        // half-configured state #1262 had to add a warning for.
        if self.credentials.github_app.is_some() {
            bail!(
                "credentials.github_app is not applied by `curie apply` yet. \
                 Use `curie cluster github-app` for now, and remove the key -- \
                 leaving it here would claim an identity the cluster does not have."
            );
        }
        Self::reject_secret_shaped(&self.credentials.model, "credentials.model")?;
        Self::reject_secret_shaped(&self.credentials.github_token, "credentials.github_token")?;
        if let Some(slack) = &self.comms.slack {
            Self::reject_secret_shaped(&Some(slack.app_token.clone()), "comms.slack.app_token")?;
            Self::reject_secret_shaped(&Some(slack.bot_token.clone()), "comms.slack.bot_token")?;
        }
        Ok(())
    }

    /// Catch the single most likely misuse: pasting the token instead of naming
    /// the variable that holds it. This file gets committed, so a value here is
    /// a leak, and the shapes are recognisable enough to refuse by prefix.
    ///
    /// Deliberately a prefix check, not a heuristic on entropy or length: a
    /// false positive would reject a legitimate variable name, and this is a
    /// guard rail, not the security boundary. `gitleaks` in CI remains that.
    fn reject_secret_shaped(value: &Option<String>, field: &str) -> Result<()> {
        const SECRET_PREFIXES: &[&str] = &["sk-", "xoxb-", "xapp-", "ghp_", "github_pat_"];
        let Some(v) = value else { return Ok(()) };
        let trimmed = v.trim();
        if trimmed.is_empty() {
            bail!("{field} must name a variable, not be empty");
        }
        for prefix in SECRET_PREFIXES {
            if trimmed.starts_with(prefix) {
                bail!(
                    "{field} looks like a secret VALUE (starts with `{prefix}`), not the \
                     NAME of a variable holding one. This file is committed -- put the \
                     value in the environment or `curie secrets set`, and name it here."
                );
            }
        }
        Ok(())
    }

    /// The `--set key=value` tokens this file implies, in a stable order so a
    /// plan diff is readable and a test can pin it.
    ///
    /// Platform toggles render before the `set:` escape hatch so an explicit
    /// `set:` entry wins on a later-key-wins reading, matching how a trailing
    /// `--set` beats an earlier one on the helm command line.
    pub fn helm_sets(&self) -> Vec<String> {
        let mut out = Vec::new();
        if let Some(ui) = self.platform.ui {
            out.push(format!("ui.deploy={ui}"));
        }
        if let Some(inference) = self.platform.inference {
            out.push(format!("inference.deploy={inference}"));
        }
        for (key, value) in &self.set {
            out.push(format!("{key}={value}"));
        }
        out
    }

    /// Provider names for `--allow-egress-host`, validated downstream by
    /// `ops::parse_egress_provider` so an unknown host is one error message,
    /// not two divergent ones.
    pub fn egress_hosts(&self) -> Vec<String> {
        self.platform
            .egress
            .iter()
            .map(|e| e.host.clone())
            .collect()
    }

    /// Every credential NAME this file references, for a single up-front
    /// resolution pass. Ordered and de-duplicated so the "missing:" list in an
    /// error reads the same way twice.
    pub fn credential_names(&self) -> Vec<String> {
        let mut names = Vec::new();
        let mut push = |n: Option<&String>| {
            if let Some(n) = n {
                if !names.contains(n) {
                    names.push(n.clone());
                }
            }
        };
        push(self.credentials.model.as_ref());
        push(self.credentials.github_token.as_ref());
        if let Some(slack) = &self.comms.slack {
            push(Some(&slack.app_token));
            push(Some(&slack.bot_token));
        }
        names
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minimal() -> &'static str {
        "version: 1\ninstall:\n  namespace: acme\n  release: acme\n"
    }

    #[test]
    fn parses_a_minimal_file() {
        let cfg = Installation::parse(minimal()).expect("minimal file should parse");
        assert_eq!(cfg.install.namespace, "acme");
        assert_eq!(cfg.install.release, "acme");
        assert!(cfg.helm_sets().is_empty(), "no platform toggles declared");
        assert!(cfg.credential_names().is_empty());
    }

    /// The reason to have a schema at all: a typo must not be silently dropped.
    #[test]
    fn unknown_keys_are_rejected_at_every_level() {
        let cases = [
            ("version: 1\ninstall:\n  namespace: a\n  release: a\nplatfrom: {}\n", "top level"),
            ("version: 1\ninstall:\n  namespace: a\n  release: a\n  nmespace: b\n", "install"),
            (
                "version: 1\ninstall:\n  namespace: a\n  release: a\nplatform:\n  ui_deploy: false\n",
                "platform",
            ),
            (
                "version: 1\ninstall:\n  namespace: a\n  release: a\ncomms:\n  slak: {}\n",
                "comms",
            ),
        ];
        for (raw, where_) in cases {
            let err = Installation::parse(raw).expect_err(&format!("{where_}: typo must fail"));
            assert!(
                format!("{err:#}").contains("unknown field"),
                "{where_}: error should name the unknown field: {err:#}"
            );
        }
    }

    /// The file is committed. A pasted token must never survive parsing.
    #[test]
    fn a_pasted_secret_value_is_refused() {
        let cases = [
            (
                "credentials:\n  model: sk-ant-api03-realtoken\n",
                "credentials.model",
            ),
            (
                "comms:\n  slack:\n    app_token: xapp-1-A-B-c\n    bot_token: BOT\n",
                "comms.slack.app_token",
            ),
            (
                "comms:\n  slack:\n    app_token: APP\n    bot_token: xoxb-11-22-zz\n",
                "comms.slack.bot_token",
            ),
            (
                "credentials:\n  github_token: ghp_abcdefghijklmnop\n",
                "credentials.github_token",
            ),
        ];
        for (tail, field) in cases {
            let raw = format!("{}{tail}", minimal());
            let err = Installation::parse(&raw).expect_err(&format!("{field}: must refuse"));
            let msg = format!("{err:#}");
            assert!(msg.contains(field), "error should name {field}: {msg}");
            assert!(
                msg.contains("NAME of a variable"),
                "error should say what to do instead: {msg}"
            );
        }
    }

    /// A NAME that merely looks ordinary must still be accepted -- the guard
    /// must not be so eager that it blocks legitimate files.
    #[test]
    fn ordinary_variable_names_are_accepted() {
        let raw = format!(
            "{}credentials:\n  model: ANTHROPIC_API_KEY\ncomms:\n  slack:\n    \
             app_token: SLACK_APP_TOKEN\n    bot_token: SLACK_BOT_TOKEN\n",
            minimal()
        );
        let cfg = Installation::parse(&raw).expect("plain names must parse");
        assert_eq!(
            cfg.credential_names(),
            vec!["ANTHROPIC_API_KEY", "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"]
        );
    }

    /// Declared-but-unapplied is the exact half-configured state #1262 had to
    /// add a runtime warning for. Refuse it at parse time instead.
    #[test]
    fn github_app_is_refused_rather_than_ignored() {
        let raw = format!(
            "{}credentials:\n  github_app:\n    id: GITHUB_APP_ID\n    \
             private_key: GITHUB_APP_PRIVATE_KEY\n",
            minimal()
        );
        let err = Installation::parse(&raw).expect_err("must not silently ignore");
        let msg = format!("{err:#}");
        assert!(
            msg.contains("cluster github-app"),
            "must point at the verb that does work today: {msg}"
        );
    }

    #[test]
    fn version_must_match() {
        let err = Installation::parse("version: 2\ninstall:\n  namespace: a\n  release: a\n")
            .expect_err("a future version must not be half-applied");
        assert!(format!("{err:#}").contains("unsupported version 2"));
    }

    /// Tri-state: unmentioned must not silently rewrite a chart default.
    #[test]
    fn an_unmentioned_toggle_emits_no_set() {
        let raw = format!("{}platform:\n  ui: false\n", minimal());
        let cfg = Installation::parse(&raw).unwrap();
        assert_eq!(
            cfg.helm_sets(),
            vec!["ui.deploy=false"],
            "inference was never mentioned, so it must not appear"
        );
    }

    #[test]
    fn explicit_set_entries_render_after_platform_toggles() {
        let raw = format!(
            "{}platform:\n  ui: false\nset:\n  security.gvisor.mode: \"off\"\n",
            minimal()
        );
        let cfg = Installation::parse(&raw).unwrap();
        assert_eq!(
            cfg.helm_sets(),
            vec!["ui.deploy=false", "security.gvisor.mode=off"],
            "a later --set wins on the helm command line, so escape-hatch keys go last"
        );
    }

    #[test]
    fn egress_hosts_are_named_not_cidrs() {
        let raw = format!(
            "{}platform:\n  egress:\n    - host: anthropic\n    - host: slack\n",
            minimal()
        );
        let cfg = Installation::parse(&raw).unwrap();
        assert_eq!(cfg.egress_hosts(), vec!["anthropic", "slack"]);
    }

    #[test]
    fn empty_required_scalars_are_rejected() {
        for (raw, field) in [
            ("version: 1\ninstall:\n  namespace: \"\"\n  release: a\n", "namespace"),
            ("version: 1\ninstall:\n  namespace: a\n  release: \"\"\n", "release"),
            (
                "version: 1\ninstall:\n  namespace: a\n  release: a\nplatform:\n  egress:\n    - host: \"\"\n",
                "host",
            ),
        ] {
            let err = Installation::parse(raw).expect_err(&format!("empty {field} must fail"));
            assert!(format!("{err:#}").contains(field), "should name {field}");
        }
    }

    /// The ADR's own example must parse, minus the one key it documents as
    /// deferred -- otherwise the decision and the code disagree.
    #[test]
    fn the_adr_example_parses() {
        let raw = "\
version: 1

install:
  namespace: acme-bot
  release: acme-bot

platform:
  ui: false
  inference: false
  egress:
    - host: anthropic
    - host: slack

credentials:
  model: ANTHROPIC_API_KEY

comms:
  slack:
    app_token: SLACK_APP_TOKEN
    bot_token: SLACK_BOT_TOKEN
";
        let cfg = Installation::parse(raw).expect("the ADR example must parse");
        assert_eq!(cfg.install.namespace, "acme-bot");
        assert_eq!(
            cfg.helm_sets(),
            vec!["ui.deploy=false", "inference.deploy=false"]
        );
        assert_eq!(cfg.egress_hosts(), vec!["anthropic", "slack"]);
        assert_eq!(
            cfg.credential_names(),
            vec!["ANTHROPIC_API_KEY", "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"]
        );
    }
}

// ---------------------------------------------------------------------------
// apply
// ---------------------------------------------------------------------------

/// Resolve one credential NAME to its value: the process environment first,
/// then Curie private storage. Mirrors `commands::secret_store_env`'s order --
/// shell env beats the vault -- so `curie apply` and `curie skill up` disagree
/// about nothing.
fn resolve_credential(name: &str) -> Result<Option<String>> {
    if let Ok(value) = std::env::var(name) {
        if !value.is_empty() {
            return Ok(Some(value));
        }
    }
    if crate::secrets::is_saved(name)? {
        return crate::secrets::get_value(name);
    }
    Ok(None)
}

/// Every name this file references, resolved in one pass.
///
/// Reports **all** missing names at once rather than failing on the first. An
/// operator setting up a new install is typically missing several, and a
/// one-at-a-time gauntlet is the difference between one round trip and four.
pub fn resolve_credentials(
    cfg: &Installation,
    resolver: &dyn Fn(&str) -> Result<Option<String>>,
) -> Result<BTreeMap<String, String>> {
    let mut resolved = BTreeMap::new();
    let mut missing = Vec::new();
    for name in cfg.credential_names() {
        match resolver(&name)? {
            Some(value) => {
                resolved.insert(name, value);
            }
            None => missing.push(name),
        }
    }
    if !missing.is_empty() {
        bail!(
            "curie.yaml names credential(s) with no value available: {}. \
             Export each in the environment, or save it with `curie secrets set <NAME>`. \
             The file names them; it never carries their values.",
            missing.join(", ")
        );
    }
    Ok(resolved)
}

/// What `curie apply` did, as one object (the `--json` contract allows exactly
/// one). `apply` drives two underlying verbs, so their outputs are composed
/// here rather than each emitting its own.
#[derive(Debug)]
pub enum ApplyOutput {
    DryRun(crate::ui::DryRunPlan),
    Applied {
        namespace: String,
        release: String,
        comms: bool,
    },
}

impl crate::ui::CliOutput for ApplyOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            ApplyOutput::DryRun(plan) => {
                <crate::ui::DryRunPlan as crate::ui::CliOutput>::to_json(plan)
            }
            ApplyOutput::Applied {
                namespace,
                release,
                comms,
            } => serde_json::json!({
                "applied": true,
                "namespace": namespace,
                "release": release,
                "comms": comms,
            }),
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            ApplyOutput::DryRun(plan) => {
                <crate::ui::DryRunPlan as crate::ui::CliOutput>::render(plan, ui)
            }
            ApplyOutput::Applied {
                namespace,
                release,
                comms,
            } => {
                ui.payload_plain(&format!("applied {release} in {namespace}"));
                if *comms {
                    ui.payload_plain("slack comms configured");
                }
            }
        }
    }
}

pub struct ApplyOpts {
    pub cfg: Installation,
    pub chart: String,
    pub dry_run: bool,
}

/// Converge the cluster to the file.
///
/// The ordering -- platform install, THEN comms -- is handled here rather than
/// asked of the operator. That ordering is load-bearing (a `cluster up` has
/// historically dropped what `comms` configured, #1256) and until now lived
/// only as a sentence in a runbook, which is exactly what ADR-0097 set out to
/// fix: the interface could not express it, so prose had to.
pub async fn apply(opts: ApplyOpts) -> Result<ApplyOutput> {
    let ApplyOpts {
        cfg,
        chart,
        dry_run,
    } = opts;

    // Resolve BEFORE mutating anything. A missing Slack token discovered after
    // the platform install would leave a half-applied cluster, which is the
    // state this whole file exists to make unreachable.
    let resolved = resolve_credentials(&cfg, &resolve_credential)?;

    let common = crate::ops::CommonOpts {
        namespace: cfg.install.namespace.clone(),
        release: cfg.install.release.clone(),
        dry_run,
    };

    let up_out = crate::ops::up(
        crate::ops::UpOpts {
            common: common.clone(),
            chart: chart.clone(),
            no_expose: false,
            set: cfg.helm_sets(),
            allow_egress_host: cfg.egress_hosts(),
            resolved_egress_cidrs: vec![],
            allow_web_egress: vec![],
            fake_model: cfg.credentials.model.is_none(),
            credentials: cfg
                .credentials
                .model
                .as_ref()
                .and_then(|n| resolved.get(n).cloned()),
            local_model: None,
            model: std::env::var("CURIE_MODEL").ok().filter(|s| !s.is_empty()),
            secrets: vec![],
            github_token: crate::ops::GithubTokenPlan::Untouched,
            dev: false,
        },
        cfg.credentials
            .github_token
            .as_ref()
            .and_then(|n| resolved.get(n).cloned()),
        false,
    )
    .await?;

    let mut lines = match up_out {
        crate::ops::ClusterUpOutput::DryRun(plan) => plan.lines,
        crate::ops::ClusterUpOutput::Up { .. } => vec![],
    };

    let mut configured_comms = false;
    if let Some(slack) = &cfg.comms.slack {
        let comms_out = crate::comms::comms(crate::comms::CommsOpts {
            common,
            chart,
            app_token: resolved.get(&slack.app_token).cloned().unwrap_or_default(),
            bot_token: resolved.get(&slack.bot_token).cloned().unwrap_or_default(),
            disconnect: false,
        })
        .await?;
        configured_comms = true;
        if let crate::comms::CommsOutput::DryRun(plan) = comms_out {
            lines.extend(plan.lines);
        }
    }

    if dry_run {
        return Ok(ApplyOutput::DryRun(crate::ui::DryRunPlan { lines }));
    }
    Ok(ApplyOutput::Applied {
        namespace: cfg.install.namespace,
        release: cfg.install.release,
        comms: configured_comms,
    })
}

#[cfg(test)]
mod apply_tests {
    use super::*;

    fn cfg_with_all_names() -> Installation {
        Installation::parse(
            "version: 1\ninstall:\n  namespace: a\n  release: a\n\
             credentials:\n  model: MODEL_KEY\n\
             comms:\n  slack:\n    app_token: APP_TOK\n    bot_token: BOT_TOK\n",
        )
        .unwrap()
    }

    /// One round trip, not four. An operator standing up a new install is
    /// usually missing several at once.
    #[test]
    fn every_missing_credential_is_reported_together() {
        let cfg = cfg_with_all_names();
        let err = resolve_credentials(&cfg, &|_| Ok(None)).expect_err("must refuse");
        let msg = format!("{err:#}");
        for name in ["MODEL_KEY", "APP_TOK", "BOT_TOK"] {
            assert!(msg.contains(name), "{name} must be listed: {msg}");
        }
    }

    /// A partially-resolved set must still fail, and name only what is absent.
    #[test]
    fn a_partial_resolution_names_only_what_is_missing() {
        let cfg = cfg_with_all_names();
        let err = resolve_credentials(&cfg, &|n| {
            Ok((n == "MODEL_KEY").then(|| "value".to_string()))
        })
        .expect_err("still incomplete");
        let msg = format!("{err:#}");
        assert!(
            !msg.contains("MODEL_KEY"),
            "resolved name must not be listed: {msg}"
        );
        assert!(msg.contains("APP_TOK") && msg.contains("BOT_TOK"), "{msg}");
    }

    #[test]
    fn a_fully_resolved_file_yields_every_value() {
        let cfg = cfg_with_all_names();
        let resolved =
            resolve_credentials(&cfg, &|n| Ok(Some(format!("{n}-value")))).expect("all present");
        assert_eq!(
            resolved.get("MODEL_KEY").map(String::as_str),
            Some("MODEL_KEY-value")
        );
        assert_eq!(
            resolved.get("APP_TOK").map(String::as_str),
            Some("APP_TOK-value")
        );
        assert_eq!(
            resolved.get("BOT_TOK").map(String::as_str),
            Some("BOT_TOK-value")
        );
    }

    /// A file naming nothing must resolve to nothing rather than erroring --
    /// the sealed/fake-model install is a legitimate shape.
    #[test]
    fn a_file_naming_no_credentials_resolves_empty() {
        let cfg =
            Installation::parse("version: 1\ninstall:\n  namespace: a\n  release: a\n").unwrap();
        let resolved = resolve_credentials(&cfg, &|_| {
            panic!("resolver must not be called when nothing is named")
        })
        .expect("no names, no error");
        assert!(resolved.is_empty());
    }

    /// The JSON contract is one object per invocation (#456).
    #[test]
    fn applied_output_is_one_json_object() {
        use crate::ui::CliOutput;
        let out = ApplyOutput::Applied {
            namespace: "acme".into(),
            release: "acme".into(),
            comms: true,
        };
        let json = out.to_json();
        assert_eq!(json["applied"], serde_json::json!(true));
        assert_eq!(json["namespace"], serde_json::json!("acme"));
        assert_eq!(json["comms"], serde_json::json!(true));
    }
}
