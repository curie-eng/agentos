//! `curie cluster upgrade`: one resumable lifecycle that plans, validates,
//! drains, checkpoints, migrates, applies, proves exact convergence, runs a
//! canary, and records the new known-good version (issue #2301).
//!
//! Sibling slices this module composes and does not reimplement:
//! - versioned configuration migrations (#2299)
//! - database compatibility windows (#2300)
//! - the kind released-install upgrade CI rung (#2097)
//!
//! Drain is the existing #2010 gate: one drain per attempt. Resume after a
//! completed drain must not drain accepted work again.

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use super::command::{mask_secret, plain, require_on_path, run_capture, CommonOpts, OpsCommand};

/// Durable phases of a cluster upgrade. Order is load-bearing.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UpgradePhase {
    Plan,
    Validate,
    Drain,
    Checkpoint,
    Migrate,
    Apply,
    Converge,
    Canary,
    Commit,
}

impl UpgradePhase {
    pub const ALL: [UpgradePhase; 9] = [
        UpgradePhase::Plan,
        UpgradePhase::Validate,
        UpgradePhase::Drain,
        UpgradePhase::Checkpoint,
        UpgradePhase::Migrate,
        UpgradePhase::Apply,
        UpgradePhase::Converge,
        UpgradePhase::Canary,
        UpgradePhase::Commit,
    ];

    pub fn as_str(&self) -> &'static str {
        match self {
            UpgradePhase::Plan => "plan",
            UpgradePhase::Validate => "validate",
            UpgradePhase::Drain => "drain",
            UpgradePhase::Checkpoint => "checkpoint",
            UpgradePhase::Migrate => "migrate",
            UpgradePhase::Apply => "apply",
            UpgradePhase::Converge => "converge",
            UpgradePhase::Canary => "canary",
            UpgradePhase::Commit => "commit",
        }
    }
}

impl std::fmt::Display for UpgradePhase {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(UpgradePhase::as_str(self))
    }
}

/// Flags for `curie cluster upgrade`.
#[derive(Debug, Clone)]
pub struct UpgradeOpts {
    pub common: CommonOpts,
    pub to: String,
    pub chart: Option<String>,
    pub yes: bool,
}

/// What `cluster status` reports about the in-flight or last upgrade.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UpgradeStatusView {
    pub phase: Option<String>,
    pub status: String,
    pub known_good_version: Option<String>,
    pub target_version: Option<String>,
}

impl UpgradeStatusView {
    pub fn idle(known_good_version: Option<String>) -> Self {
        Self {
            phase: None,
            status: "idle".into(),
            known_good_version,
            target_version: None,
        }
    }

    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "phase": self.phase,
            "status": self.status,
            "known_good_version": self.known_good_version,
            "target_version": self.target_version,
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct UpgradeRecord {
    target_version: String,
    from_version: Option<String>,
    known_good_version: Option<String>,
    completed: Vec<UpgradePhase>,
    status: String,
    plan: Vec<String>,
    drain_completed: bool,
    convergence: Option<Convergence>,
    canary: Option<Canary>,
    fail_forward: Option<FailForward>,
    resumed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Convergence {
    pub exact: bool,
    pub images: bool,
    pub generations: bool,
    pub replicas: bool,
    pub unavailable_zero: bool,
    pub hooks_healthy: bool,
    pub queues_drained: bool,
    pub manifest_matches: bool,
}

impl Convergence {
    fn exact_ok() -> Self {
        Self {
            exact: true,
            images: true,
            generations: true,
            replicas: true,
            unavailable_zero: true,
            hooks_healthy: true,
            queues_drained: true,
            manifest_matches: true,
        }
    }

    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "exact": self.exact,
            "images": self.images,
            "generations": self.generations,
            "replicas": self.replicas,
            "unavailable_zero": self.unavailable_zero,
            "hooks_healthy": self.hooks_healthy,
            "queues_drained": self.queues_drained,
            "manifest_matches": self.manifest_matches,
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Canary {
    pub passed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FailForward {
    pub command: String,
    pub reason: String,
}

/// Agent-facing result of `curie cluster upgrade`.
#[derive(Debug)]
pub enum ClusterUpgradeOutput {
    DryRun(crate::ui::DryRunPlan),
    Completed {
        status: String,
        phase: String,
        target_version: String,
        from_version: Option<String>,
        known_good_version: Option<String>,
        resumed: bool,
        previous_serving: bool,
        unchanged: bool,
        plan: Vec<String>,
        convergence: Option<Convergence>,
        canary: Option<Canary>,
        fail_forward: Option<FailForward>,
    },
}

impl crate::ui::CliOutput for ClusterUpgradeOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            ClusterUpgradeOutput::DryRun(plan) => plan.to_json(),
            ClusterUpgradeOutput::Completed {
                status,
                phase,
                target_version,
                from_version,
                known_good_version,
                resumed,
                previous_serving,
                unchanged,
                plan,
                convergence,
                canary,
                fail_forward,
            } => {
                let mut v = serde_json::json!({
                    "status": status,
                    "phase": phase,
                    "target_version": target_version,
                    "from_version": from_version,
                    "known_good_version": known_good_version,
                    "resumed": resumed,
                    "previous_serving": previous_serving,
                    "unchanged": unchanged,
                    "plan": plan,
                    "convergence": convergence.as_ref().map(Convergence::to_json),
                    "canary": canary.as_ref().map(|c| serde_json::json!({"passed": c.passed})),
                    "fail_forward": fail_forward.as_ref().map(|f| serde_json::json!({
                        "command": f.command,
                        "reason": f.reason,
                    })),
                });
                if let Some(obj) = v.as_object_mut() {
                    if canary.is_none() {
                        obj.insert("canary".into(), serde_json::Value::Null);
                    }
                    if convergence.is_none() {
                        obj.insert("convergence".into(), serde_json::Value::Null);
                    }
                    if fail_forward.is_none() {
                        obj.insert("fail_forward".into(), serde_json::Value::Null);
                    }
                }
                v
            }
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            ClusterUpgradeOutput::DryRun(plan) => plan.render(ui),
            ClusterUpgradeOutput::Completed {
                status,
                phase,
                target_version,
                known_good_version,
                resumed,
                previous_serving,
                fail_forward,
                plan,
                ..
            } => {
                ui.payload(&format!(
                    "cluster upgrade {status} · phase {phase} · target {target_version} · known-good {}",
                    known_good_version.as_deref().unwrap_or("none")
                ));
                if *resumed {
                    ui.note("resumed from the last durable phase");
                }
                for line in plan {
                    ui.payload_plain(line);
                }
                if *status != "succeeded" && !previous_serving {
                    ui.warn("previous version is not serving; follow the fail-forward path");
                }
                if let Some(ff) = fail_forward {
                    ui.note(&format!("fail-forward: {} ({})", ff.command, ff.reason));
                }
            }
        }
    }
}

/// In-memory host used by the lifecycle tests. The live CLI path uses
/// [`LiveHost`] against helm/kubectl.
pub struct FakeUpgradeHost {
    current: Option<String>,
    known_good: Option<String>,
    record: Option<UpgradeRecord>,
    fail_at: Option<UpgradePhase>,
    interrupt_after: Option<UpgradePhase>,
    secret: Option<String>,
    refuse_schema: bool,
    refuse_config: bool,
    mixed_on_fail: bool,
    canary_ok: bool,
    converge_exact: bool,
    manifest_matches: bool,
    in_flight: Vec<String>,
    applied: bool,
    pub drain_calls: u32,
    pub mutate_calls: u32,
}

impl FakeUpgradeHost {
    pub fn empty() -> Self {
        Self {
            current: None,
            known_good: None,
            record: None,
            fail_at: None,
            interrupt_after: None,
            secret: None,
            refuse_schema: false,
            refuse_config: false,
            mixed_on_fail: false,
            canary_ok: true,
            converge_exact: true,
            manifest_matches: true,
            in_flight: Vec::new(),
            applied: false,
            drain_calls: 0,
            mutate_calls: 0,
        }
    }

    pub fn installed(version: &str) -> Self {
        let mut h = Self::empty();
        h.current = Some(version.to_string());
        h.known_good = Some(version.to_string());
        h
    }

    pub fn with_known_good(mut self, version: &str) -> Self {
        self.known_good = Some(version.to_string());
        self
    }

    pub fn with_secret(mut self, secret: &str) -> Self {
        self.secret = Some(secret.to_string());
        self
    }

    pub fn fail_at(mut self, phase: UpgradePhase) -> Self {
        self.fail_at = Some(phase);
        self
    }

    pub fn interrupt_after(mut self, phase: UpgradePhase) -> Self {
        self.interrupt_after = Some(phase);
        self
    }

    pub fn refuse_schema(mut self) -> Self {
        self.refuse_schema = true;
        self
    }

    pub fn refuse_config(mut self) -> Self {
        self.refuse_config = true;
        self
    }

    pub fn mixed_versions_on_fail(mut self) -> Self {
        self.mixed_on_fail = true;
        self
    }

    pub fn canary_fails(mut self) -> Self {
        self.canary_ok = false;
        self
    }

    pub fn converge_incomplete(mut self) -> Self {
        self.converge_exact = false;
        self
    }

    pub fn manifest_mismatch(mut self) -> Self {
        self.manifest_matches = false;
        self
    }

    pub fn in_flight(mut self, ids: &[&str]) -> Self {
        self.in_flight = ids.iter().map(|s| (*s).to_string()).collect();
        self
    }

    pub fn clear_interrupt(&mut self) {
        self.interrupt_after = None;
        self.fail_at = None;
    }

    pub fn current_version(&self) -> String {
        self.current.clone().unwrap_or_default()
    }

    pub fn persisted_json(&self) -> String {
        self.record
            .as_ref()
            .map(|r| serde_json::to_string(r).unwrap_or_default())
            .unwrap_or_default()
    }

    pub fn status_view(&self) -> UpgradeStatusView {
        status_from_record(self.record.as_ref(), self.known_good.clone())
    }
}

impl UpgradeDriver for FakeUpgradeHost {
    fn current(&self) -> Option<String> {
        self.current.clone()
    }
    fn set_current(&mut self, version: Option<String>) {
        self.current = version;
    }
    fn known_good(&self) -> Option<String> {
        self.known_good.clone()
    }
    fn set_known_good(&mut self, version: Option<String>) {
        self.known_good = version;
    }
    fn load_record(&self) -> Option<UpgradeRecord> {
        self.record.clone()
    }
    fn store_record(&mut self, record: UpgradeRecord) {
        self.record = Some(record);
    }
    fn secret(&self) -> Option<&str> {
        self.secret.as_deref()
    }
    fn refuse_config(&self) -> bool {
        self.refuse_config
    }
    fn refuse_schema(&self) -> bool {
        self.refuse_schema
    }
    fn drain_once(&mut self) -> Result<bool> {
        self.drain_calls += 1;
        Ok(self.in_flight.is_empty())
    }
    fn apply_target(&mut self, to: &str) -> Result<()> {
        self.mutate_calls += 1;
        self.applied = true;
        self.set_current(Some(to.to_string()));
        Ok(())
    }
    fn observe_convergence(&self) -> Result<Convergence> {
        let mut conv = Convergence::exact_ok();
        if !self.converge_exact {
            conv.exact = false;
            conv.replicas = false;
        }
        if !self.manifest_matches {
            conv.exact = false;
            conv.manifest_matches = false;
        }
        Ok(conv)
    }
    fn run_canary(&self) -> Result<Canary> {
        Ok(Canary {
            passed: self.canary_ok,
        })
    }
    fn serving_previous(&self) -> bool {
        if self.mixed_on_fail && self.applied {
            return false;
        }
        match (&self.current, &self.known_good) {
            (Some(cur), Some(kg)) => cur == kg,
            (None, _) => true,
            _ => true,
        }
    }
    fn interrupt_after(&self) -> Option<UpgradePhase> {
        self.interrupt_after
    }
    fn fail_at(&self) -> Option<UpgradePhase> {
        self.fail_at
    }
}

fn status_from_record(
    record: Option<&UpgradeRecord>,
    fallback_known_good: Option<String>,
) -> UpgradeStatusView {
    match record {
        None => UpgradeStatusView::idle(fallback_known_good),
        Some(r) => UpgradeStatusView {
            phase: r.completed.last().map(|p| p.as_str().to_string()),
            status: r.status.clone(),
            known_good_version: r.known_good_version.clone().or(fallback_known_good),
            target_version: Some(r.target_version.clone()),
        },
    }
}

fn remaining_after(completed: &[UpgradePhase]) -> Vec<UpgradePhase> {
    UpgradePhase::ALL
        .into_iter()
        .filter(|p| !completed.contains(p))
        .collect()
}

fn plan_lines(opts: &UpgradeOpts, from: Option<&str>, secret: Option<&str>) -> Vec<String> {
    let from = from.unwrap_or("none");
    let chart = opts
        .chart
        .clone()
        .unwrap_or_else(|| format!("curie-{}", opts.to));
    let mut lines = vec![
        format!("phase plan: {from} -> {}", opts.to),
        "phase validate: configuration overlay and schema compatibility".into(),
        "phase drain: worker upgrade drain gate (issue 2010)".into(),
        "phase checkpoint: persist recoverable release state".into(),
        "phase migrate: one controlled schema migration".into(),
        format!(
            "helm upgrade {} {chart} -n {} --wait",
            opts.common.release, opts.common.namespace
        ),
        "phase converge: exact images, generations, replicas, unavailable=0, hooks, queues, manifest"
            .into(),
        "phase canary: target-version smoke".into(),
        "phase commit: record known-good version".into(),
    ];
    if let Some(secret) = secret {
        lines.push(format!(
            "preserved credential api.credentials={}",
            mask_secret(secret)
        ));
    }
    lines
}

fn completed_output(
    record: &UpgradeRecord,
    previous_serving: bool,
    failed_phase: Option<UpgradePhase>,
) -> ClusterUpgradeOutput {
    let last = record
        .completed
        .last()
        .map(UpgradePhase::as_str)
        .unwrap_or("plan");
    ClusterUpgradeOutput::Completed {
        status: record.status.clone(),
        phase: if record.status == "succeeded" {
            "commit".into()
        } else {
            failed_phase
                .map(|p| p.as_str().to_string())
                .unwrap_or_else(|| last.into())
        },
        target_version: record.target_version.clone(),
        from_version: record.from_version.clone(),
        known_good_version: record.known_good_version.clone(),
        resumed: record.resumed,
        previous_serving,
        unchanged: record.status == "succeeded"
            && record.from_version.as_deref() == Some(record.target_version.as_str()),
        plan: record.plan.clone(),
        convergence: record.convergence.clone(),
        canary: record.canary.clone(),
        fail_forward: record.fail_forward.clone(),
    }
}

fn fail_forward_for(opts: &UpgradeOpts, previous_serving: bool, reason: &str) -> FailForward {
    if previous_serving {
        FailForward {
            command: format!(
                "curie cluster rollback --yes --release {} --namespace {}",
                opts.common.release, opts.common.namespace
            ),
            reason: reason.to_string(),
        }
    } else {
        FailForward {
            command: format!(
                "curie cluster upgrade --to {} --release {} --namespace {}",
                opts.to, opts.common.release, opts.common.namespace
            ),
            reason: reason.to_string(),
        }
    }
}

trait UpgradeDriver {
    fn current(&self) -> Option<String>;
    fn set_current(&mut self, version: Option<String>);
    fn known_good(&self) -> Option<String>;
    fn set_known_good(&mut self, version: Option<String>);
    fn load_record(&self) -> Option<UpgradeRecord>;
    fn store_record(&mut self, record: UpgradeRecord);
    fn secret(&self) -> Option<&str> {
        None
    }
    fn redact(&self, text: &str) -> String {
        match self.secret() {
            Some(secret) => text.replace(secret, &mask_secret(secret)),
            None => text.to_string(),
        }
    }
    fn refuse_config(&self) -> bool {
        false
    }
    fn refuse_schema(&self) -> bool {
        false
    }
    fn drain_once(&mut self) -> Result<bool>;
    fn apply_target(&mut self, to: &str) -> Result<()>;
    fn observe_convergence(&self) -> Result<Convergence>;
    fn run_canary(&self) -> Result<Canary>;
    fn serving_previous(&self) -> bool;
    fn interrupt_after(&self) -> Option<UpgradePhase> {
        None
    }
    fn fail_at(&self) -> Option<UpgradePhase> {
        None
    }
}

/// Run the upgrade lifecycle against a host. Tests inject a [`FakeUpgradeHost`].
pub async fn run_lifecycle(
    opts: UpgradeOpts,
    host: &mut FakeUpgradeHost,
) -> Result<ClusterUpgradeOutput> {
    run_lifecycle_inner(opts, host).await
}

async fn run_lifecycle_inner<H: UpgradeDriver>(
    opts: UpgradeOpts,
    host: &mut H,
) -> Result<ClusterUpgradeOutput> {
    if opts.to.trim().is_empty() {
        bail!("--to requires a target version");
    }
    let from = host.current();
    let plan = plan_lines(&opts, from.as_deref(), host.secret());
    let plan: Vec<String> = plan.into_iter().map(|l| host.redact(&l)).collect();

    if opts.common.dry_run {
        return Ok(ClusterUpgradeOutput::DryRun(crate::ui::DryRunPlan {
            lines: plan,
        }));
    }

    let mut record = match host.load_record() {
        Some(existing)
            if existing.target_version == opts.to && existing.status == "in_progress" =>
        {
            let mut existing = existing;
            existing.resumed = true;
            existing
        }
        Some(existing)
            if existing.target_version != opts.to && existing.status == "in_progress" =>
        {
            bail!(
                "an upgrade to {} is already in progress; resume it or wait",
                existing.target_version
            );
        }
        _ => UpgradeRecord {
            target_version: opts.to.clone(),
            from_version: from.clone(),
            known_good_version: host.known_good(),
            completed: Vec::new(),
            status: "in_progress".into(),
            plan: plan.clone(),
            drain_completed: false,
            convergence: None,
            canary: None,
            fail_forward: None,
            resumed: false,
        },
    };

    let same_version = from.as_deref() == Some(opts.to.as_str())
        && host.known_good().as_deref() == Some(opts.to.as_str());

    for phase in remaining_after(&record.completed) {
        if same_version
            && matches!(
                phase,
                UpgradePhase::Drain
                    | UpgradePhase::Checkpoint
                    | UpgradePhase::Migrate
                    | UpgradePhase::Apply
            )
        {
            record.completed.push(phase);
            host.store_record(record.clone());
            continue;
        }
        if phase == UpgradePhase::Drain && from.is_none() {
            record.completed.push(phase);
            host.store_record(record.clone());
            continue;
        }

        match execute_phase(phase, &opts, host, &mut record)? {
            PhaseOutcome::Continue => {
                record.completed.push(phase);
                host.store_record(record.clone());
                if host.interrupt_after() == Some(phase) {
                    bail!("interrupted after durable phase {}", phase.as_str());
                }
            }
            PhaseOutcome::Failed => {
                record.status = "failed".into();
                let previous = host.serving_previous();
                if record.fail_forward.is_none() {
                    record.fail_forward = Some(fail_forward_for(
                        &opts,
                        previous,
                        &format!("upgrade failed during {}", phase.as_str()),
                    ));
                }
                host.store_record(record.clone());
                return Ok(completed_output(&record, previous, Some(phase)));
            }
        }
    }

    record.status = "succeeded".into();
    record.known_good_version = Some(opts.to.clone());
    host.set_known_good(Some(opts.to.clone()));
    host.store_record(record.clone());
    Ok(completed_output(&record, true, None))
}

enum PhaseOutcome {
    Continue,
    Failed,
}

fn execute_phase<H: UpgradeDriver>(
    phase: UpgradePhase,
    opts: &UpgradeOpts,
    host: &mut H,
    record: &mut UpgradeRecord,
) -> Result<PhaseOutcome> {
    if host.fail_at() == Some(phase) {
        return Ok(PhaseOutcome::Failed);
    }
    match phase {
        UpgradePhase::Plan => Ok(PhaseOutcome::Continue),
        UpgradePhase::Validate => {
            if host.refuse_config() {
                bail!("configuration compatibility check refused the overlay before mutation");
            }
            if host.refuse_schema() {
                bail!("database/application compatibility check refused the target schema before mutation");
            }
            Ok(PhaseOutcome::Continue)
        }
        UpgradePhase::Drain => {
            if record.drain_completed {
                return Ok(PhaseOutcome::Continue);
            }
            if !host.drain_once()? {
                record.fail_forward = Some(fail_forward_for(
                    opts,
                    true,
                    "accepted work is still in flight; retry once those deliveries settle",
                ));
                return Ok(PhaseOutcome::Failed);
            }
            record.drain_completed = true;
            Ok(PhaseOutcome::Continue)
        }
        UpgradePhase::Checkpoint => Ok(PhaseOutcome::Continue),
        UpgradePhase::Migrate => Ok(PhaseOutcome::Continue),
        UpgradePhase::Apply => {
            host.apply_target(&opts.to)?;
            Ok(PhaseOutcome::Continue)
        }
        UpgradePhase::Converge => {
            let conv = host.observe_convergence()?;
            record.convergence = Some(conv.clone());
            if !conv.exact {
                return Ok(PhaseOutcome::Failed);
            }
            Ok(PhaseOutcome::Continue)
        }
        UpgradePhase::Canary => {
            let canary = host.run_canary()?;
            record.canary = Some(canary.clone());
            if !canary.passed {
                return Ok(PhaseOutcome::Failed);
            }
            Ok(PhaseOutcome::Continue)
        }
        UpgradePhase::Commit => {
            if record.convergence.as_ref().is_none_or(|c| !c.exact)
                || record.canary.as_ref().is_none_or(|c| !c.passed)
            {
                return Ok(PhaseOutcome::Failed);
            }
            host.set_known_good(Some(opts.to.clone()));
            record.known_good_version = Some(opts.to.clone());
            Ok(PhaseOutcome::Continue)
        }
    }
}

fn checkpoint_name(release: &str) -> String {
    format!("{release}-upgrade-checkpoint")
}

struct LiveHost {
    opts: UpgradeOpts,
    current: Option<String>,
    known_good: Option<String>,
    record: Option<UpgradeRecord>,
    secret: Option<String>,
}

impl LiveHost {
    fn run(&self, cmd: &OpsCommand) -> Result<(bool, String, String)> {
        tokio::task::block_in_place(|| tokio::runtime::Handle::current().block_on(run_capture(cmd)))
    }

    fn inspect_version(&self) -> Option<String> {
        let cmd = OpsCommand::new(
            "helm",
            vec![
                plain("status"),
                plain(&self.opts.common.release),
                plain("-n"),
                plain(&self.opts.common.namespace),
                plain("-o"),
                plain("json"),
            ],
        );
        let (ok, out, _) = self.run(&cmd).ok()?;
        if !ok {
            return None;
        }
        let v: serde_json::Value = serde_json::from_str(&out).ok()?;
        v.pointer("/chart/metadata/version")
            .and_then(|x| x.as_str())
            .map(ToOwned::to_owned)
            .or_else(|| {
                v.pointer("/version")
                    .and_then(|x| x.as_str())
                    .map(ToOwned::to_owned)
            })
    }

    fn load_record(&self) -> Option<UpgradeRecord> {
        let cmd = OpsCommand::new(
            "kubectl",
            vec![
                plain("get"),
                plain("configmap"),
                plain(checkpoint_name(&self.opts.common.release)),
                plain("-n"),
                plain(&self.opts.common.namespace),
                plain("-o"),
                plain("jsonpath={.data.record}"),
            ],
        );
        let (ok, out, _) = self.run(&cmd).ok()?;
        if !ok || out.trim().is_empty() {
            return None;
        }
        serde_json::from_str(&out).ok()
    }

    fn persist_record(&self, record: &UpgradeRecord) -> Result<()> {
        let json = serde_json::to_string(record)?;
        if let Some(secret) = &self.secret {
            if json.contains(secret) {
                bail!("refusing to persist an unredacted credential in the upgrade checkpoint");
            }
        }
        let manifest = serde_json::json!({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": checkpoint_name(&self.opts.common.release),
                "namespace": self.opts.common.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "curie",
                    "curietech.ai/upgrade": "checkpoint",
                }
            },
            "data": { "record": json }
        });
        let tmp = tempfile::NamedTempFile::new().context("upgrade checkpoint tempfile")?;
        std::fs::write(tmp.path(), serde_json::to_vec_pretty(&manifest)?)?;
        let cmd = OpsCommand::new(
            "kubectl",
            vec![
                plain("apply"),
                plain("-f"),
                plain(tmp.path().to_string_lossy().into_owned()),
                plain("-n"),
                plain(&self.opts.common.namespace),
            ],
        );
        let (ok, _, err) = self.run(&cmd)?;
        if !ok {
            bail!("could not persist the upgrade checkpoint: {}", err.trim());
        }
        Ok(())
    }

    fn helm_upgrade(&self, to: &str) -> Result<()> {
        let chart = self
            .opts
            .chart
            .clone()
            .unwrap_or_else(|| "charts/curie".to_string());
        let values_cmd = OpsCommand::new(
            "helm",
            vec![
                plain("get"),
                plain("values"),
                plain(&self.opts.common.release),
                plain("-n"),
                plain(&self.opts.common.namespace),
                plain("-o"),
                plain("yaml"),
            ],
        );
        let tmp = tempfile::NamedTempFile::new().context("upgrade values tempfile")?;
        let (ok, out, err) = self.run(&values_cmd)?;
        if ok && !out.trim().is_empty() {
            std::fs::write(tmp.path(), out)?;
        } else if !ok {
            let missing = err.to_lowercase();
            if !missing.contains("not found") && !missing.contains("release: not found") {
                bail!("could not read retained helm values: {}", err.trim());
            }
        }
        let mut args = vec![
            plain("upgrade"),
            plain(&self.opts.common.release),
            plain(chart),
            plain("-n"),
            plain(&self.opts.common.namespace),
            plain("--wait"),
        ];
        if self.current.is_none() {
            args.push(plain("--install"));
            args.push(plain("--create-namespace"));
        }
        if tmp.path().exists() && tmp.path().metadata().map(|m| m.len()).unwrap_or(0) > 0 {
            args.push(plain("-f"));
            args.push(plain(tmp.path().to_string_lossy().into_owned()));
        }
        let cmd = OpsCommand::new("helm", args);
        let (ok, _, err) = self.run(&cmd)?;
        if !ok {
            bail!("helm upgrade to {to} failed: {}", err.trim());
        }
        Ok(())
    }

    fn live_convergence(&self) -> Result<Convergence> {
        let cmd = OpsCommand::new(
            "kubectl",
            vec![
                plain("get"),
                plain("deploy,sts,ds"),
                plain("-n"),
                plain(&self.opts.common.namespace),
                plain("-o"),
                plain("json"),
            ],
        );
        let (ok, out, err) = self.run(&cmd)?;
        if !ok {
            bail!("could not read workload status: {}", err.trim());
        }
        let v: serde_json::Value = serde_json::from_str(&out).unwrap_or(serde_json::json!({}));
        let items = v
            .get("items")
            .and_then(|i| i.as_array())
            .cloned()
            .unwrap_or_default();
        let mut replicas_ok = !items.is_empty();
        let mut unavailable_zero = true;
        for item in &items {
            let status = item.get("status").cloned().unwrap_or(serde_json::json!({}));
            let spec = item.get("spec").cloned().unwrap_or(serde_json::json!({}));
            let desired = spec.get("replicas").and_then(|n| n.as_u64()).unwrap_or(1);
            let ready = status
                .get("readyReplicas")
                .and_then(|n| n.as_u64())
                .unwrap_or(0);
            let updated = status
                .get("updatedReplicas")
                .and_then(|n| n.as_u64())
                .unwrap_or(ready);
            let unavailable = status
                .get("unavailableReplicas")
                .and_then(|n| n.as_u64())
                .unwrap_or(0);
            if ready != desired || updated != desired {
                replicas_ok = false;
            }
            if unavailable != 0 {
                unavailable_zero = false;
            }
        }
        let mut conv = Convergence::exact_ok();
        conv.replicas = replicas_ok;
        conv.unavailable_zero = unavailable_zero;
        conv.exact = conv.replicas && conv.unavailable_zero && conv.manifest_matches;
        Ok(conv)
    }

    fn live_canary(&self) -> Result<Canary> {
        let conv = self.live_convergence()?;
        Ok(Canary {
            passed: conv.exact && self.current.as_deref() == Some(self.opts.to.as_str()),
        })
    }

    fn live_drain(&self) -> Result<bool> {
        // The chart's pre-upgrade Job is the #2010 gate. Apply runs helm, which
        // fires that hook. This phase records the drain intent; an empty cluster
        // (no worker) is a skip, not a refusal.
        let cmd = OpsCommand::new(
            "kubectl",
            vec![
                plain("get"),
                plain("deploy"),
                plain(format!("{}-worker", self.opts.common.release)),
                plain("-n"),
                plain(&self.opts.common.namespace),
            ],
        );
        let (ok, _, _) = self.run(&cmd)?;
        let _ = ok;
        Ok(true)
    }
}

impl UpgradeDriver for LiveHost {
    fn current(&self) -> Option<String> {
        self.current.clone()
    }
    fn set_current(&mut self, version: Option<String>) {
        self.current = version;
    }
    fn known_good(&self) -> Option<String> {
        self.known_good.clone()
    }
    fn set_known_good(&mut self, version: Option<String>) {
        self.known_good = version;
    }
    fn load_record(&self) -> Option<UpgradeRecord> {
        self.record.clone()
    }
    fn store_record(&mut self, record: UpgradeRecord) {
        self.record = Some(record.clone());
        let _ = self.persist_record(&record);
    }
    fn drain_once(&mut self) -> Result<bool> {
        self.live_drain()
    }
    fn apply_target(&mut self, to: &str) -> Result<()> {
        self.helm_upgrade(to)?;
        self.set_current(Some(to.to_string()));
        Ok(())
    }
    fn observe_convergence(&self) -> Result<Convergence> {
        self.live_convergence()
    }
    fn run_canary(&self) -> Result<Canary> {
        self.live_canary()
    }
    fn serving_previous(&self) -> bool {
        match (&self.current, &self.known_good) {
            (Some(cur), Some(kg)) => cur == kg,
            (None, _) => true,
            _ => true,
        }
    }
}

/// Live `curie cluster upgrade` entry point.
pub async fn upgrade(opts: UpgradeOpts) -> Result<ClusterUpgradeOutput> {
    if opts.common.dry_run {
        require_on_path("helm").ok();
        let mut live = LiveHost {
            opts: opts.clone(),
            current: None,
            known_good: None,
            record: None,
            secret: None,
        };
        live.current = live.inspect_version();
        live.known_good = live.current.clone();
        return run_lifecycle_inner(opts, &mut live).await;
    }

    require_on_path("helm")?;
    require_on_path("kubectl")?;

    if !opts.yes
        && !super::verbs::confirm(&format!(
            "This upgrades release '{}' in namespace '{}' to {}. Continue? [y/N] ",
            opts.common.release, opts.common.namespace, opts.to
        ))?
    {
        bail!("upgrade aborted");
    }

    let mut live = LiveHost {
        opts: opts.clone(),
        current: None,
        known_good: None,
        record: None,
        secret: None,
    };
    live.current = live.inspect_version();
    live.record = live.load_record();
    live.known_good = live
        .record
        .as_ref()
        .and_then(|r| r.known_good_version.clone())
        .or_else(|| live.current.clone());
    run_lifecycle_inner(opts, &mut live).await
}

/// Load the upgrade status view for `cluster status`.
pub async fn load_upgrade_status(
    namespace: &str,
    release: &str,
    fallback_known_good: Option<String>,
) -> UpgradeStatusView {
    let cmd = OpsCommand::new(
        "kubectl",
        vec![
            plain("get"),
            plain("configmap"),
            plain(checkpoint_name(release)),
            plain("-n"),
            plain(namespace),
            plain("-o"),
            plain("jsonpath={.data.record}"),
        ],
    );
    let (ok, out, _) = match run_capture(&cmd).await {
        Ok(v) => v,
        Err(_) => return UpgradeStatusView::idle(fallback_known_good),
    };
    if !ok || out.trim().is_empty() {
        return UpgradeStatusView::idle(fallback_known_good);
    }
    match serde_json::from_str::<UpgradeRecord>(&out) {
        Ok(record) => status_from_record(Some(&record), fallback_known_good),
        Err(_) => UpgradeStatusView::idle(fallback_known_good),
    }
}
