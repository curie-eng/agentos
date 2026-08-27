//! Shared eval-sampling vectors (#1907): the CLI aggregator must match the
//! worker. A one-fail-two-pass identity case is GREEN under majority and still
//! reports 2/3, so a stochastic miss is not silent tier drift.

use std::path::Path;

use curie::eval_sampling::{aggregate_samples, AggregationPolicy, SampleConfig, SampleRecord};
use curie::evals::CaseOutcome;
use serde::Deserialize;

#[derive(Deserialize)]
struct VectorFile {
    vectors: Vec<Vector>,
}

#[derive(Deserialize)]
struct Vector {
    name: String,
    n: u32,
    policy: String,
    k: u32,
    samples: Vec<VectorSample>,
    aggregate_outcome: String,
    passes: u32,
    error: Option<String>,
    variance: Option<String>,
    representative_output: String,
}

#[derive(Deserialize)]
struct VectorSample {
    outcome: String,
    output: String,
    error: Option<String>,
}

fn parse_outcome(token: &str) -> CaseOutcome {
    match token {
        "pass" => CaseOutcome::Pass,
        "fail" => CaseOutcome::Fail,
        "plumbing_ok" => CaseOutcome::PlumbingOk,
        other => panic!("unknown outcome {other}"),
    }
}

fn parse_policy(token: &str) -> AggregationPolicy {
    match token {
        "majority" => AggregationPolicy::Majority,
        "pass_at_k" => AggregationPolicy::PassAtK,
        other => panic!("unknown policy {other}"),
    }
}

fn outcome_token(outcome: CaseOutcome) -> &'static str {
    match outcome {
        CaseOutcome::Pass => "pass",
        CaseOutcome::Fail => "fail",
        CaseOutcome::PlumbingOk => "plumbing_ok",
    }
}

#[test]
fn rust_replays_the_shared_eval_sampling_vectors() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/vectors/eval-sampling.json");
    let body = std::fs::read_to_string(&path).expect("eval-sampling.json must exist");
    let file: VectorFile = serde_json::from_str(&body).expect("eval-sampling.json must parse");
    assert!(
        file.vectors
            .iter()
            .any(|v| v.name.contains("two of three identity")),
        "the identity 2/3 vector is the #1907 regression"
    );

    for vector in file.vectors {
        let samples: Vec<SampleRecord> = vector
            .samples
            .iter()
            .map(|sample| SampleRecord {
                outcome: parse_outcome(&sample.outcome),
                output: sample.output.clone(),
                seconds: 1.0,
                error: sample.error.clone(),
            })
            .collect();
        let config = SampleConfig::new(vector.n, parse_policy(&vector.policy), vector.k)
            .unwrap_or_else(|err| panic!("{}: {err}", vector.name));
        let agg = aggregate_samples(&samples, config);
        assert_eq!(
            outcome_token(agg.outcome),
            vector.aggregate_outcome,
            "{}",
            vector.name
        );
        assert_eq!(agg.error, vector.error, "{}", vector.name);
        assert_eq!(agg.variance, vector.variance, "{}", vector.name);
        assert_eq!(agg.output, vector.representative_output, "{}", vector.name);
        if vector.n > 1 {
            assert_eq!(agg.samples, vector.n, "{}", vector.name);
            assert_eq!(agg.passes, vector.passes, "{}", vector.name);
        }
    }
}

#[test]
fn one_failing_identity_sample_among_passing_samples_is_majority_green() {
    let samples = vec![
        SampleRecord {
            outcome: CaseOutcome::Pass,
            output: "I am translation-bot".into(),
            seconds: 1.0,
            error: None,
        },
        SampleRecord {
            outcome: CaseOutcome::Fail,
            output: "¿Quién eres?".into(),
            seconds: 1.0,
            error: None,
        },
        SampleRecord {
            outcome: CaseOutcome::Pass,
            output: "translation-bot here".into(),
            seconds: 1.0,
            error: None,
        },
    ];
    let agg = aggregate_samples(
        &samples,
        SampleConfig::new(3, AggregationPolicy::Majority, 1).unwrap(),
    );
    assert_eq!(agg.outcome, CaseOutcome::Pass);
    assert_eq!(agg.passes, 2);
    assert_eq!(agg.samples, 3);
    assert_eq!(agg.policy, AggregationPolicy::Majority);
    assert_eq!(
        agg.variance.as_deref(),
        Some("2/3 samples passed (majority)")
    );
    assert!(agg.error.is_none());
}
