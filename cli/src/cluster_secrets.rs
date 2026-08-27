//! Cluster-tier per-agent connector secret binding (#1488, ADR-0009).
//!
//! Values go to the per-agent Helm Secret through a private values file (never
//! argv). The agent record stores a names-only placeholder so the worker can
//! route claims to the per-agent pool without keeping the secret material in
//! Postgres. Rotation deletes SandboxClaims labeled for that agent; sandbox
//! pods are not Deployments, so there is no `rollout restart` of claimed
//! sandboxes.

use std::collections::BTreeMap;

use anyhow::{bail, Result};

use crate::docker::CONNECTOR_AGENT_LABEL_KEY;
use crate::ops::{plain, require_on_path, run_step, CmdArg, CommonOpts, OpsCommand};

/// Placeholder stored on the agent record at the cluster tier. Non-empty so the
/// API validator accepts it; not the secret material. The k8s substrate strips
/// these keys off the claim; the template secretKeyRef delivers the real value.
pub const CLUSTER_SECRET_PLACEHOLDER: &str = "secretKeyRef";

/// Chart agent names match templates/agent-sandbox.yaml.
fn validate_agent_resource_name(agent: &str) -> Result<()> {
    let valid = agent.len() <= 40
        && agent
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        && agent.starts_with(|c: char| c.is_ascii_lowercase() || c.is_ascii_digit())
        && agent.ends_with(|c: char| c.is_ascii_lowercase() || c.is_ascii_digit());
    if !valid {
        bail!(
            "agent name {agent:?} is not a valid per-agent Secret key (lowercase DNS label, max 40 characters)"
        );
    }
    Ok(())
}

/// Resolve `--secret` NAMES the same way `deploy` does: env, then the host vault.
pub fn resolve_named_secrets(names: &[String]) -> Result<BTreeMap<String, String>> {
    let mut secrets = BTreeMap::new();
    for name in names {
        let value = std::env::var(name)
            .ok()
            .filter(|v| !v.is_empty())
            .or(crate::secrets::get_value(name)?);
        match value {
            Some(v) => {
                secrets.insert(name.clone(), v);
            }
            None => {
                return Err(crate::exit::usage(format!(
                    "--secret {name}: not set in the environment and not saved in Curie \
                     storage; export it or run `curie secrets set {name}` first"
                )));
            }
        }
    }
    Ok(secrets)
}

/// Names-only map written to the agent record at the cluster tier.
pub fn agent_record_secrets(secrets: &BTreeMap<String, String>) -> BTreeMap<String, String> {
    secrets
        .keys()
        .map(|name| (name.clone(), CLUSTER_SECRET_PLACEHOLDER.to_string()))
        .collect()
}

/// Dotted helm keys for `agentSandbox.connectorSecrets.<agent>.<NAME>`.
pub fn helm_secret_pairs(
    agent: &str,
    secrets: &BTreeMap<String, String>,
) -> Result<Vec<(String, String)>> {
    validate_agent_resource_name(agent)?;
    Ok(secrets
        .iter()
        .map(|(name, value)| {
            (
                format!("agentSandbox.connectorSecrets.{agent}.{name}"),
                value.clone(),
            )
        })
        .collect())
}

pub struct BindOpts {
    pub common: CommonOpts,
    pub chart: String,
    pub agent: String,
    pub secrets: BTreeMap<String, String>,
}

/// helm upgrade --reuse-values with a private values file, then replace the
/// agent's claimed sandboxes so secretKeyRef env is re-resolved at pod start.
pub fn bind_commands(opts: &BindOpts) -> Result<Vec<OpsCommand>> {
    let pairs = helm_secret_pairs(&opts.agent, &opts.secrets)?;
    if pairs.is_empty() {
        return Ok(Vec::new());
    }
    Ok(vec![
        OpsCommand::new(
            "helm",
            vec![
                plain("upgrade"),
                plain(&opts.common.release),
                plain(&opts.chart),
                plain("-n"),
                plain(&opts.common.namespace),
                plain("--reuse-values"),
                CmdArg::SecretValuesFile(pairs),
            ],
        ),
        OpsCommand::new(
            "kubectl",
            vec![
                plain("-n"),
                plain(&opts.common.namespace),
                plain("delete"),
                plain("sandboxclaim"),
                plain("-l"),
                plain(format!("{CONNECTOR_AGENT_LABEL_KEY}={}", opts.agent)),
                plain("--wait=true"),
                plain("--ignore-not-found=true"),
            ],
        ),
    ])
}

pub async fn bind(opts: BindOpts) -> Result<()> {
    let cmds = bind_commands(&opts)?;
    if cmds.is_empty() {
        return Ok(());
    }
    require_on_path("helm")?;
    require_on_path("kubectl")?;
    let ui = crate::ui::ui();
    let cl = ui.checklist();
    let label = format!(
        "binding connector secrets for agent {} on release {}",
        opts.agent, opts.common.release
    );
    for cmd in &cmds {
        run_step(&cl, &label, "bound", cmd).await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn secrets() -> BTreeMap<String, String> {
        BTreeMap::from([
            ("GITHUB_PERSONAL_ACCESS_TOKEN".into(), "ghp_agent_a".into()),
            ("JIRA_TOKEN".into(), "jira-a".into()),
        ])
    }

    #[test]
    fn agent_record_keeps_names_and_drops_values() {
        let stored = agent_record_secrets(&secrets());
        assert_eq!(
            stored["GITHUB_PERSONAL_ACCESS_TOKEN"],
            CLUSTER_SECRET_PLACEHOLDER
        );
        assert_eq!(stored["JIRA_TOKEN"], CLUSTER_SECRET_PLACEHOLDER);
        assert!(!stored
            .values()
            .any(|v| v.contains("ghp_") || v.contains("jira-a")));
    }

    #[test]
    fn helm_pairs_are_per_agent_and_keep_values_off_the_other_agent() {
        let a = helm_secret_pairs("acme-a", &secrets()).unwrap();
        let b = helm_secret_pairs(
            "acme-b",
            &BTreeMap::from([("GITHUB_PERSONAL_ACCESS_TOKEN".into(), "ghp_agent_b".into())]),
        )
        .unwrap();
        assert!(a.iter().any(|(k, v)| {
            k == "agentSandbox.connectorSecrets.acme-a.GITHUB_PERSONAL_ACCESS_TOKEN"
                && v == "ghp_agent_a"
        }));
        assert!(b.iter().any(|(k, v)| {
            k == "agentSandbox.connectorSecrets.acme-b.GITHUB_PERSONAL_ACCESS_TOKEN"
                && v == "ghp_agent_b"
        }));
        assert!(!a.iter().any(|(k, _)| k.contains("acme-b")));
        assert!(!b.iter().any(|(_, v)| v == "ghp_agent_a"));
    }

    #[test]
    fn bind_commands_use_a_values_file_and_never_argv_set() {
        let cmds = bind_commands(&BindOpts {
            common: CommonOpts {
                namespace: "curie".into(),
                release: "curie".into(),
                dry_run: false,
            },
            chart: "charts/curie".into(),
            agent: "acme-a".into(),
            secrets: secrets(),
        })
        .unwrap();
        let helm = cmds[0].display();
        assert!(helm.contains("helm upgrade"), "{helm}");
        assert!(helm.contains("--reuse-values"), "{helm}");
        assert!(helm.contains("-f"), "{helm}");
        assert!(
            !helm.contains("ghp_agent_a"),
            "secret leaked into argv: {helm}"
        );
        assert!(!helm.contains("--set"), "{helm}");
        let delete = cmds[1].display();
        assert!(delete.contains("delete sandboxclaim"), "{delete}");
        assert!(
            delete.contains(&format!("{CONNECTOR_AGENT_LABEL_KEY}=acme-a")),
            "{delete}"
        );
        assert!(delete.contains("--ignore-not-found=true"), "{delete}");
    }

    #[test]
    fn invalid_agent_name_is_rejected() {
        let err = helm_secret_pairs("Not_A_DNS", &secrets())
            .unwrap_err()
            .to_string();
        assert!(err.contains("Not_A_DNS"), "{err}");
    }
}
