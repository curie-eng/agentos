//! Self contained example installation workflows.
//!
//! The SRE bot installer embeds both its observability values and its runtime
//! bundle so a released CLI drives the same one command path as a source
//! checkout. Kubernetes remains the source of capacity truth and every
//! cluster mutation happens only after that read succeeds.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use base64::Engine as _;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use tokio::io::AsyncWriteExt;

use crate::commands::{self, DeployOpts, DeployTier};
use crate::ui::DryRunPlan;

const OBSERVABILITY_NAMESPACE: &str = "observability";
const CURIE_NAMESPACE: &str = "curie";
#[allow(dead_code)] // clap --release default; kept beside the other identity names
const CURIE_RELEASE: &str = "curie";
// #1765's issue text says 1248Mi, but its seven exact appendix requests totalled
// 1312Mi on one node once the enabled 64Mi kube-state-metrics request was
// included. #2059 then raised Tempo's own request from 192Mi to 256Mi to fit the
// measured single-pod envelope in examples/sre-bot/observability/tempo.yaml, so
// the one-node total is now 1376Mi. FIXED_MEMORY_MIB is the part that lands once
// per install regardless of node count: Grafana 128 + Loki 256 + Prometheus 512
// + kube-state-metrics 64 + Tempo 256 = 1216Mi. Alloy 128 and node exporter 32
// are DaemonSets, so PER_READY_NODE_MEMORY_MIB adds their 160Mi on every Ready
// schedulable node -- 1216 + 160 = 1376Mi on a single-node cluster. Move the
// Tempo term here whenever tempo.yaml's resources.requests.memory moves.
const FIXED_MEMORY_MIB: u128 = 1216;
const PER_READY_NODE_MEMORY_MIB: u128 = 160;
const MIB: u128 = 1024 * 1024;
const HELM_TIMEOUT: &str = "10m";
const MANAGED_HELM_RELEASES: [&str; 4] = ["grafana", "loki", "alloy", "prometheus"];
const GRAFANA_ADMIN_SECRET: &str = "grafana-admin";
const GRAFANA_RELEASE: &str = "grafana";
const READER_IDENTITY: &str = "sre-bot-reader";
const READER_TOKEN_SECRET: &str = "sre-bot-reader-token";
const WRITER_IDENTITY: &str = "sre-bot-writer";
const WRITER_TOKEN_SECRET: &str = "sre-bot-writer-token";
const WRITE_KUBECONFIG_SECRET_KEY: &str = "K8S_WRITE_KUBECONFIG";
const WRITE_ALLOWLIST_ENV: &str = "K8S_WRITE_ALLOWLIST";
const WRITE_GATE: &str = "mcp__k8s-write__restart_deployment";
const SCALE_GATE: &str = "mcp__k8s-scale__scale_deployment";
const UPGRADE_GATE: &str = "mcp__self-upgrade__upgrade_self";
// The platform-upgrade verb. Stripped like the others on a read-only install:
// its Job, its identity and its CronJob are all absent by default, so shipping
// the gate without them would validate and never fire.
const PLATFORM_UPGRADE_GATE: &str = "mcp__self-upgrade__upgrade_platform";
// The platform-upgrade objects this installer renders. Names are fixed rather
// than configurable: the connector is told the CronJob's name through its own
// env, and two places free to disagree is how a tool ends up refusing every call
// with nothing wrong in either file.
// The CONNECTOR's identity -- `create jobs`, so it can press the button. Distinct
// from PLATFORM_UPGRADER_IDENTITY below, which is what the Job itself runs as and
// is the one that can rewrite the release. Two identities on purpose: the thing
// that starts an upgrade and the thing that performs one should not be the same
// credential, or the connector would hold namespace-admin for the life of its pod.
const UPGRADER_IDENTITY: &str = "sre-bot-upgrader";
const UPGRADER_TOKEN_SECRET: &str = "sre-bot-upgrader-token";
const SELF_UPGRADE_KUBECONFIG_SECRET_KEY: &str = "SELF_UPGRADE_KUBECONFIG";
const PLATFORM_UPGRADER_IDENTITY: &str = "curie-platform-upgrader";
const PLATFORM_UPGRADE_CRONJOB_NAME: &str = "platform-upgrade";
const SELF_UPGRADE_CRONJOB_NAME: &str = "sre-bot-self-upgrade";
const PLATFORM_UPGRADE_CONFIGMAP: &str = "platform-upgrade";
// The project whose releases define "newest" for the platform upgrade. Fixed
// rather than a flag: this installer installs THIS project's example, and an
// upgrade pointed at a different repository is a different thing entirely.
const PLATFORM_UPGRADE_SOURCE_REPO: &str = "curie-eng/curie";
const PLATFORM_UPGRADE_CRONJOB_ENV: &str = "PLATFORM_UPGRADE_CRONJOB";
const SELF_UPGRADE_CRONJOB_ENV: &str = "SELF_UPGRADE_CRONJOB";
// The rule shape this build knows how to render, asserted against the shipped
// manifest exactly as the write Role's is. The manifest is edited far more often
// than this file, so a widened grant must stop the install rather than ship in
// it -- and this is the widest grant the bundle has.
const PLATFORM_RULE_SHAPE: [(&str, &[&str]); 6] = [
    (
        "",
        &[
            "secrets",
            "configmaps",
            "services",
            "serviceaccounts",
            "persistentvolumeclaims",
        ],
    ),
    (
        "apps",
        &["deployments", "statefulsets", "daemonsets", "replicasets"],
    ),
    ("batch", &["jobs", "cronjobs"]),
    ("networking.k8s.io", &["networkpolicies", "ingresses"]),
    ("rbac.authorization.k8s.io", &["roles", "rolebindings"]),
    ("", &["pods", "events"]),
];
// The one grant the write path may carry. Read from the shipped manifest and
// asserted rather than assumed, so editing that file to widen the verb set stops
// the install instead of shipping in it -- the same posture the connector and
// gate removals below already take.
const WRITE_RULE_API_GROUPS: [&str; 1] = ["apps"];
const WRITE_RULE_RESOURCES: [&str; 1] = ["deployments"];
const WRITE_RULE_VERBS: [&str; 2] = ["get", "patch"];
const READER_TOKEN_TIMEOUT: &str = "2m";
const KUBECONFIG_SECRET_KEY: &str = "K8S_READONLY_KUBECONFIG";
// The gated write connector's published image. A `sha-<commit>` tag rather than a
// semver one because no tagged release has carried this connector yet: it is
// published by `release.yaml` on every push to a release branch, and a commit tag
// is immutable in the way `latest` is not. Move this to a semver tag once a
// release publishes one.
const K8S_WRITE_IMAGE_REPOSITORY: &str = "ghcr.io/curie-eng/curie-sre-bot-k8s-write";
const K8S_WRITE_IMAGE_TAG: &str = "sha-0fb841432db69f67591648eedf96f278aa87bd2c";
// The self-upgrade connector's published image. Same reasoning as the write
// connector's above: a `sha-<commit>` tag because no tagged release carries this
// connector yet, and an immutable one because `latest` is not.
const SELF_UPGRADE_IMAGE_REPOSITORY: &str = "ghcr.io/curie-eng/curie-sre-bot-self-upgrade";
const SELF_UPGRADE_IMAGE_TAG: &str = "sha-a391c48b591cf4bf9637ce816964031288151f8e";
const TEMPO_IMAGE_REPOSITORY: &str = "ghcr.io/curie-eng/curie-sre-bot-tempo";
const TEMPO_IMAGE_TAG: &str = "0.8.0";
const TEMPO_TAGGED_IMAGE: &str = "ghcr.io/curie-eng/curie-sre-bot-tempo:0.8.0";
const RUNTIME_PLUGIN_DESCRIPTION: &str = "SRE triage assistant for plain English production health and Kubernetes questions in Slack. This installer deploys read only Kubernetes, Grafana, and Tempo connectors. It omits the source bundle's gated write connector and approval policy; enable that path only through the documented explicit build and deploy flow.";
const RUNTIME_PLUGIN_WRITE_DESCRIPTION: &str = "SRE triage assistant for plain English production health and Kubernetes questions in Slack. This installer deploys read only Kubernetes, Grafana, and Tempo connectors, plus one approval-gated Kubernetes restart tool scoped to the Deployments named at install time. Every restart requires a human approval; no other write verb is available.";
const OCI_INDEX_MEDIA_TYPE: &str = "application/vnd.oci.image.index.v1+json";
const DOCKER_INDEX_MEDIA_TYPE: &str = "application/vnd.docker.distribution.manifest.list.v2+json";

const OBSERVABILITY_FILES: &[(&str, &[u8])] = &[
    (
        "grafana-values.yaml",
        include_bytes!("../../examples/sre-bot/observability/grafana-values.yaml"),
    ),
    (
        "loki-values.yaml",
        include_bytes!("../../examples/sre-bot/observability/loki-values.yaml"),
    ),
    (
        "alloy-values.yaml",
        include_bytes!("../../examples/sre-bot/observability/alloy-values.yaml"),
    ),
    (
        "prometheus-values.yaml",
        include_bytes!("../../examples/sre-bot/observability/prometheus-values.yaml"),
    ),
    (
        "tempo.yaml",
        include_bytes!("../../examples/sre-bot/observability/tempo.yaml"),
    ),
    (
        "curie-values.yaml",
        include_bytes!("../../examples/sre-bot/observability/curie-values.yaml"),
    ),
];

const BUNDLE_FILES: &[(&str, &[u8])] = &[
    (
        ".claude-plugin/plugin.json",
        include_bytes!("../../examples/sre-bot/.claude-plugin/plugin.json"),
    ),
    (
        "connectors.yaml",
        include_bytes!("../../examples/sre-bot/connectors.yaml"),
    ),
    (
        "deploy.yaml",
        include_bytes!("../../examples/sre-bot/deploy.yaml"),
    ),
    (
        "evals/cases.json",
        include_bytes!("../../examples/sre-bot/evals/cases.json"),
    ),
    (
        "manifests/read-access.yaml",
        include_bytes!("../../examples/sre-bot/manifests/read-access.yaml"),
    ),
    (
        "manifests/write-role.yaml",
        include_bytes!("../../examples/sre-bot/manifests/write-role.yaml"),
    ),
    (
        "manifests/upgrade-role.yaml",
        include_bytes!("../../examples/sre-bot/manifests/upgrade-role.yaml"),
    ),
    (
        "manifests/platform-upgrade-role.yaml",
        include_bytes!("../../examples/sre-bot/manifests/platform-upgrade-role.yaml"),
    ),
    (
        "skills/sre-bot/SKILL.md",
        include_bytes!("../../examples/sre-bot/skills/sre-bot/SKILL.md"),
    ),
];

/// The platform-upgrade Job's template and the script it runs. Not in
/// `BUNDLE_FILES`: they are cluster objects this installer renders and applies,
/// not files the agent bundle carries.
const PLATFORM_UPGRADE_CRONJOB_YAML: &[u8] =
    include_bytes!("../../examples/sre-bot/platform-upgrade/cronjob.yaml");
const PLATFORM_UPGRADE_SCRIPT: &[u8] =
    include_bytes!("../../examples/sre-bot/platform-upgrade/upgrade.sh");

pub struct SreBotInstallOpts {
    pub observability: bool,
    pub dry_run: bool,
    pub slack_channel: Option<String>,
    /// `ns/name[,ns/name]`. Absent still INSTALLS the gated write connector,
    /// with an empty ceiling: the tool is published and gated, and every call is
    /// refused until an operator names targets. See `write_targets` below.
    pub write_allowlist: Option<String>,
    /// Leave the write connector out entirely, as absence used to.
    pub no_write: bool,
    /// Install the upgrade path: the self-upgrade connector, the platform
    /// upgrade Job and the two identities behind them. Absent, the connector is
    /// stripped exactly as it was before this flag existed, so an install that
    /// does not ask for it is unchanged.
    pub platform_upgrade: bool,
    pub namespace: String,
    pub release: String,
    pub observability_namespace: String,
}

struct InstallIdentity {
    namespace: String,
    release: String,
    observability_namespace: String,
}

impl InstallIdentity {
    fn from_opts(opts: &SreBotInstallOpts) -> Self {
        Self {
            namespace: opts.namespace.clone(),
            release: opts.release.clone(),
            observability_namespace: opts.observability_namespace.clone(),
        }
    }
}

pub enum SreBotInstallResult {
    DryRun(DryRunPlan),
    Installed(Box<commands::DeployOutput>),
}

#[derive(Clone)]
enum CommandArg {
    Plain(String),
    ObservabilityFile(&'static str),
    BundleFile(&'static str),
    CurieChart,
}

impl CommandArg {
    fn display(&self, chart: &Path) -> String {
        match self {
            Self::Plain(value) => value.clone(),
            Self::ObservabilityFile(name) => {
                format!("examples/sre-bot/observability/{name}")
            }
            Self::BundleFile(name) => format!("examples/sre-bot/{name}"),
            Self::CurieChart => chart.display().to_string(),
        }
    }

    fn live(&self, workspace: &EmbeddedWorkspace, chart: &Path) -> String {
        match self {
            Self::Plain(value) => value.clone(),
            Self::ObservabilityFile(name) => workspace
                .observability_dir()
                .join(name)
                .display()
                .to_string(),
            Self::BundleFile(name) => workspace.bundle_dir().join(name).display().to_string(),
            Self::CurieChart => chart.display().to_string(),
        }
    }
}

#[derive(Clone)]
struct InstallCommand {
    program: &'static str,
    args: Vec<CommandArg>,
    helm_target: Option<HelmTarget>,
}

impl InstallCommand {
    fn display(&self, chart: &Path) -> String {
        std::iter::once(self.program.to_string())
            .chain(self.args.iter().map(|arg| arg.display(chart)))
            .collect::<Vec<_>>()
            .join(" ")
    }
}

#[derive(Clone)]
struct HelmTarget {
    release: String,
    namespace: String,
}

fn plain(value: impl Into<String>) -> CommandArg {
    CommandArg::Plain(value.into())
}

fn helm_repo_commands() -> Vec<InstallCommand> {
    vec![
        InstallCommand {
            program: "helm",
            args: vec![
                plain("repo"),
                plain("add"),
                plain("grafana-community"),
                plain("https://grafana-community.github.io/helm-charts"),
                plain("--force-update"),
            ],
            helm_target: None,
        },
        InstallCommand {
            program: "helm",
            args: vec![
                plain("repo"),
                plain("add"),
                plain("grafana"),
                plain("https://grafana.github.io/helm-charts"),
                plain("--force-update"),
            ],
            helm_target: None,
        },
        InstallCommand {
            program: "helm",
            args: vec![
                plain("repo"),
                plain("add"),
                plain("prometheus-community"),
                plain("https://prometheus-community.github.io/helm-charts"),
                plain("--force-update"),
            ],
            helm_target: None,
        },
        InstallCommand {
            program: "helm",
            args: vec![
                plain("repo"),
                plain("update"),
                plain("grafana-community"),
                plain("grafana"),
                plain("prometheus-community"),
            ],
            helm_target: None,
        },
    ]
}

fn upstream_upgrade(
    release: &'static str,
    chart: &'static str,
    version: &'static str,
    values: &'static str,
    observability_namespace: &str,
) -> InstallCommand {
    InstallCommand {
        program: "helm",
        args: vec![
            plain("upgrade"),
            plain("--install"),
            plain(release),
            plain(chart),
            plain("--version"),
            plain(version),
            plain("--namespace"),
            plain(observability_namespace),
            plain("--create-namespace"),
            plain("-f"),
            CommandArg::ObservabilityFile(values),
            plain("--wait"),
            plain("--timeout"),
            plain(HELM_TIMEOUT),
        ],
        helm_target: Some(HelmTarget {
            release: release.to_string(),
            namespace: observability_namespace.to_string(),
        }),
    }
}

fn stack_install_commands(observability_namespace: &str) -> Vec<InstallCommand> {
    let mut commands = helm_repo_commands();
    commands.extend([
        upstream_upgrade(
            "grafana",
            "grafana-community/grafana",
            "12.11.1",
            "grafana-values.yaml",
            observability_namespace,
        ),
        upstream_upgrade(
            "loki",
            "grafana-community/loki",
            "18.10.1",
            "loki-values.yaml",
            observability_namespace,
        ),
        upstream_upgrade(
            "alloy",
            "grafana/alloy",
            "1.11.1",
            "alloy-values.yaml",
            observability_namespace,
        ),
        upstream_upgrade(
            "prometheus",
            "prometheus-community/prometheus",
            "29.27.0",
            "prometheus-values.yaml",
            observability_namespace,
        ),
        InstallCommand {
            program: "kubectl",
            args: vec![
                plain("apply"),
                plain("--namespace"),
                plain(observability_namespace),
                plain("-f"),
                CommandArg::ObservabilityFile("tempo.yaml"),
            ],
            helm_target: None,
        },
        InstallCommand {
            program: "kubectl",
            args: vec![
                plain("rollout"),
                plain("status"),
                plain("statefulset/tempo"),
                plain("--namespace"),
                plain(observability_namespace),
                plain(format!("--timeout={HELM_TIMEOUT}")),
            ],
            helm_target: None,
        },
    ]);
    commands
}

fn curie_integration_command(identity: &InstallIdentity) -> InstallCommand {
    InstallCommand {
        program: "helm",
        args: vec![
            plain("upgrade"),
            plain(&identity.release),
            CommandArg::CurieChart,
            plain("--namespace"),
            plain(&identity.namespace),
            plain("--reuse-values"),
            plain("-f"),
            CommandArg::ObservabilityFile("curie-values.yaml"),
            plain("--wait"),
            plain("--timeout"),
            plain(HELM_TIMEOUT),
        ],
        helm_target: Some(HelmTarget {
            release: identity.release.clone(),
            namespace: identity.namespace.clone(),
        }),
    }
}

fn write_role_command() -> InstallCommand {
    InstallCommand {
        program: "kubectl",
        args: vec![
            plain("apply"),
            plain("-f"),
            CommandArg::BundleFile("manifests/write-role.yaml"),
        ],
        helm_target: None,
    }
}

fn read_access_command() -> InstallCommand {
    InstallCommand {
        program: "kubectl",
        args: vec![
            plain("apply"),
            plain("-f"),
            CommandArg::BundleFile("manifests/read-access.yaml"),
        ],
        helm_target: None,
    }
}

pub async fn install_sre_bot(opts: SreBotInstallOpts) -> Result<SreBotInstallResult> {
    if !opts.observability {
        return Err(crate::exit::usage(
            "the SRE bot example installer currently requires --observability",
        ));
    }

    // Three states, not two. The write connector is now installed by default,
    // because a capability nobody can see is indistinguishable from one that was
    // never built -- which is exactly how this bundle's second write verb came to
    // read as a design choice.
    //
    // `None` here means the connector is absent. `Some(&[])` means it is present
    // with an EMPTY ceiling, and both halves of that ceiling are closed:
    //
    //   * the connector starts, publishes its tool, and refuses every call. Its
    //     own source says so: "a missing config fails closed rather than granting
    //     everything."
    //   * no Role is rendered at all. This is the load-bearing half. An RBAC rule
    //     whose `resourceNames` is empty or absent is NOT a rule that grants
    //     nothing -- it grants the verb on EVERY resource of that type. So the
    //     empty case must omit the Role rather than render one with no names, and
    //     `render_write_role` does: with no targets it emits the ServiceAccount
    //     and its token Secret and stops.
    //
    // The identity is still minted either way, because bring-up refuses without
    // `K8S_WRITE_KUBECONFIG` -- an identity that can do nothing is what makes
    // "enabled, ceiling empty" a state the platform can actually boot.
    let write_targets = match (opts.no_write, opts.write_allowlist.as_deref()) {
        (true, Some(_)) => {
            return Err(crate::exit::usage(
                "--no-write and --write-allowlist contradict each other; pass one",
            ));
        }
        (true, None) => None,
        (false, Some(raw)) => Some(parse_write_allowlist(raw)?),
        (false, None) => Some(Vec::new()),
    };
    let write_targets = write_targets.as_deref();
    let identity = InstallIdentity::from_opts(&opts);

    preflight_capacity(&identity.observability_namespace).await?;

    let resolved_chart = crate::artifacts::resolve_chart(
        None,
        crate::artifacts::Channel::current(),
        crate::artifacts::version(),
        crate::artifacts::cache_root,
        Path::new("charts/curie").is_dir(),
    )?;
    let stack_commands = stack_install_commands(&identity.observability_namespace);
    let integration_command = curie_integration_command(&identity);
    let read_access_command = read_access_command();
    let write_role_command = write_role_command();

    if opts.dry_run {
        let chart = resolved_chart.planned_target();
        let mut lines = vec![format!(
            "resolve {TEMPO_TAGGED_IMAGE} to its immutable OCI image index digest before cluster mutation"
        )];
        lines.push(format!(
            "preserve or create Secret {GRAFANA_ADMIN_SECRET} in namespace {} without exposing its generated password",
            identity.observability_namespace
        ));
        lines.extend(stack_commands.iter().map(|command| command.display(&chart)));
        lines.extend(apply_curie_platform(&chart, true, &identity).await?);
        lines.push(integration_command.display(&chart));
        lines.push(read_access_command.display(&chart));
        lines.push(format!(
            "kubectl wait --namespace {} --for=jsonpath={{.data.token}} secret/{READER_TOKEN_SECRET} --timeout={READER_TOKEN_TIMEOUT}",
            identity.namespace
        ));
        lines.push(
            "build the read only Kubernetes connector kubeconfig in memory from the ServiceAccount token"
                .to_string(),
        );
        if let Some(targets) = write_targets {
            if targets.is_empty() {
                lines.push(format!(
                    "install connectors.k8s-write with an EMPTY {WRITE_ALLOWLIST_ENV}: the tool is \
                     published and gated, every call is refused, and NO Role is rendered (an empty \
                     resourceNames would grant every Deployment). Name targets with --write-allowlist"
                ));
            } else {
                lines.push(format!(
                    "render manifests/write-role.yaml and connectors.k8s-write env {} from one allowlist: {}",
                    WRITE_ALLOWLIST_ENV,
                    write_allowlist_value(targets)
                ));
            }
        }
        let mut deploy = format!(
            "curie cluster deploy --plugin-dir embedded:examples/sre-bot --namespace {} --release {}",
            identity.namespace, identity.release
        );
        if let Some(channel) = &opts.slack_channel {
            deploy.push_str(&format!(" --slack-channel {channel}"));
        }
        lines.push(deploy);
        if write_targets.is_some() {
            lines.push(format!(
                "{} -- applied HERE only on a fresh install; when ServiceAccount \
                 {WRITER_IDENTITY} already exists in namespace {} the same apply runs BEFORE the \
                 deploy above, so a narrowed allowlist is in force before the new version activates",
                write_role_command.display(&chart),
                identity.namespace
            ));
            lines.push(format!(
                "kubectl wait --namespace {} --for=jsonpath={{.data.token}} secret/{WRITER_TOKEN_SECRET} --timeout={READER_TOKEN_TIMEOUT}",
                identity.namespace
            ));
            lines.push(
                "build the gated write Kubernetes connector kubeconfig in memory from the ServiceAccount token"
                    .to_string(),
            );
        }
        if opts.platform_upgrade {
            // The widest grant this installer can create, and the reason someone
            // runs --dry-run at all. Omitting it was the first version of this
            // flag: the live path applied these four objects and the plan said
            // nothing, so an operator deciding whether to accept a
            // namespace-admin-equivalent identity could not see it in the one
            // output built for that decision.
            lines.push(format!(
                "kubectl apply -f examples/sre-bot/manifests/upgrade-role.yaml -- the CONNECTOR's \
                 identity ({UPGRADER_IDENTITY}): create on jobs, so it can start an upgrade"
            ));
            lines.push(format!(
                "kubectl apply -f examples/sre-bot/manifests/platform-upgrade-role.yaml -- the \
                 JOB's identity ({PLATFORM_UPGRADER_IDENTITY}) in namespace {}: namespace-admin \
                 in all but name, because `helm upgrade` rewrites nearly every object the release \
                 owns. READ THAT FILE. It exists for the ~90s an upgrade runs and the sandbox \
                 never sees it",
                identity.namespace
            ));
            lines.push(format!(
                "kubectl apply -f <rendered ConfigMap {PLATFORM_UPGRADE_CONFIGMAP}> -- the upgrade \
                 script the Job runs, from examples/sre-bot/platform-upgrade/upgrade.sh"
            ));
            lines.push(format!(
                "kubectl apply -f <rendered CronJob {PLATFORM_UPGRADE_CRONJOB_NAME}, suspend: \
                 true> -- never fires on its own; the gated {PLATFORM_UPGRADE_GATE} creates a Job \
                 from it when a human approves one"
            ));
            lines.push(format!(
                "kubectl wait --namespace {} --for=jsonpath={{.data.token}} \
                 secret/{UPGRADER_TOKEN_SECRET} --timeout={READER_TOKEN_TIMEOUT}",
                identity.namespace
            ));
            lines.push(
                "build the self-upgrade connector kubeconfig in memory from the ServiceAccount token"
                    .to_string(),
            );
        }
        lines.push(
            "render and reconcile the deployed version connectors with the owned Kubernetes kubeconfig Secret override"
                .to_string(),
        );
        return Ok(SreBotInstallResult::DryRun(DryRunPlan { lines }));
    }

    let tempo_digest = resolve_tempo_index_digest().await?;
    let chart = crate::artifacts::ensure_cached(&resolved_chart).await?;
    crate::ops::require_on_path("helm")?;
    // Resolved only when the connector is kept, so a --no-write install makes no
    // registry call for an image it will never run.
    let write_digest = match write_targets {
        Some(_) => {
            Some(resolve_index_digest(K8S_WRITE_IMAGE_REPOSITORY, K8S_WRITE_IMAGE_TAG).await?)
        }
        None => None,
    };
    // Resolved before the workspace renders, like the other two: `build:` records
    // a LOCAL image id the cluster tier refuses, so a kept connector with no
    // resolved digest is one that can never start.
    let upgrade_digest = match opts.platform_upgrade {
        true => {
            Some(resolve_index_digest(SELF_UPGRADE_IMAGE_REPOSITORY, SELF_UPGRADE_IMAGE_TAG).await?)
        }
        false => None,
    };
    let workspace = EmbeddedWorkspace::create(
        &tempo_digest,
        write_digest.as_deref(),
        write_targets,
        &identity,
        upgrade_digest.as_deref(),
    )?;
    ensure_grafana_admin_secret(&identity.observability_namespace).await?;
    for command in &stack_commands {
        run_install_command(command, &workspace, &chart).await?;
    }
    // Before the apply, not after: the point is to refuse while the credential
    // still exists.
    refuse_to_drop_a_recorded_model_credential(&identity).await?;
    apply_curie_platform(&chart, false, &identity).await?;
    run_install_command(&integration_command, &workspace, &chart).await?;
    run_install_command(&read_access_command, &workspace, &chart).await?;
    let kubeconfig = read_only_connector_kubeconfig(&identity.namespace).await?;

    let bundle_dir = workspace.bundle_dir();
    let connection = resolve_embedded_cluster_connection(&identity).await?;
    // The write Role and the writer kubeconfig are ordered around the deploy by
    // PRIVILEGE DIRECTION, keyed on whether the writer identity already exists:
    //
    //     never create a NEW privileged identity before the deploy;
    //     always tighten an EXISTING one before the deploy.
    //
    // FRESH INSTALL -- no `sre-bot-writer` ServiceAccount in the namespace, so
    // the apply is DEFERRED to after the deploy succeeds. The identity it mints
    // is a NON-EXPIRING ServiceAccount token, and with named targets it carries
    // get,patch on apps/deployments. Nothing between the workspace render and
    // the deploy needs it -- the only consumer is sync_deployed_version below --
    // so creating it any sooner buys nothing and leaves a live write credential
    // in the cluster for the whole length of the deploy.
    //
    // Deferred rather than rolled back. A rollback would need the same cluster
    // API call that just failed: if the deploy died because the API server is
    // unreachable, the RBAC write is being throttled, or the port-forward
    // dropped, the compensating delete fails too and the credential is still
    // standing -- now behind a log line claiming it was cleaned up. Deferring
    // means the exposure window does not exist, rather than merely being
    // shorter, and it adds no new failure path of its own.
    //
    // What this does NOT cover: a failure between that apply and
    // sync_deployed_version still strands the identity, now with a deployed bot
    // in front of it. That residual window is one API call wide, and it is the
    // price of declining a rollback path.
    //
    // RE-INSTALL / UPGRADE -- the ServiceAccount is already there, so the same
    // apply runs BEFORE the deploy. Applying early strands nothing, because
    // every object it names already exists; and it is the only ordering that
    // fails CLOSED when an operator NARROWS the allowlist. sync_deployed_version
    // reconciles the connector Deployment's K8S_WRITE_ALLOWLIST only AFTER the
    // new version is activated, so deferring the Role on an upgrade leaves the
    // activation-to-sync window with the OLD wide Role still on the API server
    // AND the OLD wide env still on the running connector pod: both halves
    // permit, and an approved call can patch a target the operator just removed.
    // That is the #1886 class -- the two allowlists disagreeing in the dangerous
    // direction. Tightening the Role first makes that same window 403 instead.
    let writer_identity_present = match write_targets {
        Some(_) => writer_identity_exists(&identity.namespace).await?,
        None => false,
    };
    let mut write_kubeconfig = None;
    if writer_identity_present {
        write_kubeconfig = Some(
            apply_write_access(&write_role_command, &workspace, &chart, &identity.namespace)
                .await?,
        );
    }
    let deployed =
        deploy_embedded_sre_bot(&bundle_dir, &connection, opts.slack_channel.as_deref()).await?;
    if write_targets.is_some() && !writer_identity_present {
        write_kubeconfig = Some(
            apply_write_access(&write_role_command, &workspace, &chart, &identity.namespace)
                .await?,
        );
    }
    // ALWAYS after the deploy, never before. `install_sre_bot` orders privileged
    // identities by direction -- never create a NEW one before the deploy -- and
    // both of these are new every time: unlike the write Role there is no
    // allowlist here to NARROW, so the "tighten an existing one early" case that
    // justifies the pre-deploy apply simply does not arise. Creating them any
    // sooner would leave the widest credential this installer can create standing
    // for the whole length of a deploy that may still fail.
    let mut upgrade_kubeconfig = None;
    if opts.platform_upgrade {
        upgrade_kubeconfig =
            Some(apply_upgrade_path(&workspace, &chart, &identity.namespace).await?);
    }
    let mut secret_overrides = BTreeMap::from([(KUBECONFIG_SECRET_KEY.to_string(), kubeconfig)]);
    if let Some(write_kubeconfig) = write_kubeconfig {
        secret_overrides.insert(WRITE_KUBECONFIG_SECRET_KEY.to_string(), write_kubeconfig);
    }
    if let Some(upgrade_kubeconfig) = upgrade_kubeconfig {
        secret_overrides.insert(
            SELF_UPGRADE_KUBECONFIG_SECRET_KEY.to_string(),
            upgrade_kubeconfig,
        );
    }
    crate::connectors::sync_deployed_version(
        &connection.api_url,
        &connection.api_key,
        &identity.namespace,
        &identity.release,
        &deployed,
        &secret_overrides,
    )
    .await?;
    Ok(SreBotInstallResult::Installed(Box::new(deployed)))
}

/// Apply the upgrade path's objects and mint the connector's kubeconfig.
///
/// Order within this function matters: the identities come first, then the
/// script, then the CronJob that references both. Applying the CronJob first
/// would leave a template naming a ServiceAccount that does not exist, which
/// Kubernetes accepts and which fails only when a Job is finally created from it
/// -- after a human has approved an upgrade.
async fn apply_upgrade_path(
    workspace: &EmbeddedWorkspace,
    chart: &Path,
    namespace: &str,
) -> Result<String> {
    for file in [
        "manifests/upgrade-role.yaml",
        "manifests/platform-upgrade-role.yaml",
        "manifests/platform-upgrade-configmap.yaml",
        "manifests/platform-upgrade-cronjob.yaml",
    ] {
        let command = InstallCommand {
            program: "kubectl",
            args: vec![plain("apply"), plain("-f"), CommandArg::BundleFile(file)],
            helm_target: None,
        };
        run_install_command(&command, workspace, chart).await?;
    }
    connector_kubeconfig(UPGRADER_IDENTITY, UPGRADER_TOKEN_SECRET, namespace).await
}

/// Stop rather than silently reset a model credential this installer will drop.
///
/// `apply_curie_platform` goes through the declarative path with
/// `Credentials::default()`, and that path's contract is deliberate: a
/// configuration naming no model credential really does clear one, and `curie
/// diff` reports it as a reset because that is what happens
/// (`without_a_declared_model_credential_those_keys_are_resets`). The contract is
/// right; using it from an installer that declares nothing is not.
///
/// Run against a live install, that combination removed
/// `agentSandbox.runner.credentials` and left the release on the chart's
/// `fakeModel` default. Nothing looked wrong: helm reported success, every pod
/// stayed healthy, and the bot kept answering -- in three milliseconds, from the
/// fake model, "all done" (#2129).
///
/// So the installer asks first. Refusing costs a re-run with the credential
/// named; not refusing costs the credential, and there is no signal on the way
/// out that it went.
/// Does this release's recorded values carry a model credential?
///
/// Split out so the decision is testable without a cluster: the read is the part
/// that needs one, and the read is not what was wrong.
fn records_a_model_credential(existing: &serde_json::Value) -> bool {
    existing
        .pointer("/agentSandbox/runner/credentials")
        .and_then(serde_json::Value::as_str)
        .is_some_and(|recorded| !recorded.trim().is_empty())
}

async fn refuse_to_drop_a_recorded_model_credential(identity: &InstallIdentity) -> Result<()> {
    let opts = crate::ops::CommonOpts {
        namespace: identity.namespace.clone(),
        release: identity.release.clone(),
        dry_run: false,
    };
    let Some(existing) = crate::ops::fetch_release_values(&opts).await? else {
        // No release yet: a fresh install has nothing to drop.
        return Ok(());
    };
    if !records_a_model_credential(&existing) {
        return Ok(());
    }
    Err(crate::exit::usage(format!(
        "release {} in namespace {} records a model credential, and this installer \
         would clear it.\n\n\
         It applies the declarative path with no credential declared, and that path \
         removes what it does not name -- so re-running here would leave the install \
         on the chart's fakeModel default, healthy in every way except that the agent \
         is no longer a model. That state is hard to see: pods stay Ready and turns \
         still answer.\n\n\
         Preserve it first, then re-run:\n\n    \
         helm get values {} -n {} -o yaml > /tmp/values.yaml\n    \
         # keep the agentSandbox block, then after this installer finishes:\n    \
         helm upgrade {} <chart> -n {} --reuse-values -f /tmp/values.yaml",
        identity.release,
        identity.namespace,
        identity.release,
        identity.namespace,
        identity.release,
        identity.namespace,
    )))
}

async fn apply_curie_platform(
    chart: &Path,
    dry_run: bool,
    identity: &InstallIdentity,
) -> Result<Vec<String>> {
    let installation = crate::installation::Installation {
        version: crate::installation::SUPPORTED_VERSION,
        install: crate::installation::Install {
            namespace: identity.namespace.clone(),
            release: identity.release.clone(),
        },
        platform: crate::installation::Platform::default(),
        credentials: crate::installation::Credentials::default(),
        comms: crate::installation::Comms::default(),
        set: BTreeMap::new(),
    };
    let local = crate::installation::plan_installation(installation, dry_run)?;
    match crate::installation::apply(crate::installation::ApplyOpts {
        local,
        chart: chart.display().to_string(),
        allow_stateful_removal: false,
        migrate_store: false,
    })
    .await?
    {
        crate::installation::ApplyOutput::DryRun(plan) => Ok(plan.lines),
        crate::installation::ApplyOutput::Applied { .. } => Ok(Vec::new()),
    }
}

#[derive(Deserialize)]
struct RegistryToken {
    #[serde(alias = "access_token")]
    token: String,
}

async fn resolve_tempo_index_digest() -> Result<String> {
    resolve_index_digest(TEMPO_IMAGE_REPOSITORY, TEMPO_IMAGE_TAG).await
}

/// The immutable index digest behind one `ghcr.io/<org>/<name>:<tag>`.
///
/// Generalised from the tempo-only resolver because the gated write connector
/// needs exactly the same treatment. The bundle declares it `build:`, which
/// records a LOCAL image id, and a cluster cannot pull an image that exists only
/// in one machine's Docker daemon -- so keeping the connector without resolving a
/// published digest produces a bundle whose write path can never come up.
async fn resolve_index_digest(repository: &str, tag: &str) -> Result<String> {
    let path = repository
        .strip_prefix("ghcr.io/")
        .with_context(|| format!("{repository} is not a ghcr.io repository"))?
        .to_string();
    let tagged = format!("{repository}:{tag}");
    let registry = std::env::var("CURIE_TEST_SRE_BOT_REGISTRY_ENDPOINT")
        .unwrap_or_else(|_| "https://ghcr.io".to_string());
    let registry = registry.trim_end_matches('/');
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .context("building the anonymous GHCR client")?;
    let token_response = client
        .get(format!("{registry}/token"))
        .query(&[
            ("service", "ghcr.io"),
            ("scope", format!("repository:{path}:pull").as_str()),
        ])
        .send()
        .await
        .with_context(|| format!("resolving {tagged} before cluster mutation"))?;
    if !token_response.status().is_success() {
        bail!(
            "could not resolve {tagged} before cluster mutation: anonymous GHCR token request returned HTTP {}",
            token_response.status()
        );
    }
    let token: RegistryToken = token_response
        .json()
        .await
        .with_context(|| format!("reading the anonymous token for {tagged}"))?;
    if token.token.is_empty() {
        bail!("could not resolve {tagged}: GHCR returned an empty token");
    }

    let manifest_response = client
        .get(format!("{registry}/v2/{path}/manifests/{tag}"))
        .bearer_auth(&token.token)
        .header(
            reqwest::header::ACCEPT,
            format!("{OCI_INDEX_MEDIA_TYPE}, {DOCKER_INDEX_MEDIA_TYPE}"),
        )
        .send()
        .await
        .with_context(|| format!("fetching the OCI image index for {tagged}"))?;
    if !manifest_response.status().is_success() {
        bail!(
            "could not resolve {tagged}: OCI index request returned HTTP {}",
            manifest_response.status()
        );
    }
    let body = manifest_response
        .bytes()
        .await
        .with_context(|| format!("reading the OCI image index for {tagged}"))?;
    let manifest: serde_json::Value = serde_json::from_slice(&body)
        .with_context(|| format!("{tagged} returned a malformed OCI index"))?;
    let media_type = manifest
        .get("mediaType")
        .and_then(serde_json::Value::as_str);
    let is_index_media_type = matches!(
        media_type,
        Some(OCI_INDEX_MEDIA_TYPE) | Some(DOCKER_INDEX_MEDIA_TYPE)
    );
    let is_index = is_index_media_type
        && manifest
            .get("schemaVersion")
            .and_then(serde_json::Value::as_u64)
            == Some(2)
        && manifest
            .get("manifests")
            .is_some_and(serde_json::Value::is_array);
    if !is_index {
        bail!(
            "could not resolve {tagged}: expected an OCI image index, got {}",
            media_type.unwrap_or("no mediaType")
        );
    }
    let digest_hex = Sha256::digest(&body)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    let digest = format!("sha256:{digest_hex}");
    validate_sha256_digest(&digest)?;
    Ok(digest)
}

fn validate_sha256_digest(digest: &str) -> Result<()> {
    let Some(hex) = digest.strip_prefix("sha256:") else {
        bail!("resolved Tempo image digest must start with sha256:");
    };
    if hex.len() != 64 || !hex.chars().all(|character| character.is_ascii_hexdigit()) {
        bail!("resolved Tempo image digest must contain 64 lowercase hexadecimal characters");
    }
    if hex != hex.to_ascii_lowercase() {
        bail!("resolved Tempo image digest must contain 64 lowercase hexadecimal characters");
    }
    Ok(())
}

async fn ensure_grafana_admin_secret(observability_namespace: &str) -> Result<()> {
    ensure_observability_namespace(observability_namespace).await?;
    let inspect = tokio::process::Command::new("kubectl")
        .args([
            "get",
            "secret",
            GRAFANA_ADMIN_SECRET,
            "--namespace",
            observability_namespace,
            "-o",
            "json",
        ])
        .output()
        .await
        .context("inspecting the Grafana admin Secret")?;
    if inspect.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&inspect.stderr);
    let lower = stderr.to_ascii_lowercase();
    if !lower.contains("notfound") && !lower.contains("not found") {
        bail!(
            "could not inspect Secret {GRAFANA_ADMIN_SECRET} in namespace {observability_namespace} with `kubectl get secret {GRAFANA_ADMIN_SECRET} -n {observability_namespace}`: {}",
            stderr.trim()
        );
    }

    if grafana_release_exists(observability_namespace).await? {
        return migrate_grafana_admin_secret(observability_namespace).await;
    }

    let password = random_hex(32)?;
    let manifest = serde_json::to_vec(&serde_json::json!({
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": GRAFANA_ADMIN_SECRET,
            "namespace": observability_namespace,
        },
        "type": "Opaque",
        "stringData": {
            "admin-user": "admin",
            "admin-password": password,
        },
    }))?;
    apply_private_manifest(&manifest, "Grafana admin Secret", observability_namespace).await
}

async fn grafana_release_exists(observability_namespace: &str) -> Result<bool> {
    let output = tokio::process::Command::new("helm")
        .args([
            "status",
            GRAFANA_RELEASE,
            "--namespace",
            observability_namespace,
            "-o",
            "json",
        ])
        .output()
        .await
        .context("inspecting the existing Grafana release")?;
    if output.status.success() {
        return Ok(true);
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    if stderr.trim() == "Error: release: not found" {
        return Ok(false);
    }
    bail!(
        "could not determine whether Grafana is already installed; run `helm status {GRAFANA_RELEASE} -n {observability_namespace}` and retry"
    )
}

#[derive(Clone)]
struct SecretKeyReference {
    name: String,
    key: String,
}

async fn migrate_grafana_admin_secret(observability_namespace: &str) -> Result<()> {
    let output = tokio::process::Command::new("kubectl")
        .args([
            "get",
            "deployment,statefulset",
            "--namespace",
            observability_namespace,
            "-l",
            "app.kubernetes.io/instance=grafana",
            "-o",
            "json",
        ])
        .output()
        .await
        .context("discovering the existing Grafana admin credential")?;
    if !output.status.success() {
        bail!("could not read the existing Grafana admin credential");
    }
    let workloads: serde_json::Value = serde_json::from_slice(&output.stdout)
        .context("the existing Grafana workload response was malformed")?;
    let user = find_grafana_secret_reference(&workloads, "GF_SECURITY_ADMIN_USER")?;
    let password = find_grafana_secret_reference(&workloads, "GF_SECURITY_ADMIN_PASSWORD")?;

    let mut source_secrets = BTreeMap::new();
    for source_name in [&user.name, &password.name] {
        if source_secrets.contains_key(source_name) {
            continue;
        }
        let source = tokio::process::Command::new("kubectl")
            .args([
                "get",
                "secret",
                source_name,
                "--namespace",
                observability_namespace,
                "-o",
                "json",
            ])
            .output()
            .await
            .context("reading the existing Grafana admin credential")?;
        if !source.status.success() {
            bail!("could not read the existing Grafana admin credential");
        }
        let secret: serde_json::Value = serde_json::from_slice(&source.stdout)
            .context("the existing Grafana admin Secret response was malformed")?;
        source_secrets.insert(source_name.clone(), secret);
    }

    let user_data = secret_data_value(&source_secrets, &user)?;
    let password_data = secret_data_value(&source_secrets, &password)?;
    let manifest = serde_json::to_vec(&serde_json::json!({
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": GRAFANA_ADMIN_SECRET,
            "namespace": observability_namespace,
        },
        "type": "Opaque",
        "data": {
            "admin-user": user_data,
            "admin-password": password_data,
        },
    }))?;
    apply_private_manifest(&manifest, "Grafana admin Secret", observability_namespace).await
}

fn find_grafana_secret_reference(
    workloads: &serde_json::Value,
    env_name: &str,
) -> Result<SecretKeyReference> {
    let mut references = Vec::new();
    for item in workloads
        .get("items")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
    {
        for container in item
            .pointer("/spec/template/spec/containers")
            .and_then(serde_json::Value::as_array)
            .into_iter()
            .flatten()
        {
            for env in container
                .get("env")
                .and_then(serde_json::Value::as_array)
                .into_iter()
                .flatten()
            {
                if env.get("name").and_then(serde_json::Value::as_str) != Some(env_name) {
                    continue;
                }
                let reference = env.pointer("/valueFrom/secretKeyRef").ok_or_else(|| {
                    anyhow!("could not read the existing Grafana admin credential")
                })?;
                let name = reference
                    .get("name")
                    .and_then(serde_json::Value::as_str)
                    .filter(|value| !value.is_empty())
                    .ok_or_else(|| {
                        anyhow!("could not read the existing Grafana admin credential")
                    })?;
                let key = reference
                    .get("key")
                    .and_then(serde_json::Value::as_str)
                    .filter(|value| !value.is_empty())
                    .ok_or_else(|| {
                        anyhow!("could not read the existing Grafana admin credential")
                    })?;
                references.push(SecretKeyReference {
                    name: name.to_string(),
                    key: key.to_string(),
                });
            }
        }
    }
    if references.len() != 1 {
        bail!("could not read the existing Grafana admin credential");
    }
    Ok(references.remove(0))
}

fn secret_data_value(
    secrets: &BTreeMap<String, serde_json::Value>,
    reference: &SecretKeyReference,
) -> Result<String> {
    let encoded = secrets
        .get(&reference.name)
        .and_then(|secret| secret.get("data"))
        .and_then(|data| data.get(&reference.key))
        .and_then(serde_json::Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| anyhow!("could not read the existing Grafana admin credential"))?;
    base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| anyhow!("could not read the existing Grafana admin credential"))?;
    Ok(encoded.to_string())
}

async fn ensure_observability_namespace(observability_namespace: &str) -> Result<()> {
    let inspect = tokio::process::Command::new("kubectl")
        .args(["get", "namespace", observability_namespace, "-o", "json"])
        .output()
        .await
        .context("inspecting the observability namespace")?;
    if inspect.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&inspect.stderr);
    let lower = stderr.to_ascii_lowercase();
    if !lower.contains("notfound") && !lower.contains("not found") {
        bail!(
            "could not inspect namespace {observability_namespace} with `kubectl get namespace {observability_namespace}`: {}",
            stderr.trim()
        );
    }
    let output = tokio::process::Command::new("kubectl")
        .args(["create", "namespace", observability_namespace])
        .output()
        .await
        .context("creating the observability namespace")?;
    if !output.status.success() {
        bail!(
            "could not create namespace {observability_namespace}; run `kubectl create namespace {observability_namespace}` and retry"
        );
    }
    Ok(())
}

async fn apply_private_manifest(
    manifest: &[u8],
    description: &str,
    observability_namespace: &str,
) -> Result<()> {
    let mut child = tokio::process::Command::new("kubectl")
        .args(["apply", "-f", "-"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("starting kubectl for {description}"))?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow!("kubectl stdin was unavailable for {description}"))?;
    stdin
        .write_all(manifest)
        .await
        .with_context(|| format!("writing {description} to kubectl stdin"))?;
    drop(stdin);
    let output = child
        .wait_with_output()
        .await
        .with_context(|| format!("waiting for kubectl to apply {description}"))?;
    if !output.status.success() {
        bail!(
            "could not apply {description} {GRAFANA_ADMIN_SECRET} in namespace {observability_namespace}; inspect access with `kubectl auth can-i create secret -n {observability_namespace}`"
        );
    }
    Ok(())
}

fn random_hex(bytes: usize) -> Result<String> {
    let mut value = vec![0u8; bytes];
    getrandom::fill(&mut value)
        .map_err(|error| anyhow!("OS random number generator unavailable: {error}"))?;
    Ok(value.iter().map(|byte| format!("{byte:02x}")).collect())
}

async fn run_install_command(
    command: &InstallCommand,
    workspace: &EmbeddedWorkspace,
    chart: &Path,
) -> Result<()> {
    let args = command
        .args
        .iter()
        .map(|arg| arg.live(workspace, chart))
        .collect::<Vec<_>>();
    crate::ui::ui().plumbing(&format!("+ {} {}", command.program, args.join(" ")));
    let output = tokio::process::Command::new(command.program)
        .args(&args)
        .output()
        .await
        .with_context(|| format!("failed to invoke `{}`; is it on PATH?", command.program))?;
    if output.status.success() {
        return Ok(());
    }

    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    if let Some(target) = &command.helm_target {
        if is_helm_timeout(&stderr) {
            let recovery = helm_pending_upgrade_recovery(target);
            return Err(crate::exit::CliError::failure(format!(
                "Helm timed out waiting for release {} in namespace {}: {}. Recover the pending upgrade with: {}",
                target.release,
                target.namespace,
                if stderr.is_empty() { "command timed out" } else { &stderr },
                recovery,
            ))
            .with_fix(recovery)
            .into());
        }
    }
    bail!(
        "`{}` failed: {}",
        command.display(chart),
        if stderr.is_empty() {
            "command exited nonzero"
        } else {
            &stderr
        }
    )
}

fn is_helm_timeout(stderr: &str) -> bool {
    let lower = stderr.to_ascii_lowercase();
    lower.contains("timed out")
        || lower.contains("timeout")
        || lower.contains("context deadline exceeded")
}

fn helm_pending_upgrade_recovery(target: &HelmTarget) -> String {
    format!(
        "kubectl delete secret -n {} -l 'owner=helm,name={},status=pending-upgrade'",
        target.namespace, target.release
    )
}

async fn read_only_connector_kubeconfig(namespace: &str) -> Result<String> {
    connector_kubeconfig(READER_IDENTITY, READER_TOKEN_SECRET, namespace).await
}

async fn write_connector_kubeconfig(namespace: &str) -> Result<String> {
    connector_kubeconfig(WRITER_IDENTITY, WRITER_TOKEN_SECRET, namespace).await
}

/// Apply the rendered write Role and mint the writer identity's kubeconfig.
///
/// One helper because the install issues this from two places -- before the
/// deploy on an upgrade, after it on a fresh install -- and the two orderings
/// must stay identical in what they apply and what they hand back.
async fn apply_write_access(
    write_role_command: &InstallCommand,
    workspace: &EmbeddedWorkspace,
    chart: &Path,
    namespace: &str,
) -> Result<String> {
    run_install_command(write_role_command, workspace, chart).await?;
    write_connector_kubeconfig(namespace).await
}

/// Does the gated write ServiceAccount already exist in `namespace`?
///
/// This is what picks the ordering of the write Role apply around the deploy, so
/// it must not confuse "absent" with "could not tell". `--ignore-not-found`
/// makes absence an exit-0 with empty stdout, which separates the two cleanly
/// without parsing stderr: any nonzero exit is a real probe failure and is
/// surfaced as an error. Reporting an unreadable cluster as a fresh install
/// would pick the deferred ordering on an upgrade and re-open exactly the
/// fail-open window this probe exists to close.
async fn writer_identity_exists(namespace: &str) -> Result<bool> {
    let args = [
        "get",
        "serviceaccount",
        WRITER_IDENTITY,
        "--namespace",
        namespace,
        "--ignore-not-found",
        "-o",
        "name",
    ];
    crate::ui::ui().plumbing(&format!("+ kubectl {}", args.join(" ")));
    let output = tokio::process::Command::new("kubectl")
        .args(args)
        .output()
        .await
        .context("probing for the gated write ServiceAccount")?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        bail!(
            "could not determine whether ServiceAccount {WRITER_IDENTITY} already exists in namespace {namespace}, and the write Role apply is ordered on that answer; check the cluster with `kubectl get serviceaccount {WRITER_IDENTITY} -n {namespace}` and retry: {}",
            if stderr.trim().is_empty() {
                "kubectl exited nonzero"
            } else {
                stderr.trim()
            }
        );
    }
    Ok(!String::from_utf8_lossy(&output.stdout).trim().is_empty())
}

/// Build one connector's in-memory kubeconfig from a ServiceAccount token Secret.
///
/// Shared by the read and write identities so they cannot drift in how they
/// verify the token: both refuse an absent, malformed, or empty token rather
/// than emitting a kubeconfig the connector would only fail on later.
async fn connector_kubeconfig(
    identity: &str,
    token_secret: &str,
    namespace: &str,
) -> Result<String> {
    let wait_args = [
        "wait",
        "--namespace",
        namespace,
        "--for=jsonpath={.data.token}",
        &format!("secret/{token_secret}"),
        &format!("--timeout={READER_TOKEN_TIMEOUT}"),
    ];
    crate::ui::ui().plumbing(&format!("+ kubectl {}", wait_args.join(" ")));
    let wait = tokio::process::Command::new("kubectl")
        .args(wait_args)
        .output()
        .await
        .context("waiting for the SRE bot ServiceAccount token")?;
    if !wait.status.success() {
        let stderr = String::from_utf8_lossy(&wait.stderr);
        bail!(
            "the connector token {token_secret} was not populated within {READER_TOKEN_TIMEOUT}: {}. Inspect it with `kubectl get secret {token_secret} -n {namespace}` and retry",
            if stderr.trim().is_empty() {
                "kubectl wait exited nonzero"
            } else {
                stderr.trim()
            }
        );
    }

    let get_args = [
        "get",
        "secret",
        token_secret,
        "--namespace",
        namespace,
        "-o",
        "json",
    ];
    let output = tokio::process::Command::new("kubectl")
        .args(get_args)
        .output()
        .await
        .context("reading the SRE bot ServiceAccount token")?;
    if !output.status.success() {
        bail!(
            "could not read Secret {token_secret} in namespace {namespace}; inspect it with `kubectl get secret {token_secret} -n {namespace}` and retry"
        );
    }
    let secret: serde_json::Value = serde_json::from_slice(&output.stdout)
        .context("the SRE bot token Secret returned malformed JSON")?;
    let data = secret
        .get("data")
        .and_then(serde_json::Value::as_object)
        .context("the SRE bot token Secret has no data")?;
    let ca = data
        .get("ca.crt")
        .and_then(serde_json::Value::as_str)
        .context("the SRE bot token Secret has no ca.crt")?;
    base64::engine::general_purpose::STANDARD
        .decode(ca)
        .context("the SRE bot token Secret contains an invalid ca.crt")?;
    let token = data
        .get("token")
        .and_then(serde_json::Value::as_str)
        .context("the SRE bot token Secret has no token")?;
    let token = base64::engine::general_purpose::STANDARD
        .decode(token)
        .context("the SRE bot token Secret contains an invalid token")?;
    let token =
        String::from_utf8(token).context("the SRE bot token Secret contains a non UTF-8 token")?;
    if token.is_empty() {
        bail!("the SRE bot token Secret contains an empty token");
    }

    serde_json::to_string(&serde_json::json!({
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{
            "name": "in-cluster",
            "cluster": {
                "server": "https://kubernetes.default.svc",
                "certificate-authority-data": ca,
            },
        }],
        "users": [{
            "name": identity,
            "user": {"token": token},
        }],
        "contexts": [{
            "name": identity,
            "context": {"cluster": "in-cluster", "user": identity},
        }],
        "current-context": identity,
    }))
    .context("serializing the read only connector kubeconfig")
}

struct EmbeddedClusterConnection {
    api_url: String,
    api_key: String,
    _port_forward: Option<tokio::process::Child>,
}

async fn resolve_embedded_cluster_connection(
    identity: &InstallIdentity,
) -> Result<EmbeddedClusterConnection> {
    let api_key = crate::ops::discover_api_key(&identity.namespace, &identity.release).await?;
    let explicit_api_url = std::env::var("CURIE_API_URL")
        .ok()
        .filter(|value| !value.trim().is_empty());
    let local_port = crate::message::DEFAULT_API_LOCAL_PORT;
    let tunnel = commands::deploy_api_tunnel(
        explicit_api_url.as_deref(),
        &identity.namespace,
        &identity.release,
        local_port,
        crate::message::API_REMOTE_PORT,
    )
    .await;
    let (api_url, port_forward) = match tunnel {
        Some((_fullname, command)) => {
            let (child, effective_port) =
                crate::message::start_port_forward(&command, local_port, "SRE bot deploy API")
                    .await?;
            (format!("http://localhost:{effective_port}"), Some(child))
        }
        None => {
            let url = explicit_api_url.expect("explicit API URL when no port forward is planned");
            if crate::api::is_insecure_endpoint(&url) {
                bail!(
                    "refusing to send the auto-discovered release key over cleartext HTTP to {url}; use an https:// URL or unset CURIE_API_URL to use the loopback port-forward"
                );
            }
            (url, None)
        }
    };
    Ok(EmbeddedClusterConnection {
        api_url,
        api_key,
        _port_forward: port_forward,
    })
}

async fn deploy_embedded_sre_bot(
    bundle_dir: &Path,
    connection: &EmbeddedClusterConnection,
    slack_channel: Option<&str>,
) -> Result<commands::DeployOutput> {
    let connect_hint = format!(
        "the platform API at {} is unreachable; confirm the Curie release with `curie cluster status` and retry this installer",
        connection.api_url
    );
    commands::deploy(DeployOpts {
        agent: None,
        target: None,
        plugin_dir: bundle_dir.to_path_buf(),
        api_url: connection.api_url.clone(),
        api_key: connection.api_key.clone(),
        slack_channel: slack_channel.map(str::to_string),
        repo: None,
        workspace: commands::WorkspaceIntent::Preserve,
        env: None,
        label: None,
        secret: vec![],
        secret_binding_supported: false,
        connect_hint,
        tier: DeployTier::Cluster,
    })
    .await
}

struct EmbeddedWorkspace {
    root: PathBuf,
}

impl EmbeddedWorkspace {
    fn create(
        tempo_digest: &str,
        write_digest: Option<&str>,
        write_targets: Option<&[WriteTarget]>,
        identity: &InstallIdentity,
        upgrade_digest: Option<&str>,
    ) -> Result<Self> {
        let root = std::env::temp_dir().join(format!(
            "curie-sre-bot-install-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4()
        ));
        std::fs::create_dir(&root)
            .with_context(|| format!("creating embedded SRE bot workspace {}", root.display()))?;
        let workspace = Self { root };
        for (name, contents) in OBSERVABILITY_FILES {
            let rendered =
                rewrite_observability_namespace(contents, &identity.observability_namespace);
            workspace.write(&Path::new("observability").join(name), &rendered)?;
        }
        for (name, contents) in BUNDLE_FILES {
            if *name == "connectors.yaml" {
                let runtime = runtime_connector_declaration(
                    contents,
                    tempo_digest,
                    write_digest,
                    write_targets,
                    &identity.observability_namespace,
                    upgrade_digest,
                )?;
                workspace.write(&Path::new("bundle").join(name), &runtime)?;
            } else if *name == ".claude-plugin/plugin.json" {
                let runtime = runtime_plugin_manifest(
                    contents,
                    write_targets.is_some(),
                    upgrade_digest.is_some(),
                )?;
                workspace.write(&Path::new("bundle").join(name), &runtime)?;
            } else if *name == "manifests/write-role.yaml" {
                // Rendered from the allowlist when the write path is opted into,
                // and NOT written at all otherwise. Shipping the shipped
                // placeholder next to read-access.yaml -- which this install does
                // apply -- reads as though a Deployment patch grant were in
                // effect, when every consumer of it was stripped above.
                match write_targets {
                    Some(targets) => {
                        let rendered = render_write_role(contents, targets, &identity.namespace)?;
                        workspace.write(&Path::new("bundle").join(name), &rendered)?;
                    }
                    None => continue,
                }
            } else if *name == "manifests/upgrade-role.yaml" {
                // Both upgrade identities are written only when the path is
                // opted into. Writing them otherwise would leave manifests
                // describing grants this install deliberately did not create,
                // next to ones it did -- the same trap the write Role avoids.
                if upgrade_digest.is_none() {
                    continue;
                }
                let rendered = render_upgrade_role(contents, &identity.namespace)?;
                workspace.write(&Path::new("bundle").join(name), &rendered)?;
            } else if *name == "manifests/platform-upgrade-role.yaml" {
                if upgrade_digest.is_none() {
                    continue;
                }
                let rendered = render_platform_upgrade_role(contents, &identity.namespace)?;
                workspace.write(&Path::new("bundle").join(name), &rendered)?;
            } else if *name == "manifests/read-access.yaml" {
                let rendered = render_read_access(contents, &identity.namespace)?;
                workspace.write(&Path::new("bundle").join(name), &rendered)?;
            } else {
                workspace.write(&Path::new("bundle").join(name), contents)?;
            }
        }
        if upgrade_digest.is_some() {
            // Not bundle files: cluster objects this installer renders and
            // applies. They live beside the bundle in the workspace so a failed
            // install leaves exactly what was about to be applied on disk.
            workspace.write(
                Path::new("bundle/manifests/platform-upgrade-configmap.yaml"),
                &render_platform_upgrade_configmap(PLATFORM_UPGRADE_SCRIPT, &identity.namespace)?,
            )?;
            workspace.write(
                Path::new("bundle/manifests/platform-upgrade-cronjob.yaml"),
                &render_platform_cronjob(
                    PLATFORM_UPGRADE_CRONJOB_YAML,
                    &identity.namespace,
                    &identity.release,
                    PLATFORM_UPGRADE_SOURCE_REPO,
                )?,
            )?;
        }
        Ok(workspace)
    }

    fn write(&self, relative: &Path, contents: &[u8]) -> Result<()> {
        let path = self.root.join(relative);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating {}", parent.display()))?;
        }
        std::fs::write(&path, contents).with_context(|| format!("writing {}", path.display()))
    }

    fn observability_dir(&self) -> PathBuf {
        self.root.join("observability")
    }

    fn bundle_dir(&self) -> PathBuf {
        self.root.join("bundle")
    }
}

/// One `namespace/name` the write connector may target.
///
/// The SAME parsed list renders both ceilings: the Role's `resourceNames` and
/// the connector's `K8S_WRITE_ALLOWLIST`. They are two allowlists over the same
/// question in two files, and editing one without the other is not hypothetical
/// -- downstream they disagreed for four days after a cluster changed, in the
/// direction that 403s AFTER a human approved the call. Deriving both from one
/// input makes that disagreement unrepresentable rather than merely detectable.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct WriteTarget {
    namespace: String,
    name: String,
}

impl WriteTarget {
    fn qualified(&self) -> String {
        format!("{}/{}", self.namespace, self.name)
    }
}

fn valid_dns_label(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 253
        && value
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '.')
        && !value.starts_with('-')
        && !value.ends_with('-')
}

/// Parse `--write-allowlist ns/name[,ns/name]`, refusing anything that would
/// produce a grant nobody chose.
pub fn parse_write_allowlist(raw: &str) -> Result<Vec<WriteTarget>> {
    let mut targets: Vec<WriteTarget> = Vec::new();
    for entry in raw.split(',').map(str::trim).filter(|e| !e.is_empty()) {
        // The placeholder the bundle ships. Accepting it would render a Role
        // granting patch on a Deployment literally named `<deployment>`, which
        // is inert but reads as configured.
        if entry.contains('<') || entry.contains('>') {
            bail!(
                "--write-allowlist got the placeholder {entry:?}; replace it with a real \
                 namespace/name pair"
            );
        }
        let (namespace, name) = entry.split_once('/').with_context(|| {
            format!("--write-allowlist entry {entry:?} is not in namespace/name form")
        })?;
        if !valid_dns_label(namespace) || !valid_dns_label(name) {
            bail!(
                "--write-allowlist entry {entry:?} is not a valid namespace/name pair \
                 (lowercase alphanumeric, '-' and '.')"
            );
        }
        targets.push(WriteTarget {
            namespace: namespace.to_string(),
            name: name.to_string(),
        });
    }
    if targets.is_empty() {
        bail!("--write-allowlist needs at least one namespace/name pair");
    }
    targets.sort();
    targets.dedup();
    Ok(targets)
}

fn write_allowlist_value(targets: &[WriteTarget]) -> String {
    targets
        .iter()
        .map(WriteTarget::qualified)
        .collect::<Vec<_>>()
        .join(",")
}

/// Render the write identity for `targets` from the shipped manifest's own rule.
///
/// The ServiceAccount and its token Secret live in the release namespace; each
/// target namespace gets a Role naming only its own Deployments plus a
/// RoleBinding back to that one ServiceAccount. The verb set is read from
/// `manifests/write-role.yaml` and asserted against what this build knows how to
/// grant, so widening that file stops the install rather than shipping in it.
fn rewrite_observability_namespace(contents: &[u8], observability_namespace: &str) -> Vec<u8> {
    if observability_namespace == OBSERVABILITY_NAMESPACE {
        return contents.to_vec();
    }
    let text = String::from_utf8_lossy(contents);
    text.replace(
        &format!(".{OBSERVABILITY_NAMESPACE}.svc.cluster.local"),
        &format!(".{observability_namespace}.svc.cluster.local"),
    )
    .replace(
        &format!("namespace: {OBSERVABILITY_NAMESPACE}"),
        &format!("namespace: {observability_namespace}"),
    )
    .into_bytes()
}

fn rewrite_manifest_namespace(value: &mut serde_json::Value, namespace: &str) {
    if let Some(metadata) = value
        .get_mut("metadata")
        .and_then(serde_json::Value::as_object_mut)
    {
        if metadata.contains_key("namespace") {
            metadata.insert(
                "namespace".to_string(),
                serde_json::Value::String(namespace.to_string()),
            );
        }
    }
    if let Some(subjects) = value
        .get_mut("subjects")
        .and_then(serde_json::Value::as_array_mut)
    {
        for subject in subjects {
            if let Some(object) = subject.as_object_mut() {
                if object.contains_key("namespace") {
                    object.insert(
                        "namespace".to_string(),
                        serde_json::Value::String(namespace.to_string()),
                    );
                }
            }
        }
    }
}

fn render_read_access(source: &[u8], namespace: &str) -> Result<Vec<u8>> {
    if namespace == CURIE_NAMESPACE {
        return Ok(source.to_vec());
    }
    let source =
        std::str::from_utf8(source).context("embedded SRE bot read-access.yaml is not UTF-8")?;
    let mut rendered = String::new();
    for document in serde_norway::Deserializer::from_str(source) {
        let mut value: serde_json::Value = serde::Deserialize::deserialize(document)
            .context("parsing embedded SRE bot read-access.yaml")?;
        rewrite_manifest_namespace(&mut value, namespace);
        rendered.push_str("---\n");
        rendered.push_str(
            &serde_norway::to_string(&value)
                .context("serializing the rendered SRE bot read identity")?,
        );
    }
    Ok(rendered.into_bytes())
}

/// The CONNECTOR's upgrade identity, in the install's namespace.
///
/// A namespace rewrite and nothing else: unlike the write Role, its grant is
/// fixed rather than derived from operator input, so there is no allowlist to
/// render and nothing for this to get wrong beyond the namespace.
fn render_upgrade_role(source: &[u8], namespace: &str) -> Result<Vec<u8>> {
    if namespace == CURIE_NAMESPACE {
        return Ok(source.to_vec());
    }
    let source =
        std::str::from_utf8(source).context("embedded SRE bot upgrade-role.yaml is not UTF-8")?;
    let mut rendered = String::new();
    for document in serde_norway::Deserializer::from_str(source) {
        let mut value: serde_json::Value = serde::Deserialize::deserialize(document)
            .context("parsing embedded SRE bot upgrade-role.yaml")?;
        rewrite_manifest_namespace(&mut value, namespace);
        rendered.push_str("---\n");
        rendered.push_str(
            &serde_norway::to_string(&value)
                .context("serializing the rendered SRE bot upgrade identity")?,
        );
    }
    Ok(rendered.into_bytes())
}

/// The ConfigMap carrying the upgrade script the Job runs.
///
/// Rendered rather than `kubectl create configmap --from-file`, so the whole
/// install is one stream of `kubectl apply` over files this process wrote --
/// idempotent on a re-install, and inspectable in the workspace when it fails.
fn render_platform_upgrade_configmap(script: &[u8], namespace: &str) -> Result<Vec<u8>> {
    let script = std::str::from_utf8(script)
        .context("embedded SRE bot platform upgrade script is not UTF-8")?;
    let document = serde_json::json!({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": PLATFORM_UPGRADE_CONFIGMAP, "namespace": namespace},
        "data": {"upgrade.sh": script},
    });
    let mut rendered = String::from("---\n");
    rendered.push_str(
        &serde_norway::to_string(&document)
            .context("serializing the SRE bot platform upgrade ConfigMap")?,
    );
    Ok(rendered.into_bytes())
}

fn render_write_role(
    source: &[u8],
    targets: &[WriteTarget],
    curie_namespace: &str,
) -> Result<Vec<u8>> {
    let source =
        std::str::from_utf8(source).context("embedded SRE bot write-role.yaml is not UTF-8")?;
    let mut rule: Option<serde_json::Value> = None;
    for document in serde_norway::Deserializer::from_str(source) {
        let value: serde_json::Value = serde::Deserialize::deserialize(document)
            .context("parsing embedded SRE bot write-role.yaml")?;
        if value.get("kind").and_then(serde_json::Value::as_str) != Some("Role") {
            continue;
        }
        let rules = value
            .get("rules")
            .and_then(serde_json::Value::as_array)
            .context("the embedded write Role declares no rules")?;
        if rules.len() != 1 {
            bail!(
                "the embedded write Role declares {} rules; this build renders exactly one",
                rules.len()
            );
        }
        rule = Some(rules[0].clone());
    }
    let rule = rule.context("the embedded write-role.yaml declares no Role")?;
    for (field, expected) in [
        ("apiGroups", WRITE_RULE_API_GROUPS.as_slice()),
        ("resources", WRITE_RULE_RESOURCES.as_slice()),
        ("verbs", WRITE_RULE_VERBS.as_slice()),
    ] {
        let actual: Vec<&str> = rule
            .get(field)
            .and_then(serde_json::Value::as_array)
            .map(|items| items.iter().filter_map(serde_json::Value::as_str).collect())
            .unwrap_or_default();
        if actual != expected {
            bail!(
                "the embedded write Role's {field} are {actual:?}, but this build only knows how \
                 to render {expected:?}; widening the grant needs a matching change here"
            );
        }
    }

    let mut documents: Vec<serde_json::Value> = vec![
        serde_json::json!({
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": WRITER_IDENTITY, "namespace": curie_namespace},
        }),
        serde_json::json!({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": WRITER_TOKEN_SECRET,
                "namespace": curie_namespace,
                "annotations": {"kubernetes.io/service-account.name": WRITER_IDENTITY},
            },
            "type": "kubernetes.io/service-account-token",
        }),
    ];
    let mut namespaces: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for target in targets {
        namespaces
            .entry(target.namespace.as_str())
            .or_default()
            .push(target.name.as_str());
    }
    for (namespace, names) in namespaces {
        documents.push(serde_json::json!({
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": WRITER_IDENTITY, "namespace": namespace},
            "rules": [{
                "apiGroups": WRITE_RULE_API_GROUPS,
                "resources": WRITE_RULE_RESOURCES,
                "resourceNames": names,
                "verbs": WRITE_RULE_VERBS,
            }],
        }));
        documents.push(serde_json::json!({
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": WRITER_IDENTITY, "namespace": namespace},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": WRITER_IDENTITY,
            },
            "subjects": [{
                "kind": "ServiceAccount",
                "name": WRITER_IDENTITY,
                "namespace": curie_namespace,
            }],
        }));
    }
    let mut rendered = String::new();
    for document in documents {
        rendered.push_str("---\n");
        rendered.push_str(
            &serde_norway::to_string(&document)
                .context("serializing the rendered SRE bot write identity")?,
        );
    }
    Ok(rendered.into_bytes())
}

/// The platform-upgrade identity, rendered into the release's namespace.
///
/// Mirrors `render_write_role`, including the part that matters most: the
/// shipped manifest's rules are ASSERTED against what this build knows how to
/// render before anything is emitted. This is the widest grant in the bundle --
/// namespace-admin in all but name -- so a manifest that grows a rule must stop
/// the install rather than have the installer grant it silently.
fn render_platform_upgrade_role(source: &[u8], curie_namespace: &str) -> Result<Vec<u8>> {
    let source = std::str::from_utf8(source)
        .context("embedded SRE bot platform-upgrade-role.yaml is not UTF-8")?;
    let mut rules: Option<Vec<serde_json::Value>> = None;
    for document in serde_norway::Deserializer::from_str(source) {
        let value: serde_json::Value = serde::Deserialize::deserialize(document)
            .context("parsing embedded SRE bot platform-upgrade-role.yaml")?;
        if value.get("kind").and_then(serde_json::Value::as_str) != Some("Role") {
            continue;
        }
        rules = Some(
            value
                .get("rules")
                .and_then(serde_json::Value::as_array)
                .context("the embedded platform-upgrade Role declares no rules")?
                .clone(),
        );
    }
    let rules = rules.context("the embedded platform-upgrade-role.yaml declares no Role")?;
    if rules.len() != PLATFORM_RULE_SHAPE.len() {
        bail!(
            "the embedded platform-upgrade Role declares {} rules; this build renders exactly \
             {}. Widening the grant needs a matching change here, because this installer is \
             what creates it.",
            rules.len(),
            PLATFORM_RULE_SHAPE.len()
        );
    }
    for (index, (group, resources)) in PLATFORM_RULE_SHAPE.iter().enumerate() {
        let rule = &rules[index];
        let groups: Vec<&str> = rule
            .get("apiGroups")
            .and_then(serde_json::Value::as_array)
            .map(|items| items.iter().filter_map(serde_json::Value::as_str).collect())
            .unwrap_or_default();
        let actual: Vec<&str> = rule
            .get("resources")
            .and_then(serde_json::Value::as_array)
            .map(|items| items.iter().filter_map(serde_json::Value::as_str).collect())
            .unwrap_or_default();
        if groups != [*group] || actual != *resources {
            bail!(
                "the embedded platform-upgrade Role's rule {index} is {groups:?}/{actual:?}, but \
                 this build only knows how to render {:?}/{resources:?}",
                [group]
            );
        }
    }

    let mut documents: Vec<serde_json::Value> = vec![
        serde_json::json!({
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": PLATFORM_UPGRADER_IDENTITY, "namespace": curie_namespace},
        }),
        serde_json::json!({
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": PLATFORM_UPGRADER_IDENTITY, "namespace": curie_namespace},
            "rules": rules,
        }),
        serde_json::json!({
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": PLATFORM_UPGRADER_IDENTITY, "namespace": curie_namespace},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": PLATFORM_UPGRADER_IDENTITY,
            },
            "subjects": [{
                "kind": "ServiceAccount",
                "name": PLATFORM_UPGRADER_IDENTITY,
                "namespace": curie_namespace,
            }],
        }),
    ];
    // No static token Secret, unlike the reader and writer identities. This one
    // is a Job's ServiceAccount: Kubernetes projects its token into the pod for
    // the ninety seconds the upgrade runs. A static token would be a
    // namespace-admin credential sitting in a Secret forever, which is exactly
    // what this shape exists to avoid.
    let mut rendered = String::new();
    for document in documents.drain(..) {
        rendered.push_str("---\n");
        rendered.push_str(
            &serde_norway::to_string(&document)
                .context("serializing the rendered SRE bot platform upgrade identity")?,
        );
    }
    Ok(rendered.into_bytes())
}

/// The platform-upgrade CronJob, pointed at this install.
///
/// The shipped file carries `CHANGE ME` placeholders for the namespace, the
/// release and the repository. Rewriting them here is the whole reason this flag
/// exists: an operator editing four values across two files gets one of them
/// wrong, and the failure is a tool that refuses every call with nothing visibly
/// wrong in either file.
fn render_platform_cronjob(
    source: &[u8],
    curie_namespace: &str,
    release: &str,
    repo: &str,
) -> Result<Vec<u8>> {
    let source = std::str::from_utf8(source)
        .context("embedded SRE bot platform-upgrade cronjob.yaml is not UTF-8")?;
    let mut cronjob: Option<serde_json::Value> = None;
    for document in serde_norway::Deserializer::from_str(source) {
        let value: serde_json::Value = serde::Deserialize::deserialize(document)
            .context("parsing embedded SRE bot platform-upgrade cronjob.yaml")?;
        if value.get("kind").and_then(serde_json::Value::as_str) == Some("CronJob") {
            cronjob = Some(value);
        }
    }
    let mut cronjob =
        cronjob.context("the embedded platform-upgrade cronjob.yaml has no CronJob")?;

    let metadata = cronjob
        .get_mut("metadata")
        .and_then(serde_json::Value::as_object_mut)
        .context("the embedded platform-upgrade CronJob has no metadata")?;
    metadata.insert(
        "namespace".to_string(),
        serde_json::Value::String(curie_namespace.to_string()),
    );
    // The name the connector is told about. Asserted rather than trusted: if the
    // shipped file is renamed, the connector's env would point at a CronJob that
    // does not exist and every call would refuse.
    let name = metadata
        .get("name")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default();
    if name != PLATFORM_UPGRADE_CRONJOB_NAME {
        bail!(
            "the embedded platform-upgrade CronJob is named {name:?}, but this build tells the \
             connector to start {PLATFORM_UPGRADE_CRONJOB_NAME:?}"
        );
    }
    // Suspended is not negotiable here. This installer's contract is that the
    // upgrade happens when a human approves one, never on a timer nobody chose.
    cronjob
        .pointer_mut("/spec")
        .and_then(serde_json::Value::as_object_mut)
        .context("the embedded platform-upgrade CronJob has no spec")?
        .insert("suspend".to_string(), serde_json::Value::Bool(true));

    let env = cronjob
        .pointer_mut("/spec/jobTemplate/spec/template/spec/containers/0/env")
        .and_then(serde_json::Value::as_array_mut)
        .context("the embedded platform-upgrade CronJob container declares no env")?;
    let mut seen = 0usize;
    for entry in env.iter_mut() {
        let Some(object) = entry.as_object_mut() else {
            continue;
        };
        let replacement = match object.get("name").and_then(serde_json::Value::as_str) {
            Some("PLATFORM_UPGRADE_NAMESPACE") => curie_namespace,
            Some("PLATFORM_UPGRADE_RELEASE") => release,
            Some("PLATFORM_UPGRADE_REPO") => repo,
            _ => continue,
        };
        object.insert(
            "value".to_string(),
            serde_json::Value::String(replacement.to_string()),
        );
        seen += 1;
    }
    if seen != 3 {
        bail!(
            "the embedded platform-upgrade CronJob carries {seen} of the 3 values this build \
             rewrites (namespace, release, repository); the rest would keep their placeholders"
        );
    }

    let mut rendered = String::from("---\n");
    rendered.push_str(
        &serde_norway::to_string(&cronjob)
            .context("serializing the rendered SRE bot platform upgrade CronJob")?,
    );
    Ok(rendered.into_bytes())
}

fn runtime_connector_declaration(
    source: &[u8],
    tempo_digest: &str,
    write_digest: Option<&str>,
    write_targets: Option<&[WriteTarget]>,
    observability_namespace: &str,
    upgrade_digest: Option<&str>,
) -> Result<Vec<u8>> {
    let source =
        std::str::from_utf8(source).context("embedded SRE bot connectors.yaml is not UTF-8")?;
    let mut declaration: serde_json::Value =
        serde_norway::from_str(source).context("parsing embedded SRE bot connectors.yaml")?;
    let connectors = declaration
        .get_mut("connectors")
        .and_then(serde_json::Value::as_object_mut)
        .context("embedded SRE bot must declare connectors")?;
    match write_targets {
        // Opt-in: keep the one gated write connector and point its allowlist at
        // the SAME targets the Role was rendered from.
        Some(targets) => {
            let write = connectors
                .get_mut("k8s-write")
                .and_then(serde_json::Value::as_object_mut)
                .context("embedded SRE bot must declare connectors.k8s-write")?;
            let env = write
                .entry("env")
                .or_insert_with(|| serde_json::Value::Object(Default::default()))
                .as_object_mut()
                .context("embedded SRE bot k8s-write connector env is not a mapping")?;
            env.insert(
                WRITE_ALLOWLIST_ENV.to_string(),
                serde_json::Value::String(write_allowlist_value(targets)),
            );
        }
        None => {
            if connectors.remove("k8s-write").is_none() {
                bail!("embedded SRE bot must declare connectors.k8s-write");
            }
        }
    }
    // Scale stays out either way: a separate blast radius (scale to zero is an
    // outage) deserves its own opt-in, not a ride on this one.
    if connectors.remove("k8s-scale").is_none() {
        bail!("embedded SRE bot must declare connectors.k8s-scale");
    }
    // Self-upgrade stays out too, and for a stronger reason than scale. It is
    // inert without a CronJob this installer does not create, and its identity
    // holds namespace-wide `create` on `jobs` in the namespace that holds the
    // platform API key (see manifests/upgrade-role.yaml). A grant that wide is
    // an operator's decision made while reading that file, never a side effect
    // of running an installer.
    match upgrade_digest {
        // Kept, with both CronJob names filled in. The bundle ships
        // PLATFORM_UPGRADE_CRONJOB empty and SELF_UPGRADE_CRONJOB defaulted, and
        // an install that hand-edits either finds the worker's connector
        // reconciler putting the declaration back within the minute -- so the
        // installer is the only thing that can make these real.
        Some(digest) => {
            let upgrade = connectors
                .get_mut("self-upgrade")
                .and_then(serde_json::Value::as_object_mut)
                .context("embedded SRE bot must declare connectors.self-upgrade")?;
            if upgrade.remove("build").is_none() || upgrade.contains_key("image") {
                bail!(
                    "embedded SRE bot self-upgrade connector must declare one build source and \
                     no image before immutable resolution"
                );
            }
            upgrade.insert(
                "image".to_string(),
                serde_json::Value::String(format!("{SELF_UPGRADE_IMAGE_REPOSITORY}@{digest}")),
            );
            let env = upgrade
                .entry("env")
                .or_insert_with(|| serde_json::Value::Object(Default::default()))
                .as_object_mut()
                .context("embedded SRE bot self-upgrade connector env is not a mapping")?;
            env.insert(
                PLATFORM_UPGRADE_CRONJOB_ENV.to_string(),
                serde_json::Value::String(PLATFORM_UPGRADE_CRONJOB_NAME.to_string()),
            );
            env.insert(
                SELF_UPGRADE_CRONJOB_ENV.to_string(),
                serde_json::Value::String(SELF_UPGRADE_CRONJOB_NAME.to_string()),
            );
        }
        // Stripped exactly as before this flag existed: inert without the Job,
        // and its identity is an operator's decision, never an installer's.
        None => {
            if connectors.remove("self-upgrade").is_none() {
                bail!("embedded SRE bot must declare connectors.self-upgrade");
            }
        }
    }
    // Fail closed on a connector this build does not know about. Removing the
    // write connectors by name is only as narrow as intended if the bundle has no
    // third one -- and the bundle is edited far more often than this file, so an
    // unrecognized connector must stop the install rather than ship in it.
    let known: &[&str] = match (write_targets.is_some(), upgrade_digest.is_some()) {
        (true, true) => &[
            "kubernetes",
            "grafana",
            "tempo",
            "k8s-write",
            "self-upgrade",
        ],
        (true, false) => &["kubernetes", "grafana", "tempo", "k8s-write"],
        (false, true) => &["kubernetes", "grafana", "tempo", "self-upgrade"],
        (false, false) => &["kubernetes", "grafana", "tempo"],
    };
    if let Some(unexpected) = connectors
        .keys()
        .find(|name| !known.contains(&name.as_str()))
    {
        bail!(
            "embedded SRE bot declares connector {unexpected}, which this build does not \
             know how to classify; the read only install ships only read connectors"
        );
    }
    let tempo = connectors
        .get_mut("tempo")
        .and_then(serde_json::Value::as_object_mut)
        .context("embedded SRE bot must declare connectors.tempo")?;
    if tempo.remove("build").is_none() || tempo.contains_key("image") {
        bail!(
            "embedded SRE bot Tempo connector must declare one build source and no image before immutable resolution"
        );
    }
    tempo.insert(
        "image".to_string(),
        serde_json::Value::String(format!("{TEMPO_IMAGE_REPOSITORY}@{tempo_digest}")),
    );
    // The kept write connector gets the same treatment, and for the same reason:
    // `build:` records a LOCAL image id, which the cluster tier refuses because a
    // cluster cannot pull an image that exists only in one machine's Docker
    // daemon. Without this the opt-in produced a bundle whose write path could
    // never come up. Deliberately after the known-connector check above, so an
    // unrecognised connector is still reported as one rather than as a missing
    // image.
    if write_targets.is_some() {
        let digest =
            write_digest.context("keeping connectors.k8s-write needs its resolved image digest")?;
        let write = connectors
            .get_mut("k8s-write")
            .and_then(serde_json::Value::as_object_mut)
            .context("embedded SRE bot must declare connectors.k8s-write")?;
        if write.remove("build").is_none() || write.contains_key("image") {
            bail!(
                "embedded SRE bot k8s-write connector must declare one build source and no \
                 image before immutable resolution"
            );
        }
        write.insert(
            "image".to_string(),
            serde_json::Value::String(format!("{K8S_WRITE_IMAGE_REPOSITORY}@{digest}")),
        );
    }
    let serialized = serde_norway::to_string(&declaration)
        .context("serializing the immutable SRE bot connector declaration")?;
    Ok(rewrite_observability_namespace(
        serialized.as_bytes(),
        observability_namespace,
    ))
}

fn runtime_plugin_manifest(
    source: &[u8],
    write_enabled: bool,
    upgrade_enabled: bool,
) -> Result<Vec<u8>> {
    let mut manifest: serde_json::Value =
        serde_json::from_slice(source).context("parsing embedded SRE bot plugin.json")?;
    // Pinned, not merely present. This install strips approvalPolicy entirely
    // because it ships read only, and stripping a gate is only safe when the
    // connector it guards is also gone -- so an unrecognized gate means the
    // bundle grew a write verb this build does not know to remove.
    let expected_policy = serde_json::json!({
        "gates": [
            {"gate": WRITE_GATE, "route": "sre-approvals"},
            {"gate": SCALE_GATE, "route": "sre-approvals"},
            {"gate": UPGRADE_GATE, "route": "sre-approvals"},
            {"gate": PLATFORM_UPGRADE_GATE, "route": "sre-approvals"}
        ]
    });
    if manifest.get("approvalPolicy") != Some(&expected_policy) {
        bail!("embedded SRE bot must declare the exact gated write verbs");
    }
    let manifest = manifest
        .as_object_mut()
        .context("embedded SRE bot plugin.json must be an object")?;
    // Keep exactly the gates whose connectors survived, and no others. A gate
    // naming a stripped connector fails bundle validation for everyone; a kept
    // connector with no gate is the ungated write this whole path exists to
    // avoid. Both halves are decided here, from the same two conditions.
    let mut kept: Vec<serde_json::Value> = Vec::new();
    if write_enabled {
        kept.push(serde_json::json!({"gate": WRITE_GATE, "route": "sre-approvals"}));
    }
    if upgrade_enabled {
        kept.push(serde_json::json!({"gate": UPGRADE_GATE, "route": "sre-approvals"}));
        kept.push(serde_json::json!({"gate": PLATFORM_UPGRADE_GATE, "route": "sre-approvals"}));
    }
    if !kept.is_empty() {
        // Keep exactly the gate for the connector that stayed. A gate naming a
        // connector this install removed fails bundle validation for everyone,
        // and a connector kept without its gate is the ungated write this whole
        // path exists to avoid -- so the two are decided together, here, from one
        // condition.
        manifest.insert(
            "approvalPolicy".to_string(),
            serde_json::json!({"gates": kept}),
        );
        manifest.insert(
            "description".to_string(),
            serde_json::Value::String(RUNTIME_PLUGIN_WRITE_DESCRIPTION.to_string()),
        );
    } else {
        manifest.remove("approvalPolicy");
        manifest.insert(
            "description".to_string(),
            serde_json::Value::String(RUNTIME_PLUGIN_DESCRIPTION.to_string()),
        );
    }
    serde_json::to_vec_pretty(&manifest).context("serializing the SRE bot plugin manifest")
}

impl Drop for EmbeddedWorkspace {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

#[derive(Deserialize)]
struct KubeList<T> {
    items: Vec<T>,
}

#[derive(Deserialize)]
struct Node {
    metadata: ObjectMeta,
    #[serde(default)]
    spec: NodeSpec,
    status: NodeStatus,
}

#[derive(Deserialize)]
struct ObjectMeta {
    name: String,
    #[serde(default)]
    namespace: String,
    #[serde(default)]
    labels: BTreeMap<String, String>,
}

#[derive(Default, Deserialize)]
struct NodeSpec {
    #[serde(default)]
    unschedulable: bool,
}

#[derive(Deserialize)]
struct NodeStatus {
    allocatable: BTreeMap<String, String>,
    conditions: Vec<NodeCondition>,
}

#[derive(Deserialize)]
struct NodeCondition {
    #[serde(rename = "type")]
    kind: String,
    status: String,
}

#[derive(Deserialize)]
struct Pod {
    metadata: ObjectMeta,
    spec: PodSpec,
    status: PodStatus,
}

#[derive(Deserialize)]
struct PodStatus {
    phase: String,
}

#[derive(Default, Deserialize)]
struct PodSpec {
    #[serde(rename = "nodeName")]
    node_name: Option<String>,
    containers: Vec<Container>,
    #[serde(rename = "initContainers", default)]
    init_containers: Vec<Container>,
    #[serde(default)]
    resources: ResourceRequirements,
    #[serde(default)]
    overhead: BTreeMap<String, String>,
}

#[derive(Deserialize)]
struct Container {
    name: String,
    #[serde(default)]
    resources: ResourceRequirements,
    #[serde(rename = "restartPolicy")]
    restart_policy: Option<String>,
}

#[derive(Default, Deserialize)]
struct ResourceRequirements {
    #[serde(default)]
    requests: BTreeMap<String, String>,
}

async fn preflight_capacity(observability_namespace: &str) -> Result<()> {
    crate::ops::require_on_path("kubectl")?;
    let node_command = "kubectl get nodes -o json";
    let nodes: KubeList<Node> = read_kubernetes_json(
        &["get", "nodes", "-o", "json"],
        node_command,
        "node allocatable memory",
    )
    .await?;

    let mut ready_nodes = BTreeMap::new();
    for node in nodes.items {
        let ready = node
            .status
            .conditions
            .iter()
            .any(|condition| condition.kind == "Ready" && condition.status == "True");
        if !ready || node.spec.unschedulable {
            continue;
        }
        let memory = node.status.allocatable.get("memory").ok_or_else(|| {
            anyhow!(
                "Ready node {} has no status.allocatable.memory; inspect with `{node_command}`",
                node.metadata.name
            )
        })?;
        ready_nodes.insert(
            node.metadata.name,
            parse_memory_quantity(memory)
                .with_context(|| format!("parsing allocatable memory from `{node_command}`"))?,
        );
    }
    if ready_nodes.is_empty() {
        bail!(
            "no Ready schedulable nodes expose allocatable memory; inspect the cluster prerequisite with `{node_command}`"
        );
    }

    let pod_command = "kubectl get pods --all-namespaces -o json";
    let pods: KubeList<Pod> = read_kubernetes_json(
        &["get", "pods", "--all-namespaces", "-o", "json"],
        pod_command,
        "scheduled pod memory requests",
    )
    .await?;
    let ready_names = ready_nodes.keys().cloned().collect::<BTreeSet<_>>();
    let mut scheduled_requests = 0u128;
    for pod in pods.items {
        if matches!(pod.status.phase.as_str(), "Succeeded" | "Failed") {
            continue;
        }
        if is_managed_observability_pod(&pod, observability_namespace) {
            continue;
        }
        let Some(node_name) = pod.spec.node_name.as_deref() else {
            continue;
        };
        if !ready_names.contains(node_name) {
            continue;
        }
        scheduled_requests = scheduled_requests
            .checked_add(effective_pod_memory_request(&pod)?)
            .ok_or_else(|| anyhow!("scheduled pod memory request total overflowed"))?;
    }

    let allocatable = ready_nodes.values().try_fold(0u128, |total, memory| {
        total
            .checked_add(*memory)
            .ok_or_else(|| anyhow!("Ready node allocatable memory total overflowed"))
    })?;
    let available = allocatable.saturating_sub(scheduled_requests);
    let required_memory_mib = FIXED_MEMORY_MIB
        .checked_add(
            PER_READY_NODE_MEMORY_MIB
                .checked_mul(ready_nodes.len() as u128)
                .ok_or_else(|| anyhow!("Ready node memory requirement overflowed"))?,
        )
        .ok_or_else(|| anyhow!("observability memory requirement overflowed"))?;
    let required_memory_bytes = required_memory_mib
        .checked_mul(MIB)
        .ok_or_else(|| anyhow!("observability memory byte requirement overflowed"))?;
    if available < required_memory_bytes {
        bail!(
            "curie example sre-bot install --observability has insufficient schedulable memory: required {required_memory_mib}Mi, available {}Mi; reduce scheduled pod requests or add Ready node memory, then rerun this command",
            available / MIB
        );
    }
    Ok(())
}

fn is_managed_observability_pod(pod: &Pod, observability_namespace: &str) -> bool {
    if pod.metadata.namespace != observability_namespace {
        return false;
    }
    let labels = &pod.metadata.labels;
    labels
        .get("app.kubernetes.io/instance")
        .is_some_and(|instance| MANAGED_HELM_RELEASES.contains(&instance.as_str()))
        || labels
            .get("app.kubernetes.io/name")
            .is_some_and(|name| name == "tempo")
}

async fn read_kubernetes_json<T: for<'de> Deserialize<'de>>(
    args: &[&str],
    display: &str,
    purpose: &str,
) -> Result<T> {
    let output = tokio::process::Command::new("kubectl")
        .args(args)
        .output()
        .await
        .with_context(|| format!("failed to invoke `{display}`"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        bail!(
            "could not read {purpose} with `{display}`: {}",
            if stderr.is_empty() {
                "kubectl exited nonzero"
            } else {
                &stderr
            }
        );
    }
    serde_json::from_slice(&output.stdout)
        .with_context(|| format!("malformed JSON from `{display}` while reading {purpose}"))
}

fn effective_pod_memory_request(pod: &Pod) -> Result<u128> {
    let mut application = 0u128;
    for container in &pod.spec.containers {
        application = checked_add(
            application,
            resource_memory(&container.resources, &pod.metadata.name, &container.name)?,
        )?;
    }

    let mut restartable = 0u128;
    let mut max_init_stage = 0u128;
    for container in &pod.spec.init_containers {
        let request = resource_memory(&container.resources, &pod.metadata.name, &container.name)?;
        let stage = if container.restart_policy.as_deref() == Some("Always") {
            restartable = checked_add(restartable, request)?;
            restartable
        } else {
            checked_add(restartable, request)?
        };
        max_init_stage = max_init_stage.max(stage);
    }

    let steady_state = checked_add(application, restartable)?;
    let container_request = steady_state.max(max_init_stage);
    let pod_level = resource_memory(&pod.spec.resources, &pod.metadata.name, "pod")?;
    let overhead = optional_memory(&pod.spec.overhead, &pod.metadata.name, "pod overhead")?;
    checked_add(container_request.max(pod_level), overhead)
}

fn resource_memory(resources: &ResourceRequirements, pod: &str, container: &str) -> Result<u128> {
    optional_memory(&resources.requests, pod, container)
}

fn optional_memory(requests: &BTreeMap<String, String>, pod: &str, owner: &str) -> Result<u128> {
    requests.get("memory").map_or(Ok(0), |quantity| {
        parse_memory_quantity(quantity)
            .with_context(|| format!("invalid memory request for {pod}/{owner}"))
    })
}

fn checked_add(left: u128, right: u128) -> Result<u128> {
    left.checked_add(right)
        .ok_or_else(|| anyhow!("pod memory request overflowed"))
}

fn parse_memory_quantity(quantity: &str) -> Result<u128> {
    let quantity = quantity.trim();
    let (number, multiplier) = [
        ("Ei", 1024f64.powi(6)),
        ("Pi", 1024f64.powi(5)),
        ("Ti", 1024f64.powi(4)),
        ("Gi", 1024f64.powi(3)),
        ("Mi", 1024f64.powi(2)),
        ("Ki", 1024f64),
        ("E", 1000f64.powi(6)),
        ("P", 1000f64.powi(5)),
        ("T", 1000f64.powi(4)),
        ("G", 1000f64.powi(3)),
        ("M", 1000f64.powi(2)),
        ("K", 1000f64),
        ("k", 1000f64),
        ("m", 0.001f64),
        ("u", 0.000_001f64),
        ("n", 0.000_000_001f64),
    ]
    .into_iter()
    .find_map(|(suffix, multiplier)| {
        quantity
            .strip_suffix(suffix)
            .map(|number| (number, multiplier))
    })
    .unwrap_or((quantity, 1f64));
    let number = number
        .parse::<f64>()
        .with_context(|| format!("unsupported Kubernetes memory quantity {quantity:?}"))?;
    let bytes = number * multiplier;
    if !bytes.is_finite() || bytes < 0.0 || bytes > u128::MAX as f64 {
        bail!("unsupported Kubernetes memory quantity {quantity:?}");
    }
    Ok(bytes.ceil() as u128)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn memory_quantities_cover_the_kubernetes_shapes_used_by_nodes_and_pods() {
        assert_eq!(parse_memory_quantity("1Gi").unwrap(), 1024 * 1024 * 1024);
        // 1409024Ki is the exact one-node required total (FIXED_MEMORY_MIB
        // 1216 + PER_READY_NODE_MEMORY_MIB 160 = 1376Mi) in the Ki form a node
        // reports allocatable memory in; it moved from 1312Mi under #2059.
        assert_eq!(parse_memory_quantity("1409024Ki").unwrap(), 1376 * MIB);
        assert_eq!(parse_memory_quantity("500M").unwrap(), 500_000_000);
        assert_eq!(parse_memory_quantity("1e6").unwrap(), 1_000_000);
    }

    #[test]
    fn the_upgrade_path_off_strips_the_connector_and_its_gates() {
        // The behaviour before this flag existed, pinned so the flag cannot
        // change what an install that did not ask for it receives.
        let connectors = runtime_connector_declaration(
            bundle_file("connectors.yaml"),
            "sha256:tempo",
            None,
            None,
            OBSERVABILITY_NAMESPACE,
            None,
        )
        .unwrap();
        let parsed: serde_json::Value = serde_norway::from_slice(&connectors).unwrap();
        assert!(parsed["connectors"].get("self-upgrade").is_none());

        let manifest =
            runtime_plugin_manifest(bundle_file(".claude-plugin/plugin.json"), false, false)
                .unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&manifest).unwrap();
        assert!(parsed.get("approvalPolicy").is_none());
    }

    #[test]
    fn the_upgrade_path_on_fills_in_both_cronjob_names() {
        // The whole reason this flag exists. The bundle ships
        // PLATFORM_UPGRADE_CRONJOB empty, and the worker's connector reconciler
        // puts that declaration back within the minute over anything set by
        // hand -- so if the installer does not render a real value, nothing can.
        let connectors = runtime_connector_declaration(
            bundle_file("connectors.yaml"),
            "sha256:tempo",
            None,
            None,
            OBSERVABILITY_NAMESPACE,
            Some("sha256:upgrade"),
        )
        .unwrap();
        let parsed: serde_json::Value = serde_norway::from_slice(&connectors).unwrap();
        let env = &parsed["connectors"]["self-upgrade"]["env"];
        assert_eq!(
            env[PLATFORM_UPGRADE_CRONJOB_ENV],
            PLATFORM_UPGRADE_CRONJOB_NAME
        );
        assert_eq!(env[SELF_UPGRADE_CRONJOB_ENV], SELF_UPGRADE_CRONJOB_NAME);
        // `build:` records a LOCAL image id the cluster tier refuses, so a kept
        // connector without a resolved digest is one that can never start.
        assert!(parsed["connectors"]["self-upgrade"].get("build").is_none());
        assert_eq!(
            parsed["connectors"]["self-upgrade"]["image"],
            format!("{SELF_UPGRADE_IMAGE_REPOSITORY}@sha256:upgrade")
        );
    }

    #[test]
    fn a_kept_upgrade_connector_keeps_exactly_its_two_gates() {
        // A gate naming a stripped connector fails validation for everyone; a
        // kept connector with no gate is an ungated write. Both are decided from
        // the same condition, so both are asserted here.
        let manifest =
            runtime_plugin_manifest(bundle_file(".claude-plugin/plugin.json"), false, true)
                .unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&manifest).unwrap();
        let gates: Vec<&str> = parsed["approvalPolicy"]["gates"]
            .as_array()
            .unwrap()
            .iter()
            .map(|gate| gate["gate"].as_str().unwrap())
            .collect();
        assert_eq!(gates, vec![UPGRADE_GATE, PLATFORM_UPGRADE_GATE]);
    }

    #[test]
    fn a_release_recording_a_model_credential_is_refused() {
        // The shape `helm get values -o json` returns for an install that has
        // one. This is the case that cost a credential.
        let existing = serde_json::json!({
            "agentSandbox": {"runner": {"credentials": "sk-ant-EXAMPLE", "fakeModel": false}}
        });
        assert!(records_a_model_credential(&existing));
    }

    #[test]
    fn a_release_without_one_is_not_refused() {
        for existing in [
            serde_json::json!({}),
            serde_json::json!({"agentSandbox": {}}),
            serde_json::json!({"agentSandbox": {"runner": {}}}),
            // Empty is not "present": an operator who cleared it did so
            // deliberately, and refusing would block them from re-running.
            serde_json::json!({"agentSandbox": {"runner": {"credentials": ""}}}),
        ] {
            assert!(!records_a_model_credential(&existing), "{existing}");
        }
    }

    fn platform_role_source() -> &'static [u8] {
        bundle_file("manifests/platform-upgrade-role.yaml")
    }

    #[test]
    fn the_platform_role_lands_in_the_release_namespace_with_no_static_token() {
        let rendered = render_platform_upgrade_role(platform_role_source(), "curie-prod").unwrap();
        let text = String::from_utf8(rendered).unwrap();
        assert!(text.contains("namespace: curie-prod"));
        assert!(!text.contains("namespace: curie\n"));
        // The reader and writer identities ship a static token Secret; this one
        // must not. A namespace-admin-equivalent token that outlives its Job is
        // the thing this shape exists to avoid.
        assert!(
            !text.contains("service-account-token"),
            "the platform upgrade identity must not get a static token: {text}"
        );
    }

    #[test]
    fn a_widened_platform_role_stops_the_install() {
        // The manifest is edited far more often than this file. A rule appended
        // there must fail here rather than be granted silently -- this is the
        // widest credential the bundle creates.
        let source = String::from_utf8(platform_role_source().to_vec()).unwrap();
        let widened = source.replace(
            "  - apiGroups: [\"\"]\n    resources: [\"pods\", \"events\"]\n    verbs: [\"get\", \"list\", \"watch\"]",
            "  - apiGroups: [\"\"]\n    resources: [\"pods\", \"events\"]\n    verbs: [\"get\", \"list\", \"watch\"]\n  \
             - apiGroups: [\"*\"]\n    resources: [\"*\"]\n    verbs: [\"*\"]",
        );
        assert_ne!(
            widened, source,
            "the fixture no longer matches the manifest"
        );
        let error = render_platform_upgrade_role(widened.as_bytes(), "curie-prod").unwrap_err();
        assert!(
            error.to_string().contains("rules"),
            "a widened Role must be refused by name: {error}"
        );
    }

    #[test]
    fn the_platform_cronjob_is_pointed_at_this_install() {
        let rendered = render_platform_cronjob(
            PLATFORM_UPGRADE_CRONJOB_YAML,
            "curie-prod",
            "curie-prod-release",
            "acme/widget",
        )
        .unwrap();
        let text = String::from_utf8(rendered).unwrap();
        assert!(text.contains("namespace: curie-prod"));
        assert!(text.contains("curie-prod-release"));
        assert!(text.contains("acme/widget"));
        // The placeholders the shipped file carries must all be gone; one left
        // behind is a tool that refuses every call with nothing visibly wrong.
        assert!(
            !text.contains("curie-eng/curie"),
            "the repository placeholder survived: {text}"
        );
    }

    #[test]
    fn the_rendered_cronjob_is_suspended() {
        // The installer's contract: an upgrade happens when a human approves
        // one, never on a timer nobody chose.
        let rendered = render_platform_cronjob(
            PLATFORM_UPGRADE_CRONJOB_YAML,
            "curie-prod",
            "release",
            "acme/widget",
        )
        .unwrap();
        let text = String::from_utf8(rendered).unwrap();
        assert!(text.contains("suspend: true"), "{text}");
    }

    #[test]
    fn a_renamed_cronjob_stops_the_install() {
        // The connector is told this name through its own env. Two places free
        // to disagree is how a verb ends up refusing every call.
        let source = String::from_utf8(PLATFORM_UPGRADE_CRONJOB_YAML.to_vec()).unwrap();
        let renamed = source.replace("name: platform-upgrade", "name: something-else");
        assert_ne!(
            renamed, source,
            "the fixture no longer matches the manifest"
        );
        let error =
            render_platform_cronjob(renamed.as_bytes(), "curie-prod", "release", "acme/widget")
                .unwrap_err();
        assert!(error.to_string().contains("platform-upgrade"), "{error}");
    }

    fn bundle_file(name: &str) -> &'static [u8] {
        BUNDLE_FILES
            .iter()
            .find(|(candidate, _)| *candidate == name)
            .map(|(_, contents)| *contents)
            .unwrap_or_else(|| panic!("embedded bundle has no {name}"))
    }

    fn targets(raw: &str) -> Vec<WriteTarget> {
        parse_write_allowlist(raw).expect("fixture allowlist parses")
    }

    #[test]
    fn write_allowlist_normalizes_order_and_duplicates() {
        let parsed = targets("curie/b , curie/a,curie/b");
        assert_eq!(
            parsed
                .iter()
                .map(WriteTarget::qualified)
                .collect::<Vec<_>>(),
            vec!["curie/a", "curie/b"]
        );
    }

    #[test]
    fn write_allowlist_refuses_the_shipped_placeholder() {
        let error = parse_write_allowlist("<namespace>/<deployment>")
            .expect_err("the placeholder must not render a Role");
        assert!(error.to_string().contains("placeholder"), "{error:#}");
    }

    #[test]
    fn write_allowlist_refuses_entries_that_are_not_namespace_qualified() {
        for raw in ["curie-api", "curie/", "/curie-api", "curie/Curie_API", ""] {
            assert!(
                parse_write_allowlist(raw).is_err(),
                "{raw:?} must be refused"
            );
        }
    }

    /// The property this whole flag exists for: ONE input, both ceilings, and
    /// they agree by construction rather than by a later comparison.
    #[test]
    fn one_allowlist_renders_both_ceilings_identically() {
        let parsed = targets("curie/curie-api,other/web");
        let role = render_write_role(
            bundle_file("manifests/write-role.yaml"),
            &parsed,
            CURIE_NAMESPACE,
        )
        .expect("write role renders");
        let role = String::from_utf8(role).expect("rendered role is UTF-8");
        let mut granted: Vec<String> = Vec::new();
        for document in serde_norway::Deserializer::from_str(&role) {
            let value: serde_json::Value =
                serde::Deserialize::deserialize(document).expect("rendered document parses");
            if value.get("kind").and_then(serde_json::Value::as_str) != Some("Role") {
                continue;
            }
            let namespace = value["metadata"]["namespace"].as_str().unwrap().to_string();
            for name in value["rules"][0]["resourceNames"].as_array().unwrap() {
                granted.push(format!("{namespace}/{}", name.as_str().unwrap()));
            }
        }
        granted.sort();

        let connectors = runtime_connector_declaration(
            bundle_file("connectors.yaml"),
            "sha256:fixture",
            Some("sha256:writefixture"),
            Some(&parsed),
            OBSERVABILITY_NAMESPACE,
            None,
        )
        .expect("connector declaration renders");
        let declaration: serde_json::Value =
            serde_norway::from_slice(&connectors).expect("rendered connectors parse");
        let allowlist = declaration["connectors"]["k8s-write"]["env"][WRITE_ALLOWLIST_ENV]
            .as_str()
            .expect("the write connector carries an allowlist");
        let mut declared: Vec<String> = allowlist.split(',').map(str::to_string).collect();
        declared.sort();

        assert_eq!(
            granted, declared,
            "the two ceilings must name the same targets"
        );
        assert_eq!(granted, vec!["curie/curie-api", "other/web"]);
    }

    /// The write connector is installed by default now, so the empty ceiling has
    /// to be a state that is safe rather than merely representable.
    ///
    /// The RBAC trap this guards: a rule whose `resourceNames` is empty or absent
    /// grants the verb on EVERY resource of that type. So "no targets" must omit
    /// the Role, not render one with no names -- otherwise the default install
    /// would hand the bot patch on every Deployment in the namespace, which is the
    /// exact opposite of what an empty allowlist reads like.
    #[test]
    fn no_targets_renders_an_identity_with_no_role_at_all() {
        let rendered = render_write_role(
            bundle_file("manifests/write-role.yaml"),
            &[],
            CURIE_NAMESPACE,
        )
        .expect("write role renders with no targets");
        let text = String::from_utf8(rendered).expect("rendered role is UTF-8");
        let mut kinds: Vec<String> = Vec::new();
        for document in serde_norway::Deserializer::from_str(&text) {
            let value: serde_json::Value =
                serde::Deserialize::deserialize(document).expect("rendered document parses");
            if let Some(kind) = value.get("kind").and_then(serde_json::Value::as_str) {
                kinds.push(kind.to_string());
            }
        }
        kinds.sort();
        assert_eq!(
            kinds,
            vec!["Secret".to_string(), "ServiceAccount".to_string()],
            "an empty ceiling must mint the identity and grant it nothing; rendered: {text}"
        );
    }

    /// The other half of the empty ceiling: the connector is present, so the tool
    /// is published and gated, and its own allowlist is empty rather than the
    /// shipped placeholder.
    #[test]
    fn no_targets_keeps_the_connector_with_an_empty_allowlist() {
        let rendered = runtime_connector_declaration(
            bundle_file("connectors.yaml"),
            "sha256:fixture",
            Some("sha256:writefixture"),
            Some(&[]),
            OBSERVABILITY_NAMESPACE,
            None,
        )
        .expect("connector declaration renders with no targets");
        let declaration: serde_json::Value =
            serde_norway::from_slice(&rendered).expect("rendered declaration parses");
        let write = &declaration["connectors"]["k8s-write"];
        assert!(
            !write.is_null(),
            "the write connector must be installed by default"
        );
        assert_eq!(
            write["env"][WRITE_ALLOWLIST_ENV].as_str(),
            Some(""),
            "an empty ceiling is an empty allowlist, never the shipped placeholder"
        );
    }

    /// And the opt-out still produces exactly what absence used to.
    #[test]
    fn opting_out_leaves_the_write_connector_absent() {
        let rendered = runtime_connector_declaration(
            bundle_file("connectors.yaml"),
            "sha256:fixture",
            None,
            None,
            OBSERVABILITY_NAMESPACE,
            None,
        )
        .expect("connector declaration renders with the write path out");
        let declaration: serde_json::Value =
            serde_norway::from_slice(&rendered).expect("rendered declaration parses");
        assert!(
            declaration["connectors"]["k8s-write"].is_null(),
            "--no-write must leave the connector out entirely"
        );
    }

    #[test]
    fn rendered_write_role_refuses_a_widened_grant() {
        let widened = b"---\napiVersion: rbac.authorization.k8s.io/v1\nkind: Role\nmetadata:\n  name: sre-bot-writer\n  namespace: curie\nrules:\n  - apiGroups: [apps]\n    resources: [deployments]\n    resourceNames: [my-app]\n    verbs: [get, patch, delete]\n";
        let error = render_write_role(widened, &targets("curie/api"), CURIE_NAMESPACE)
            .expect_err("a widened verb set must stop the install");
        assert!(error.to_string().contains("verbs"), "{error:#}");
    }

    #[test]
    fn write_role_identity_uses_the_selected_curie_namespace() {
        let rendered = render_write_role(
            bundle_file("manifests/write-role.yaml"),
            &targets("apps/api"),
            "soak",
        )
        .expect("write role renders");
        let text = String::from_utf8(rendered).expect("rendered role is UTF-8");
        let mut sa_ns = Vec::new();
        let mut role_ns = Vec::new();
        let mut subject_ns = Vec::new();
        for document in serde_norway::Deserializer::from_str(&text) {
            let value: serde_json::Value =
                serde::Deserialize::deserialize(document).expect("rendered document parses");
            match value.get("kind").and_then(serde_json::Value::as_str) {
                Some("ServiceAccount") | Some("Secret") => {
                    sa_ns.push(value["metadata"]["namespace"].as_str().unwrap().to_string());
                }
                Some("Role") => {
                    role_ns.push(value["metadata"]["namespace"].as_str().unwrap().to_string());
                }
                Some("RoleBinding") => {
                    subject_ns.push(
                        value["subjects"][0]["namespace"]
                            .as_str()
                            .unwrap()
                            .to_string(),
                    );
                    role_ns.push(value["metadata"]["namespace"].as_str().unwrap().to_string());
                }
                _ => {}
            }
        }
        assert_eq!(sa_ns, vec!["soak", "soak"]);
        assert_eq!(subject_ns, vec!["soak"]);
        assert_eq!(role_ns, vec!["apps", "apps"]);
        assert!(
            !text.contains("namespace: curie"),
            "selected Curie identity must replace the shipped default: {text}"
        );
    }

    #[test]
    fn write_opt_in_keeps_only_the_restart_gate() {
        let manifest =
            runtime_plugin_manifest(bundle_file(".claude-plugin/plugin.json"), true, false)
                .expect("write manifest renders");
        let manifest: serde_json::Value =
            serde_json::from_slice(&manifest).expect("rendered manifest parses");
        let gates = manifest["approvalPolicy"]["gates"].as_array().unwrap();
        assert_eq!(gates.len(), 1, "scale must not ride along: {gates:?}");
        assert_eq!(gates[0]["gate"].as_str(), Some(WRITE_GATE));
    }

    /// Every write connector kept must still be gated, and every gate must still
    /// name a connector kept -- the two halves that were forgotten separately.
    #[test]
    fn write_opt_in_keeps_the_connector_its_gate_names() {
        let parsed = targets("curie/api");
        let connectors = runtime_connector_declaration(
            bundle_file("connectors.yaml"),
            "sha256:fixture",
            Some("sha256:writefixture"),
            Some(&parsed),
            OBSERVABILITY_NAMESPACE,
            None,
        )
        .expect("connector declaration renders");
        let declaration: serde_json::Value = serde_norway::from_slice(&connectors).unwrap();
        let connectors = declaration["connectors"].as_object().unwrap();
        assert!(connectors.contains_key("k8s-write"));
        assert!(
            !connectors.contains_key("k8s-scale"),
            "scale is a separate opt-in"
        );

        let manifest =
            runtime_plugin_manifest(bundle_file(".claude-plugin/plugin.json"), true, false)
                .unwrap();
        let manifest: serde_json::Value = serde_json::from_slice(&manifest).unwrap();
        for gate in manifest["approvalPolicy"]["gates"].as_array().unwrap() {
            let gate = gate["gate"].as_str().unwrap();
            let server = gate.trim_start_matches("mcp__").split("__").next().unwrap();
            assert!(
                connectors.contains_key(server),
                "gate {gate} names connector {server}, which this install removed"
            );
        }
    }

    #[test]
    fn read_only_install_is_unchanged_by_the_new_flag() {
        let connectors = runtime_connector_declaration(
            bundle_file("connectors.yaml"),
            "sha256:fixture",
            None,
            None,
            OBSERVABILITY_NAMESPACE,
            None,
        )
        .expect("read only declaration renders");
        let declaration: serde_json::Value = serde_norway::from_slice(&connectors).unwrap();
        let connectors = declaration["connectors"].as_object().unwrap();
        assert!(!connectors.contains_key("k8s-write"));
        assert!(!connectors.contains_key("k8s-scale"));

        let manifest =
            runtime_plugin_manifest(bundle_file(".claude-plugin/plugin.json"), false, false)
                .unwrap();
        let manifest: serde_json::Value = serde_json::from_slice(&manifest).unwrap();
        assert!(manifest.get("approvalPolicy").is_none());
    }

    #[test]
    fn write_opt_in_still_refuses_an_unknown_connector() {
        let source = b"connectors:\n  kubernetes: {}\n  grafana: {}\n  tempo:\n    build:\n      context: connectors/tempo\n  k8s-write: {}\n  k8s-scale: {}\n  self-upgrade: {}\n  mystery: {}\n";
        let error = runtime_connector_declaration(
            source,
            "sha256:fixture",
            Some("sha256:writefixture"),
            Some(&targets("curie/api")),
            OBSERVABILITY_NAMESPACE,
            None,
        )
        .expect_err("an unclassified connector must stop the install");
        assert!(error.to_string().contains("mystery"), "{error:#}");
    }

    #[test]
    fn runtime_connector_transform_requires_the_declared_write_connector() {
        let source = b"connectors:\n  tempo:\n    build:\n      context: connectors/tempo\n      platforms: [linux/amd64]\n";
        let error = runtime_connector_declaration(
            source,
            "sha256:fixture",
            None,
            None,
            OBSERVABILITY_NAMESPACE,
            None,
        )
        .expect_err("missing k8s-write must be refused");
        assert!(
            error
                .to_string()
                .contains("must declare connectors.k8s-write"),
            "unexpected error: {error:#}"
        );
    }

    #[test]
    fn runtime_plugin_transform_requires_the_exact_write_gate_policy() {
        let exact_gate = serde_json::json!({
            "gate": "mcp__k8s-write__restart_deployment",
            "route": "sre-approvals"
        });
        let scale_gate = serde_json::json!({
            "gate": "mcp__k8s-scale__scale_deployment",
            "route": "sre-approvals"
        });
        let cases = [
            (
                "missing approval policy",
                serde_json::json!({"name": "sre-bot", "description": "source"}),
            ),
            (
                "renamed gate",
                serde_json::json!({
                    "name": "sre-bot",
                    "description": "source",
                    "approvalPolicy": {"gates": [
                        {
                            "gate": "mcp__k8s-write__rollout_restart",
                            "route": "sre-approvals"
                        },
                        scale_gate.clone()
                    ]}
                }),
            ),
            (
                "additional gate",
                serde_json::json!({
                    "name": "sre-bot",
                    "description": "source",
                    "approvalPolicy": {"gates": [
                        exact_gate.clone(),
                        scale_gate.clone(),
                        {"gate": "mcp__other__write", "route": "sre-approvals"}
                    ]}
                }),
            ),
            (
                "only the restart gate, scale gate dropped",
                serde_json::json!({
                    "name": "sre-bot",
                    "description": "source",
                    "approvalPolicy": {"gates": [exact_gate.clone()]}
                }),
            ),
            (
                "different route",
                serde_json::json!({
                    "name": "sre-bot",
                    "description": "source",
                    "approvalPolicy": {"gates": [
                        {
                            "gate": "mcp__k8s-write__restart_deployment",
                            "route": "other-approvals"
                        },
                        scale_gate.clone()
                    ]}
                }),
            ),
        ];

        for (case, manifest) in cases {
            let source = serde_json::to_vec(&manifest).expect("serialize fixture manifest");
            let error = match runtime_plugin_manifest(&source, false, false) {
                Ok(_) => panic!("{case} must be refused"),
                Err(error) => error,
            };
            assert!(
                error
                    .to_string()
                    .contains("must declare the exact gated write verbs"),
                "unexpected error for {case}: {error:#}"
            );
        }
    }
}
