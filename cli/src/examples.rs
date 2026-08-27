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
const CURIE_RELEASE: &str = "curie";
// The issue text says 1248Mi, but its seven exact appendix requests total
// 1312Mi on one node once the enabled 64Mi kube-state-metrics request is
// included. Alloy and node exporter add 160Mi on every Ready schedulable node.
const FIXED_MEMORY_MIB: u128 = 1152;
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
// The one grant the write path may carry. Read from the shipped manifest and
// asserted rather than assumed, so editing that file to widen the verb set stops
// the install instead of shipping in it -- the same posture the connector and
// gate removals below already take.
const WRITE_RULE_API_GROUPS: [&str; 1] = ["apps"];
const WRITE_RULE_RESOURCES: [&str; 1] = ["deployments"];
const WRITE_RULE_VERBS: [&str; 2] = ["get", "patch"];
const READER_TOKEN_TIMEOUT: &str = "2m";
const KUBECONFIG_SECRET_KEY: &str = "K8S_READONLY_KUBECONFIG";
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
        "skills/sre-bot/SKILL.md",
        include_bytes!("../../examples/sre-bot/skills/sre-bot/SKILL.md"),
    ),
];

pub struct SreBotInstallOpts {
    pub observability: bool,
    pub dry_run: bool,
    pub slack_channel: Option<String>,
    /// `ns/name[,ns/name]`. Absent keeps the install read only.
    pub write_allowlist: Option<String>,
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
    release: &'static str,
    namespace: &'static str,
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
            plain(OBSERVABILITY_NAMESPACE),
            plain("--create-namespace"),
            plain("-f"),
            CommandArg::ObservabilityFile(values),
            plain("--wait"),
            plain("--timeout"),
            plain(HELM_TIMEOUT),
        ],
        helm_target: Some(HelmTarget {
            release,
            namespace: OBSERVABILITY_NAMESPACE,
        }),
    }
}

fn stack_install_commands() -> Vec<InstallCommand> {
    let mut commands = helm_repo_commands();
    commands.extend([
        upstream_upgrade(
            "grafana",
            "grafana-community/grafana",
            "12.11.1",
            "grafana-values.yaml",
        ),
        upstream_upgrade(
            "loki",
            "grafana-community/loki",
            "18.10.1",
            "loki-values.yaml",
        ),
        upstream_upgrade("alloy", "grafana/alloy", "1.11.1", "alloy-values.yaml"),
        upstream_upgrade(
            "prometheus",
            "prometheus-community/prometheus",
            "29.27.0",
            "prometheus-values.yaml",
        ),
        InstallCommand {
            program: "kubectl",
            args: vec![
                plain("apply"),
                plain("--namespace"),
                plain(OBSERVABILITY_NAMESPACE),
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
                plain(OBSERVABILITY_NAMESPACE),
                plain(format!("--timeout={HELM_TIMEOUT}")),
            ],
            helm_target: None,
        },
    ]);
    commands
}

fn curie_integration_command() -> InstallCommand {
    InstallCommand {
        program: "helm",
        args: vec![
            plain("upgrade"),
            plain(CURIE_RELEASE),
            CommandArg::CurieChart,
            plain("--namespace"),
            plain(CURIE_NAMESPACE),
            plain("--reuse-values"),
            plain("-f"),
            CommandArg::ObservabilityFile("curie-values.yaml"),
            plain("--wait"),
            plain("--timeout"),
            plain(HELM_TIMEOUT),
        ],
        helm_target: Some(HelmTarget {
            release: CURIE_RELEASE,
            namespace: CURIE_NAMESPACE,
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

    let write_targets = match opts.write_allowlist.as_deref() {
        Some(raw) => Some(parse_write_allowlist(raw)?),
        None => None,
    };
    let write_targets = write_targets.as_deref();

    preflight_capacity().await?;

    let resolved_chart = crate::artifacts::resolve_chart(
        None,
        crate::artifacts::Channel::current(),
        crate::artifacts::version(),
        crate::artifacts::cache_root,
        Path::new("charts/curie").is_dir(),
    )?;
    let stack_commands = stack_install_commands();
    let integration_command = curie_integration_command();
    let read_access_command = read_access_command();
    let write_role_command = write_role_command();

    if opts.dry_run {
        let chart = resolved_chart.planned_target();
        let mut lines = vec![format!(
            "resolve {TEMPO_TAGGED_IMAGE} to its immutable OCI image index digest before cluster mutation"
        )];
        lines.push(format!(
            "preserve or create Secret {GRAFANA_ADMIN_SECRET} in namespace {OBSERVABILITY_NAMESPACE} without exposing its generated password"
        ));
        lines.extend(stack_commands.iter().map(|command| command.display(&chart)));
        lines.extend(apply_curie_platform(&chart, true).await?);
        lines.push(integration_command.display(&chart));
        lines.push(read_access_command.display(&chart));
        lines.push(format!(
            "kubectl wait --namespace {CURIE_NAMESPACE} --for=jsonpath={{.data.token}} secret/{READER_TOKEN_SECRET} --timeout={READER_TOKEN_TIMEOUT}"
        ));
        lines.push(
            "build the read only Kubernetes connector kubeconfig in memory from the ServiceAccount token"
                .to_string(),
        );
        if let Some(targets) = write_targets {
            lines.push(format!(
                "render manifests/write-role.yaml and connectors.k8s-write env {} from one allowlist: {}",
                WRITE_ALLOWLIST_ENV,
                write_allowlist_value(targets)
            ));
            lines.push(write_role_command.display(&chart));
            lines.push(format!(
                "kubectl wait --namespace {CURIE_NAMESPACE} --for=jsonpath={{.data.token}} secret/{WRITER_TOKEN_SECRET} --timeout={READER_TOKEN_TIMEOUT}"
            ));
            lines.push(
                "build the gated write Kubernetes connector kubeconfig in memory from the ServiceAccount token"
                    .to_string(),
            );
        }
        let mut deploy = "curie cluster deploy --plugin-dir embedded:examples/sre-bot --namespace curie --release curie".to_string();
        if let Some(channel) = &opts.slack_channel {
            deploy.push_str(&format!(" --slack-channel {channel}"));
        }
        lines.push(deploy);
        lines.push(
            "render and reconcile the deployed version connectors with the owned Kubernetes kubeconfig Secret override"
                .to_string(),
        );
        return Ok(SreBotInstallResult::DryRun(DryRunPlan { lines }));
    }

    let tempo_digest = resolve_tempo_index_digest().await?;
    let chart = crate::artifacts::ensure_cached(&resolved_chart).await?;
    crate::ops::require_on_path("helm")?;
    let workspace = EmbeddedWorkspace::create(&tempo_digest, write_targets)?;
    ensure_grafana_admin_secret().await?;
    for command in &stack_commands {
        run_install_command(command, &workspace, &chart).await?;
    }
    apply_curie_platform(&chart, false).await?;
    run_install_command(&integration_command, &workspace, &chart).await?;
    run_install_command(&read_access_command, &workspace, &chart).await?;
    let kubeconfig = read_only_connector_kubeconfig().await?;
    let write_kubeconfig = match write_targets {
        Some(_) => {
            run_install_command(&write_role_command, &workspace, &chart).await?;
            Some(write_connector_kubeconfig().await?)
        }
        None => None,
    };

    let bundle_dir = workspace.bundle_dir();
    let connection = resolve_embedded_cluster_connection().await?;
    let deployed =
        deploy_embedded_sre_bot(&bundle_dir, &connection, opts.slack_channel.as_deref()).await?;
    let mut secret_overrides = BTreeMap::from([(KUBECONFIG_SECRET_KEY.to_string(), kubeconfig)]);
    if let Some(write_kubeconfig) = write_kubeconfig {
        secret_overrides.insert(WRITE_KUBECONFIG_SECRET_KEY.to_string(), write_kubeconfig);
    }
    crate::connectors::sync_deployed_version(
        &connection.api_url,
        &connection.api_key,
        CURIE_NAMESPACE,
        CURIE_RELEASE,
        &deployed,
        &secret_overrides,
    )
    .await?;
    Ok(SreBotInstallResult::Installed(Box::new(deployed)))
}

async fn apply_curie_platform(chart: &Path, dry_run: bool) -> Result<Vec<String>> {
    let installation = crate::installation::Installation {
        version: crate::installation::SUPPORTED_VERSION,
        install: crate::installation::Install {
            namespace: CURIE_NAMESPACE.to_string(),
            release: CURIE_RELEASE.to_string(),
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
            ("scope", "repository:curie-eng/curie-sre-bot-tempo:pull"),
        ])
        .send()
        .await
        .with_context(|| format!("resolving {TEMPO_TAGGED_IMAGE} before cluster mutation"))?;
    if !token_response.status().is_success() {
        bail!(
            "could not resolve {TEMPO_TAGGED_IMAGE} before cluster mutation: anonymous GHCR token request returned HTTP {}",
            token_response.status()
        );
    }
    let token: RegistryToken = token_response
        .json()
        .await
        .with_context(|| format!("reading the anonymous token for {TEMPO_TAGGED_IMAGE}"))?;
    if token.token.is_empty() {
        bail!("could not resolve {TEMPO_TAGGED_IMAGE}: GHCR returned an empty token");
    }

    let manifest_response = client
        .get(format!(
            "{registry}/v2/curie-eng/curie-sre-bot-tempo/manifests/{TEMPO_IMAGE_TAG}"
        ))
        .bearer_auth(&token.token)
        .header(
            reqwest::header::ACCEPT,
            format!("{OCI_INDEX_MEDIA_TYPE}, {DOCKER_INDEX_MEDIA_TYPE}"),
        )
        .send()
        .await
        .with_context(|| format!("fetching the OCI image index for {TEMPO_TAGGED_IMAGE}"))?;
    if !manifest_response.status().is_success() {
        bail!(
            "could not resolve {TEMPO_TAGGED_IMAGE}: OCI index request returned HTTP {}",
            manifest_response.status()
        );
    }
    let body = manifest_response
        .bytes()
        .await
        .with_context(|| format!("reading the OCI image index for {TEMPO_TAGGED_IMAGE}"))?;
    let manifest: serde_json::Value = serde_json::from_slice(&body)
        .with_context(|| format!("{TEMPO_TAGGED_IMAGE} returned a malformed OCI index"))?;
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
            "could not resolve {TEMPO_TAGGED_IMAGE}: expected an OCI image index, got {}",
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

async fn ensure_grafana_admin_secret() -> Result<()> {
    ensure_observability_namespace().await?;
    let inspect = tokio::process::Command::new("kubectl")
        .args([
            "get",
            "secret",
            GRAFANA_ADMIN_SECRET,
            "--namespace",
            OBSERVABILITY_NAMESPACE,
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
            "could not inspect Secret {GRAFANA_ADMIN_SECRET} in namespace {OBSERVABILITY_NAMESPACE} with `kubectl get secret {GRAFANA_ADMIN_SECRET} -n {OBSERVABILITY_NAMESPACE}`: {}",
            stderr.trim()
        );
    }

    if grafana_release_exists().await? {
        return migrate_grafana_admin_secret().await;
    }

    let password = random_hex(32)?;
    let manifest = serde_json::to_vec(&serde_json::json!({
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": GRAFANA_ADMIN_SECRET,
            "namespace": OBSERVABILITY_NAMESPACE,
        },
        "type": "Opaque",
        "stringData": {
            "admin-user": "admin",
            "admin-password": password,
        },
    }))?;
    apply_private_manifest(&manifest, "Grafana admin Secret").await
}

async fn grafana_release_exists() -> Result<bool> {
    let output = tokio::process::Command::new("helm")
        .args([
            "status",
            GRAFANA_RELEASE,
            "--namespace",
            OBSERVABILITY_NAMESPACE,
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
        "could not determine whether Grafana is already installed; run `helm status {GRAFANA_RELEASE} -n {OBSERVABILITY_NAMESPACE}` and retry"
    )
}

#[derive(Clone)]
struct SecretKeyReference {
    name: String,
    key: String,
}

async fn migrate_grafana_admin_secret() -> Result<()> {
    let output = tokio::process::Command::new("kubectl")
        .args([
            "get",
            "deployment,statefulset",
            "--namespace",
            OBSERVABILITY_NAMESPACE,
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
                OBSERVABILITY_NAMESPACE,
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
            "namespace": OBSERVABILITY_NAMESPACE,
        },
        "type": "Opaque",
        "data": {
            "admin-user": user_data,
            "admin-password": password_data,
        },
    }))?;
    apply_private_manifest(&manifest, "Grafana admin Secret").await
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

async fn ensure_observability_namespace() -> Result<()> {
    let inspect = tokio::process::Command::new("kubectl")
        .args(["get", "namespace", OBSERVABILITY_NAMESPACE, "-o", "json"])
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
            "could not inspect namespace {OBSERVABILITY_NAMESPACE} with `kubectl get namespace {OBSERVABILITY_NAMESPACE}`: {}",
            stderr.trim()
        );
    }
    let output = tokio::process::Command::new("kubectl")
        .args(["create", "namespace", OBSERVABILITY_NAMESPACE])
        .output()
        .await
        .context("creating the observability namespace")?;
    if !output.status.success() {
        bail!(
            "could not create namespace {OBSERVABILITY_NAMESPACE}; run `kubectl create namespace {OBSERVABILITY_NAMESPACE}` and retry"
        );
    }
    Ok(())
}

async fn apply_private_manifest(manifest: &[u8], description: &str) -> Result<()> {
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
            "could not apply {description} {GRAFANA_ADMIN_SECRET} in namespace {OBSERVABILITY_NAMESPACE}; inspect access with `kubectl auth can-i create secret -n {OBSERVABILITY_NAMESPACE}`"
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

async fn read_only_connector_kubeconfig() -> Result<String> {
    connector_kubeconfig(READER_IDENTITY, READER_TOKEN_SECRET).await
}

async fn write_connector_kubeconfig() -> Result<String> {
    connector_kubeconfig(WRITER_IDENTITY, WRITER_TOKEN_SECRET).await
}

/// Build one connector's in-memory kubeconfig from a ServiceAccount token Secret.
///
/// Shared by the read and write identities so they cannot drift in how they
/// verify the token: both refuse an absent, malformed, or empty token rather
/// than emitting a kubeconfig the connector would only fail on later.
async fn connector_kubeconfig(identity: &str, token_secret: &str) -> Result<String> {
    let wait_args = [
        "wait",
        "--namespace",
        CURIE_NAMESPACE,
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
            "the connector token {token_secret} was not populated within {READER_TOKEN_TIMEOUT}: {}. Inspect it with `kubectl get secret {token_secret} -n {CURIE_NAMESPACE}` and retry",
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
        CURIE_NAMESPACE,
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
            "could not read Secret {token_secret} in namespace {CURIE_NAMESPACE}; inspect it with `kubectl get secret {token_secret} -n {CURIE_NAMESPACE}` and retry"
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

async fn resolve_embedded_cluster_connection() -> Result<EmbeddedClusterConnection> {
    let api_key = crate::ops::discover_api_key(CURIE_NAMESPACE, CURIE_RELEASE).await?;
    let explicit_api_url = std::env::var("CURIE_API_URL")
        .ok()
        .filter(|value| !value.trim().is_empty());
    let local_port = crate::message::DEFAULT_API_LOCAL_PORT;
    let (api_url, port_forward) = match commands::deploy_port_forward(
        explicit_api_url.as_deref(),
        CURIE_NAMESPACE,
        CURIE_RELEASE,
        local_port,
        crate::message::API_REMOTE_PORT,
    ) {
        Some(command) => {
            let port_forward = Some(
                crate::message::start_port_forward(&command, local_port, "SRE bot deploy API")
                    .await?,
            );
            (format!("http://localhost:{local_port}"), port_forward)
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
    fn create(tempo_digest: &str, write_targets: Option<&[WriteTarget]>) -> Result<Self> {
        let root = std::env::temp_dir().join(format!(
            "curie-sre-bot-install-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4()
        ));
        std::fs::create_dir(&root)
            .with_context(|| format!("creating embedded SRE bot workspace {}", root.display()))?;
        let workspace = Self { root };
        for (name, contents) in OBSERVABILITY_FILES {
            workspace.write(&Path::new("observability").join(name), contents)?;
        }
        for (name, contents) in BUNDLE_FILES {
            if *name == "connectors.yaml" {
                let runtime = runtime_connector_declaration(contents, tempo_digest, write_targets)?;
                workspace.write(&Path::new("bundle").join(name), &runtime)?;
            } else if *name == ".claude-plugin/plugin.json" {
                let runtime = runtime_plugin_manifest(contents, write_targets.is_some())?;
                workspace.write(&Path::new("bundle").join(name), &runtime)?;
            } else if *name == "manifests/write-role.yaml" {
                // Rendered from the allowlist when the write path is opted into,
                // and NOT written at all otherwise. Shipping the shipped
                // placeholder next to read-access.yaml -- which this install does
                // apply -- reads as though a Deployment patch grant were in
                // effect, when every consumer of it was stripped above.
                match write_targets {
                    Some(targets) => {
                        let rendered = render_write_role(contents, targets)?;
                        workspace.write(&Path::new("bundle").join(name), &rendered)?;
                    }
                    None => continue,
                }
            } else {
                workspace.write(&Path::new("bundle").join(name), contents)?;
            }
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
fn render_write_role(source: &[u8], targets: &[WriteTarget]) -> Result<Vec<u8>> {
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
            "metadata": {"name": WRITER_IDENTITY, "namespace": CURIE_NAMESPACE},
        }),
        serde_json::json!({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": WRITER_TOKEN_SECRET,
                "namespace": CURIE_NAMESPACE,
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
                "namespace": CURIE_NAMESPACE,
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

fn runtime_connector_declaration(
    source: &[u8],
    tempo_digest: &str,
    write_targets: Option<&[WriteTarget]>,
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
    // Fail closed on a connector this build does not know about. Removing the
    // write connectors by name is only as narrow as intended if the bundle has no
    // third one -- and the bundle is edited far more often than this file, so an
    // unrecognized connector must stop the install rather than ship in it.
    let known: &[&str] = if write_targets.is_some() {
        &["kubernetes", "grafana", "tempo", "k8s-write"]
    } else {
        &["kubernetes", "grafana", "tempo"]
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
    serde_norway::to_string(&declaration)
        .map(String::into_bytes)
        .context("serializing the immutable SRE bot connector declaration")
}

fn runtime_plugin_manifest(source: &[u8], write_enabled: bool) -> Result<Vec<u8>> {
    let mut manifest: serde_json::Value =
        serde_json::from_slice(source).context("parsing embedded SRE bot plugin.json")?;
    // Pinned, not merely present. This install strips approvalPolicy entirely
    // because it ships read only, and stripping a gate is only safe when the
    // connector it guards is also gone -- so an unrecognized gate means the
    // bundle grew a write verb this build does not know to remove.
    let expected_policy = serde_json::json!({
        "gates": [
            {"gate": WRITE_GATE, "route": "sre-approvals"},
            {"gate": SCALE_GATE, "route": "sre-approvals"}
        ]
    });
    if manifest.get("approvalPolicy") != Some(&expected_policy) {
        bail!("embedded SRE bot must declare the exact gated write verbs");
    }
    let manifest = manifest
        .as_object_mut()
        .context("embedded SRE bot plugin.json must be an object")?;
    if write_enabled {
        // Keep exactly the gate for the connector that stayed. A gate naming a
        // connector this install removed fails bundle validation for everyone,
        // and a connector kept without its gate is the ungated write this whole
        // path exists to avoid -- so the two are decided together, here, from one
        // condition.
        manifest.insert(
            "approvalPolicy".to_string(),
            serde_json::json!({
                "gates": [{"gate": WRITE_GATE, "route": "sre-approvals"}]
            }),
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

async fn preflight_capacity() -> Result<()> {
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
        if is_managed_observability_pod(&pod) {
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

fn is_managed_observability_pod(pod: &Pod) -> bool {
    if pod.metadata.namespace != OBSERVABILITY_NAMESPACE {
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
        assert_eq!(parse_memory_quantity("1343488Ki").unwrap(), 1312 * MIB);
        assert_eq!(parse_memory_quantity("500M").unwrap(), 500_000_000);
        assert_eq!(parse_memory_quantity("1e6").unwrap(), 1_000_000);
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
        let role = render_write_role(bundle_file("manifests/write-role.yaml"), &parsed)
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
            Some(&parsed),
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

    #[test]
    fn rendered_write_role_refuses_a_widened_grant() {
        let widened = b"---\napiVersion: rbac.authorization.k8s.io/v1\nkind: Role\nmetadata:\n  name: sre-bot-writer\n  namespace: curie\nrules:\n  - apiGroups: [apps]\n    resources: [deployments]\n    resourceNames: [my-app]\n    verbs: [get, patch, delete]\n";
        let error = render_write_role(widened, &targets("curie/api"))
            .expect_err("a widened verb set must stop the install");
        assert!(error.to_string().contains("verbs"), "{error:#}");
    }

    #[test]
    fn write_opt_in_keeps_only_the_restart_gate() {
        let manifest = runtime_plugin_manifest(bundle_file(".claude-plugin/plugin.json"), true)
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
            Some(&parsed),
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
            runtime_plugin_manifest(bundle_file(".claude-plugin/plugin.json"), true).unwrap();
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
        let connectors =
            runtime_connector_declaration(bundle_file("connectors.yaml"), "sha256:fixture", None)
                .expect("read only declaration renders");
        let declaration: serde_json::Value = serde_norway::from_slice(&connectors).unwrap();
        let connectors = declaration["connectors"].as_object().unwrap();
        assert!(!connectors.contains_key("k8s-write"));
        assert!(!connectors.contains_key("k8s-scale"));

        let manifest =
            runtime_plugin_manifest(bundle_file(".claude-plugin/plugin.json"), false).unwrap();
        let manifest: serde_json::Value = serde_json::from_slice(&manifest).unwrap();
        assert!(manifest.get("approvalPolicy").is_none());
    }

    #[test]
    fn write_opt_in_still_refuses_an_unknown_connector() {
        let source = b"connectors:\n  kubernetes: {}\n  grafana: {}\n  tempo:\n    build:\n      context: connectors/tempo\n  k8s-write: {}\n  k8s-scale: {}\n  mystery: {}\n";
        let error =
            runtime_connector_declaration(source, "sha256:fixture", Some(&targets("curie/api")))
                .expect_err("an unclassified connector must stop the install");
        assert!(error.to_string().contains("mystery"), "{error:#}");
    }

    #[test]
    fn runtime_connector_transform_requires_the_declared_write_connector() {
        let source = b"connectors:\n  tempo:\n    build:\n      context: connectors/tempo\n      platforms: [linux/amd64]\n";
        let error = runtime_connector_declaration(source, "sha256:fixture", None)
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
            let error = match runtime_plugin_manifest(&source, false) {
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
