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
const READER_TOKEN_SECRET: &str = "sre-bot-reader-token";
const READER_TOKEN_TIMEOUT: &str = "2m";
const KUBECONFIG_SECRET_KEY: &str = "K8S_READONLY_KUBECONFIG";
const TEMPO_IMAGE_REPOSITORY: &str = "ghcr.io/curie-eng/curie-sre-bot-tempo";
const TEMPO_IMAGE_TAG: &str = "0.8.0";
const TEMPO_TAGGED_IMAGE: &str = "ghcr.io/curie-eng/curie-sre-bot-tempo:0.8.0";
const RUNTIME_PLUGIN_DESCRIPTION: &str = "SRE triage assistant for plain English production health and Kubernetes questions in Slack. This installer deploys read only Kubernetes, Grafana, and Tempo connectors. It omits the source bundle's gated write connector and approval policy; enable that path only through the documented explicit build and deploy flow.";
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
    let workspace = EmbeddedWorkspace::create(&tempo_digest)?;
    ensure_grafana_admin_secret().await?;
    for command in &stack_commands {
        run_install_command(command, &workspace, &chart).await?;
    }
    apply_curie_platform(&chart, false).await?;
    run_install_command(&integration_command, &workspace, &chart).await?;
    run_install_command(&read_access_command, &workspace, &chart).await?;
    let kubeconfig = read_only_connector_kubeconfig().await?;

    let bundle_dir = workspace.bundle_dir();
    let connection = resolve_embedded_cluster_connection().await?;
    let deployed =
        deploy_embedded_sre_bot(&bundle_dir, &connection, opts.slack_channel.as_deref()).await?;
    let secret_overrides = BTreeMap::from([(KUBECONFIG_SECRET_KEY.to_string(), kubeconfig)]);
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
    let wait_args = [
        "wait",
        "--namespace",
        CURIE_NAMESPACE,
        "--for=jsonpath={.data.token}",
        &format!("secret/{READER_TOKEN_SECRET}"),
        &format!("--timeout={READER_TOKEN_TIMEOUT}"),
    ];
    crate::ui::ui().plumbing(&format!("+ kubectl {}", wait_args.join(" ")));
    let wait = tokio::process::Command::new("kubectl")
        .args(wait_args)
        .output()
        .await
        .context("waiting for the SRE bot reader ServiceAccount token")?;
    if !wait.status.success() {
        let stderr = String::from_utf8_lossy(&wait.stderr);
        bail!(
            "the read only connector token {READER_TOKEN_SECRET} was not populated within {READER_TOKEN_TIMEOUT}: {}. Inspect it with `kubectl get secret {READER_TOKEN_SECRET} -n {CURIE_NAMESPACE}` and retry",
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
        READER_TOKEN_SECRET,
        "--namespace",
        CURIE_NAMESPACE,
        "-o",
        "json",
    ];
    let output = tokio::process::Command::new("kubectl")
        .args(get_args)
        .output()
        .await
        .context("reading the SRE bot reader ServiceAccount token")?;
    if !output.status.success() {
        bail!(
            "could not read Secret {READER_TOKEN_SECRET} in namespace {CURIE_NAMESPACE}; inspect it with `kubectl get secret {READER_TOKEN_SECRET} -n {CURIE_NAMESPACE}` and retry"
        );
    }
    let secret: serde_json::Value = serde_json::from_slice(&output.stdout)
        .context("the SRE bot reader token Secret returned malformed JSON")?;
    let data = secret
        .get("data")
        .and_then(serde_json::Value::as_object)
        .context("the SRE bot reader token Secret has no data")?;
    let ca = data
        .get("ca.crt")
        .and_then(serde_json::Value::as_str)
        .context("the SRE bot reader token Secret has no ca.crt")?;
    base64::engine::general_purpose::STANDARD
        .decode(ca)
        .context("the SRE bot reader token Secret contains an invalid ca.crt")?;
    let token = data
        .get("token")
        .and_then(serde_json::Value::as_str)
        .context("the SRE bot reader token Secret has no token")?;
    let token = base64::engine::general_purpose::STANDARD
        .decode(token)
        .context("the SRE bot reader token Secret contains an invalid token")?;
    let token = String::from_utf8(token)
        .context("the SRE bot reader token Secret contains a non UTF-8 token")?;
    if token.is_empty() {
        bail!("the SRE bot reader token Secret contains an empty token");
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
            "name": "sre-bot-reader",
            "user": {"token": token},
        }],
        "contexts": [{
            "name": "sre-bot-reader",
            "context": {"cluster": "in-cluster", "user": "sre-bot-reader"},
        }],
        "current-context": "sre-bot-reader",
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
    fn create(tempo_digest: &str) -> Result<Self> {
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
                let runtime = runtime_connector_declaration(contents, tempo_digest)?;
                workspace.write(&Path::new("bundle").join(name), &runtime)?;
            } else if *name == ".claude-plugin/plugin.json" {
                let runtime = runtime_plugin_manifest(contents)?;
                workspace.write(&Path::new("bundle").join(name), &runtime)?;
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

fn runtime_connector_declaration(source: &[u8], tempo_digest: &str) -> Result<Vec<u8>> {
    let source =
        std::str::from_utf8(source).context("embedded SRE bot connectors.yaml is not UTF-8")?;
    let mut declaration: serde_json::Value =
        serde_norway::from_str(source).context("parsing embedded SRE bot connectors.yaml")?;
    let connectors = declaration
        .get_mut("connectors")
        .and_then(serde_json::Value::as_object_mut)
        .context("embedded SRE bot must declare connectors")?;
    if connectors.remove("k8s-write").is_none() {
        bail!("embedded SRE bot must declare connectors.k8s-write");
    }
    if connectors.remove("k8s-scale").is_none() {
        bail!("embedded SRE bot must declare connectors.k8s-scale");
    }
    // Fail closed on a connector this build does not know about. Removing the
    // two write connectors by name is only read-only if the bundle has no third
    // one -- and the bundle is edited far more often than this file, so an
    // unrecognized connector must stop the install rather than ship in it.
    if let Some(unexpected) = connectors
        .keys()
        .find(|name| !matches!(name.as_str(), "kubernetes" | "grafana" | "tempo"))
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

fn runtime_plugin_manifest(source: &[u8]) -> Result<Vec<u8>> {
    let mut manifest: serde_json::Value =
        serde_json::from_slice(source).context("parsing embedded SRE bot plugin.json")?;
    // Pinned, not merely present. This install strips approvalPolicy entirely
    // because it ships read only, and stripping a gate is only safe when the
    // connector it guards is also gone -- so an unrecognized gate means the
    // bundle grew a write verb this build does not know to remove.
    let expected_policy = serde_json::json!({
        "gates": [
            {
                "gate": "mcp__k8s-write__restart_deployment",
                "route": "sre-approvals"
            },
            {
                "gate": "mcp__k8s-scale__scale_deployment",
                "route": "sre-approvals"
            }
        ]
    });
    if manifest.get("approvalPolicy") != Some(&expected_policy) {
        bail!("embedded SRE bot must declare the exact gated write verbs");
    }
    let manifest = manifest
        .as_object_mut()
        .context("embedded SRE bot plugin.json must be an object")?;
    manifest.remove("approvalPolicy");
    manifest.insert(
        "description".to_string(),
        serde_json::Value::String(RUNTIME_PLUGIN_DESCRIPTION.to_string()),
    );
    serde_json::to_vec_pretty(&manifest)
        .context("serializing the read only SRE bot plugin manifest")
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

    #[test]
    fn runtime_connector_transform_requires_the_declared_write_connector() {
        let source = b"connectors:\n  tempo:\n    build:\n      context: connectors/tempo\n      platforms: [linux/amd64]\n";
        let error = runtime_connector_declaration(source, "sha256:fixture")
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
            let error = match runtime_plugin_manifest(&source) {
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
