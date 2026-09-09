//! #2350: `cluster deploy` must wait for connector Deployments and fail named
//! when rollout does not become ready. Pure observation/redaction tests always
//! run. Live Kubernetes cases need `CURIE_E2E_CLUSTER=1` and a disposable
//! context; they never touch shared namespaces.

use std::collections::BTreeMap;
use std::process::Command;
use std::time::{Duration, Instant};

use curie::connectors::{
    connector_workloads, current_template_hash, observe_rollout, redact_last_log,
    remaining_timeout, rollout_failure, rollout_status_args, ConnectorWorkload, RolloutObservation,
    CONNECTOR_ROLLOUT_DEADLINE, LAST_LOG_MAX_CHARS, LAST_LOG_TAIL_LINES,
};
use curie::exit::{classify, ExitClass};
use serde_json::{json, Value};

fn urls(pairs: &[(&str, &str)]) -> BTreeMap<String, Value> {
    pairs
        .iter()
        .map(|(name, url)| ((*name).to_string(), json!({"url": url})))
        .collect()
}

fn deployment(name: &str) -> Value {
    json!({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name},
    })
}

fn service(name: &str) -> Value {
    json!({
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name},
    })
}

fn deploy_status(replicas: u64, ready: u64, available: u64) -> Value {
    json!({
        "metadata": {"generation": 1},
        "spec": {"replicas": replicas},
        "status": {
            "observedGeneration": 1,
            "replicas": replicas,
            "updatedReplicas": replicas,
            "readyReplicas": ready,
            "availableReplicas": available,
        }
    })
}

fn pod_waiting_hash(name: &str, reason: &str, hash: &str) -> Value {
    json!({
        "metadata": {
            "name": name,
            "labels": {"pod-template-hash": hash}
        },
        "status": {
            "containerStatuses": [{
                "name": "server",
                "state": {"waiting": {"reason": reason, "message": "should-never-surface"}}
            }]
        }
    })
}

fn pod_waiting(name: &str, reason: &str) -> Value {
    json!({
        "metadata": {"name": name},
        "status": {
            "containerStatuses": [{
                "name": "server",
                "state": {"waiting": {"reason": reason, "message": "should-never-surface"}}
            }]
        }
    })
}

fn pod_terminated(name: &str, reason: &str, exit: u64) -> Value {
    json!({
        "metadata": {"name": name},
        "status": {
            "containerStatuses": [{
                "name": "server",
                "state": {"terminated": {"reason": reason, "exitCode": exit, "message": "raw"}}
            }]
        }
    })
}

#[test]
fn no_connectors_yield_no_workloads() {
    assert!(connector_workloads(&[], &BTreeMap::new()).is_empty());
}

#[test]
fn remote_url_without_deployment_is_not_waited() {
    let manifests = vec![service("curie-acme-bot-mcp-internal")];
    let mcp = urls(&[("internal", "https://mcp.example.com/mcp")]);
    assert!(connector_workloads(&manifests, &mcp).is_empty());
}

#[test]
fn hosted_deployment_is_named_from_the_mcp_url() {
    let name = "curie-acme-bot-mcp-github";
    let manifests = vec![
        deployment(name),
        service(name),
        json!({"kind": "NetworkPolicy", "metadata": {"name": format!("{name}-allow")}}),
    ];
    let mcp = urls(&[(
        "github",
        "http://curie-acme-bot-mcp-github.curie.svc.cluster.local:8000/mcp",
    )]);
    assert_eq!(
        connector_workloads(&manifests, &mcp),
        vec![ConnectorWorkload {
            connector: "github".into(),
            deployment: name.into(),
        }]
    );
}

#[test]
fn ready_replicas_are_healthy() {
    assert_eq!(
        observe_rollout(&deploy_status(1, 1, 1), &[], None),
        RolloutObservation::Ready
    );
}

#[test]
fn leftover_ready_replicas_are_not_the_new_revision() {
    let stale = json!({
        "metadata": {"generation": 2},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "updatedReplicas": 0,
            "readyReplicas": 1,
            "availableReplicas": 1,
        }
    });
    assert_eq!(
        observe_rollout(&stale, &[], None),
        RolloutObservation::Pending
    );
}

#[test]
fn overlapping_old_ready_pod_is_not_the_new_revision() {
    let overlapping = json!({
        "metadata": {"generation": 2},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": 2,
            "replicas": 2,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
        }
    });
    assert_eq!(
        observe_rollout(
            &overlapping,
            &[pod_waiting_hash("new-pod", "ContainerCreating", "newhash")],
            Some("newhash"),
        ),
        RolloutObservation::Pending
    );
}

#[test]
fn stale_progress_deadline_does_not_fail_a_new_generation() {
    let stale = json!({
        "metadata": {"generation": 2},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": 0,
            "availableReplicas": 0,
            "conditions": [{
                "type": "Progressing",
                "reason": "ProgressDeadlineExceeded",
                "status": "False"
            }]
        }
    });
    assert_eq!(
        observe_rollout(&stale, &[], None),
        RolloutObservation::Pending
    );
}

#[test]
fn crashloop_observation_fails_named_connector() {
    let observation = observe_rollout(
        &deploy_status(1, 0, 0),
        &[pod_waiting(
            "curie-acme-bot-mcp-github-abc",
            "CrashLoopBackOff",
        )],
        None,
    );
    assert_eq!(
        observation,
        RolloutObservation::Failed {
            reason: "CrashLoopBackOff",
            pod: Some("curie-acme-bot-mcp-github-abc".into()),
        }
    );
    let err = rollout_failure(
        "github",
        "curie",
        "curie-acme-bot-mcp-github",
        "CrashLoopBackOff",
        Some("Error: unknown command \"http\" for \"server\""),
    );
    let text = format!("{err:#}");
    assert!(text.contains("connector github"), "{text}");
    assert!(text.contains("CrashLoopBackOff"), "{text}");
    assert!(text.contains("unknown command"), "{text}");
    let (class, fix) = classify(&err);
    assert_eq!(class, ExitClass::Failure);
    let fix = fix.expect("recovery command");
    assert!(
        fix.contains("kubectl -n curie logs deploy/curie-acme-bot-mcp-github --tail=50"),
        "{fix}"
    );
    assert!(fix.contains("curie cluster deploy"), "{fix}");
}

#[test]
fn image_pull_failure_is_terminal() {
    for reason in ["ImagePullBackOff", "ErrImagePull", "InvalidImageName"] {
        let observation = observe_rollout(
            &deploy_status(1, 0, 0),
            &[pod_waiting("mcp-github-xyz", reason)],
            None,
        );
        assert_eq!(
            observation,
            RolloutObservation::Failed {
                reason,
                pod: Some("mcp-github-xyz".into()),
            },
            "{reason}"
        );
    }
}

#[test]
fn run_container_error_is_terminal() {
    let observation = observe_rollout(
        &deploy_status(1, 0, 0),
        &[pod_terminated("mcp-crash-xyz", "RunContainerError", 127)],
        None,
    );
    assert_eq!(
        observation,
        RolloutObservation::Failed {
            reason: "RunContainerError",
            pod: Some("mcp-crash-xyz".into()),
        }
    );
}

#[test]
fn container_creating_stays_pending() {
    assert_eq!(
        observe_rollout(
            &deploy_status(1, 0, 0),
            &[pod_waiting("mcp-github-xyz", "ContainerCreating")],
            None,
        ),
        RolloutObservation::Pending
    );
}

#[test]
fn kubernetes_message_fields_never_enter_the_observation() {
    let observation = observe_rollout(
        &deploy_status(1, 0, 0),
        &[pod_waiting("mcp-github-xyz", "CrashLoopBackOff")],
        None,
    );
    let rendered = format!("{observation:?}");
    assert!(!rendered.contains("should-never-surface"), "{rendered}");
}

#[test]
fn remaining_timeout_is_none_at_the_deadline() {
    let now = Instant::now();
    assert!(remaining_timeout(now + Duration::from_secs(5), now).is_some());
    assert!(remaining_timeout(now, now).is_none());
    assert!(remaining_timeout(now, now + Duration::from_secs(1)).is_none());
}

#[test]
fn rollout_wait_uses_the_comms_deadline() {
    let args = rollout_status_args(
        "curie",
        "curie-acme-bot-mcp-github",
        CONNECTOR_ROLLOUT_DEADLINE,
    );
    assert!(args.iter().any(|a| a == "--timeout=120s"), "{args:?}");
    assert_eq!(CONNECTOR_ROLLOUT_DEADLINE, Duration::from_secs(120));
}

#[test]
fn last_log_is_bounded_and_redacts_connector_secrets() {
    let mut secrets = BTreeMap::new();
    secrets.insert(
        "GITHUB_PERSONAL_ACCESS_TOKEN".into(),
        "gho_this_is_not_a_real_token".into(),
    );
    let mut lines = vec!["Authorization: Bearer gho_this_is_not_a_real_token".to_string()];
    lines.extend((0..40).map(|i| format!("noise-{i}")));
    lines.push("password=super-secret".into());
    let redacted = redact_last_log(&lines.join("\n"), &secrets);
    assert!(
        !redacted.contains("gho_this_is_not_a_real_token"),
        "{redacted}"
    );
    assert!(!redacted.contains("super-secret"), "{redacted}");
    assert!(
        redacted.contains("[REDACTED]") || redacted.contains("<redacted>"),
        "{redacted}"
    );
    assert!(
        redacted.lines().count() <= LAST_LOG_TAIL_LINES,
        "{redacted}"
    );
    assert!(
        redacted.chars().count() <= LAST_LOG_MAX_CHARS + 4,
        "{redacted}"
    );
}

#[test]
fn last_log_redacts_json_credential_fields() {
    let redacted = redact_last_log(
        r#"{"token":"sensitive-value","password":"also-secret"}"#,
        &BTreeMap::new(),
    );
    assert!(!redacted.contains("sensitive-value"), "{redacted}");
    assert!(!redacted.contains("also-secret"), "{redacted}");
}

#[test]
fn superseded_crashloop_does_not_fail_the_new_revision() {
    let replicasets = vec![
        json!({
            "metadata": {
                "annotations": {"deployment.kubernetes.io/revision": "1"},
                "labels": {"pod-template-hash": "oldhash"}
            }
        }),
        json!({
            "metadata": {
                "annotations": {"deployment.kubernetes.io/revision": "2"},
                "labels": {"pod-template-hash": "newhash"}
            }
        }),
    ];
    let hash = current_template_hash(&replicasets);
    assert_eq!(hash.as_deref(), Some("newhash"));
    let observation = observe_rollout(
        &deploy_status(1, 0, 0),
        &[
            pod_waiting_hash("old-pod", "CrashLoopBackOff", "oldhash"),
            pod_waiting_hash("new-pod", "ContainerCreating", "newhash"),
        ],
        hash.as_deref(),
    );
    assert_eq!(observation, RolloutObservation::Pending);
}

#[test]
fn rollout_failure_omits_an_excerpt_that_still_carries_a_secret() {
    let err = rollout_failure(
        "github",
        "curie",
        "curie-acme-bot-mcp-github",
        "CrashLoopBackOff",
        None,
    );
    let text = format!("{err:#}");
    assert!(!text.contains("gho_"), "{text}");
    assert!(!text.to_lowercase().contains("last log"), "{text}");
}

fn live_cluster() -> bool {
    std::env::var("CURIE_E2E_CLUSTER").ok().as_deref() == Some("1")
}

fn kubectl(args: &[&str]) -> (bool, String, String) {
    let output = Command::new("kubectl")
        .args(args)
        .output()
        .expect("kubectl");
    (
        output.status.success(),
        String::from_utf8_lossy(&output.stdout).into_owned(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

fn apply_doc(namespace: &str, doc: &Value) {
    let body = serde_json::to_string(doc).expect("json");
    let mut child = Command::new("kubectl")
        .args(["-n", namespace, "apply", "-f", "-"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("apply");
    {
        use std::io::Write;
        child
            .stdin
            .as_mut()
            .expect("stdin")
            .write_all(body.as_bytes())
            .expect("write");
    }
    let output = child.wait_with_output().expect("wait apply");
    assert!(
        output.status.success(),
        "apply failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn minimal_deployment(name: &str, image: &str, command: &[&str]) -> Value {
    let pull = if image.contains("does-not-exist") {
        "Always"
    } else {
        "IfNotPresent"
    };
    json!({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "labels": {"app.kubernetes.io/name": name}
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": name}},
                "spec": {
                    "containers": [{
                        "name": "server",
                        "image": image,
                        "imagePullPolicy": pull,
                        "command": command,
                    }]
                }
            }
        }
    })
}

#[tokio::test]
async fn live_cluster_ready_crashloop_image_pull_timeout_and_empty() {
    if !live_cluster() {
        eprintln!("skipping: set CURIE_E2E_CLUSTER=1 for disposable Kubernetes proof");
        return;
    }
    let ns = format!(
        "curie-2350-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_secs()
    );
    let (ok, _, err) = kubectl(&["create", "namespace", &ns]);
    assert!(ok, "create namespace {ns}: {err}");
    struct DeleteNs(String);
    impl Drop for DeleteNs {
        fn drop(&mut self) {
            let _ = Command::new("kubectl")
                .args(["delete", "namespace", &self.0, "--wait=false"])
                .output();
        }
    }
    let _guard = DeleteNs(ns.clone());

    let target = curie::connectors::bind_current_cluster(&ns, "curie")
        .await
        .expect("bind cluster");

    curie::connectors::wait_for_connector_rollouts(
        &target,
        &ns,
        &[],
        &BTreeMap::new(),
        Instant::now() + Duration::from_secs(5),
    )
    .await
    .expect("empty wait");

    apply_doc(
        &ns,
        &minimal_deployment("mcp-sleep", "busybox:1.36", &["sleep", "3600"]),
    );
    curie::connectors::wait_for_connector_rollouts(
        &target,
        &ns,
        &[ConnectorWorkload {
            connector: "sleep".into(),
            deployment: "mcp-sleep".into(),
        }],
        &BTreeMap::new(),
        Instant::now() + Duration::from_secs(60),
    )
    .await
    .expect("healthy sleep connector");

    let leak = "gho_this_is_not_a_real_token";
    apply_doc(
        &ns,
        &minimal_deployment(
            "mcp-crash",
            "busybox:1.36",
            &["sh", "-c", &format!("echo {leak}; http --port 8000")],
        ),
    );
    let mut secrets = BTreeMap::new();
    secrets.insert("GITHUB_PERSONAL_ACCESS_TOKEN".into(), leak.into());
    let err = curie::connectors::wait_for_connector_rollouts(
        &target,
        &ns,
        &[ConnectorWorkload {
            connector: "github".into(),
            deployment: "mcp-crash".into(),
        }],
        &secrets,
        Instant::now() + Duration::from_secs(60),
    )
    .await
    .expect_err("crashloop must be nonzero");
    let text = format!("{err:#}");
    assert!(text.contains("connector github"), "{text}");
    assert!(!text.contains(leak), "secret leaked in {text}");
    let (class, fix) = classify(&err);
    assert_eq!(class, ExitClass::Failure);
    let fix = fix.expect("fix");
    assert!(fix.contains("kubectl -n"), "recovery command: {fix}");
    assert!(!fix.contains(leak), "secret leaked in fix {fix}");

    apply_doc(
        &ns,
        &minimal_deployment(
            "mcp-pull",
            "ghcr.io/curie-eng/does-not-exist:2350-no-such-tag",
            &["http"],
        ),
    );
    let err = curie::connectors::wait_for_connector_rollouts(
        &target,
        &ns,
        &[ConnectorWorkload {
            connector: "pullfail".into(),
            deployment: "mcp-pull".into(),
        }],
        &BTreeMap::new(),
        Instant::now() + Duration::from_secs(60),
    )
    .await
    .expect_err("image pull must be nonzero");
    let text = format!("{err:#}");
    assert!(text.contains("connector pullfail"), "{text}");
    assert!(
        text.contains("ImagePull")
            || text.contains("ErrImagePull")
            || text.contains("InvalidImage"),
        "{text}"
    );

    let stuck = json!({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "mcp-stuck",
            "labels": {"app.kubernetes.io/name": "mcp-stuck"}
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app.kubernetes.io/name": "mcp-stuck"}},
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": "mcp-stuck"}},
                "spec": {
                    "nodeSelector": {"kubernetes.io/hostname": "no-such-node-2350"},
                    "containers": [{
                        "name": "server",
                        "image": "busybox:1.36",
                        "imagePullPolicy": "IfNotPresent",
                        "command": ["sleep", "3600"],
                    }]
                }
            }
        }
    });
    apply_doc(&ns, &stuck);
    let started = Instant::now();
    let err = curie::connectors::wait_for_connector_rollouts(
        &target,
        &ns,
        &[ConnectorWorkload {
            connector: "stuck".into(),
            deployment: "mcp-stuck".into(),
        }],
        &BTreeMap::new(),
        Instant::now() + Duration::from_secs(4),
    )
    .await
    .expect_err("unschedulable wait must time out");
    assert!(
        started.elapsed() < Duration::from_secs(15),
        "deadline must bound the wait"
    );
    let text = format!("{err:#}");
    assert!(text.contains("connector stuck"), "{text}");
    assert!(text.contains("timeout"), "{text}");

    let (ok, _, err) = kubectl(&["delete", "namespace", &ns, "--wait=true", "--timeout=60s"]);
    assert!(
        ok || err.contains("NotFound"),
        "delete namespace {ns}: {err}"
    );
}
