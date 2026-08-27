//! Multi-sample aggregation for the CLI parity eval path (#1907).
//!
//! Mirrors `apps/worker/src/curie_worker/eval/sampling.py`. The two cannot share
//! code across languages, so both replay `tests/vectors/eval-sampling.json`.

use clap::ValueEnum;
use serde::{Deserialize, Serialize};

use crate::evals::CaseOutcome;

/// How `n` per-sample verdicts reduce to one. Wire tokens match the worker
/// (`majority`, `pass_at_k`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ValueEnum)]
#[serde(rename_all = "snake_case")]
pub enum AggregationPolicy {
    Majority,
    #[value(name = "pass_at_k", alias = "pass-at-k")]
    #[serde(rename = "pass_at_k")]
    PassAtK,
}

impl AggregationPolicy {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Majority => "majority",
            Self::PassAtK => "pass_at_k",
        }
    }
}

impl std::fmt::Display for AggregationPolicy {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// How many times to run each case and how to reduce the verdicts.
///
/// `n=1` (the default) is a no-op: the single sample is returned unchanged.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SampleConfig {
    pub n: u32,
    pub policy: AggregationPolicy,
    pub k: u32,
}

impl Default for SampleConfig {
    fn default() -> Self {
        Self {
            n: 1,
            policy: AggregationPolicy::Majority,
            k: 1,
        }
    }
}

impl SampleConfig {
    pub fn new(n: u32, policy: AggregationPolicy, k: u32) -> anyhow::Result<Self> {
        if n < 1 {
            anyhow::bail!("samples n must be >= 1, got {n}");
        }
        if k < 1 {
            anyhow::bail!("pass@k k must be >= 1, got {k}");
        }
        Ok(Self { n, policy, k })
    }

    pub fn effective_k(self) -> u32 {
        self.k.min(self.n)
    }
}

/// One independent sample of a case, before aggregation.
#[derive(Debug, Clone, PartialEq)]
pub struct SampleRecord {
    pub outcome: CaseOutcome,
    pub output: String,
    /// Seconds for this sample. The aggregate sums them, matching the worker.
    pub seconds: f64,
    /// None when the turn completed and was graded (or plumbing). Some when the
    /// turn never produced a verdict -- the CLI equivalent of EvalCaseResult.error.
    pub error: Option<String>,
}

/// Reduced verdict for one case after N samples.
#[derive(Debug, Clone, PartialEq)]
pub struct AggregatedCase {
    pub outcome: CaseOutcome,
    pub output: String,
    pub seconds: f64,
    pub samples: u32,
    pub passes: u32,
    pub policy: AggregationPolicy,
    pub variance: Option<String>,
    pub error: Option<String>,
}

fn sample_completed(sample: &SampleRecord) -> bool {
    match sample.outcome {
        CaseOutcome::Fail => sample.error.as_deref().is_none_or(str::is_empty),
        _ => true,
    }
}

fn representative(samples: &[SampleRecord], outcome: CaseOutcome) -> &SampleRecord {
    samples
        .iter()
        .find(|sample| sample.outcome == outcome)
        .unwrap_or(&samples[0])
}

/// Reduce `n` per-sample results for one case to a single verdict.
///
/// A single sample is returned as an identity-shaped aggregate so `n=1` matches
/// the one-shot result. Variance rides `variance`; `error` is set only when no
/// sample completed (issue #857).
pub fn aggregate_samples(samples: &[SampleRecord], config: SampleConfig) -> AggregatedCase {
    assert!(
        !samples.is_empty(),
        "aggregate_samples requires at least one sample"
    );
    if samples.len() == 1 {
        let sample = &samples[0];
        return AggregatedCase {
            outcome: sample.outcome,
            output: sample.output.clone(),
            seconds: sample.seconds,
            samples: 1,
            passes: u32::from(sample.outcome == CaseOutcome::Pass),
            policy: config.policy,
            variance: None,
            error: sample.error.clone(),
        };
    }

    let total_seconds: f64 = samples.iter().map(|s| s.seconds).sum();
    if samples.iter().all(|s| s.outcome == CaseOutcome::PlumbingOk) {
        return AggregatedCase {
            outcome: CaseOutcome::PlumbingOk,
            output: samples[0].output.clone(),
            seconds: total_seconds,
            samples: samples.len() as u32,
            passes: 0,
            policy: config.policy,
            variance: None,
            error: None,
        };
    }

    let graded: Vec<&SampleRecord> = samples
        .iter()
        .filter(|s| s.outcome != CaseOutcome::PlumbingOk)
        .collect();
    let passes = graded
        .iter()
        .filter(|s| s.outcome == CaseOutcome::Pass)
        .count() as u32;
    let graded_count = graded.len() as u32;
    let (green, bar) = match config.policy {
        AggregationPolicy::PassAtK => (
            passes >= config.effective_k(),
            format!("pass@{}", config.effective_k()),
        ),
        AggregationPolicy::Majority => (passes * 2 > graded_count, "majority".to_string()),
    };
    let outcome = if green {
        CaseOutcome::Pass
    } else {
        CaseOutcome::Fail
    };
    let variance = format!("{passes}/{graded_count} samples passed ({bar})");
    let representative = representative(samples, outcome);
    let none_completed = !samples.iter().any(sample_completed);
    AggregatedCase {
        outcome,
        output: representative.output.clone(),
        seconds: total_seconds,
        samples: samples.len() as u32,
        passes,
        policy: config.policy,
        variance: Some(variance.clone()),
        error: none_completed.then(|| format!("variance-aware grading failed: {variance}")),
    }
}
