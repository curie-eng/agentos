//! Apply the connector objects the API rendered, and prune what is no longer
//! declared (ADR-0086, #1063).
//!
//! The API computes these manifests; this applies them. That split is
//! deliberate: rendering is a pure function, so the API needs no cluster access
//! and keeps its deliberately read-only RBAC even though it is the service that
//! receives internet webhooks. Applying happens here, under the operator's own
//! kubectl credentials -- where cluster-write authority already lived.
//!
//! Pruning is the reason this is not a bare `kubectl apply`. Every object
//! carries a label naming the agent that declared it, so removing a connector
//! from `connectors.yaml` removes its Deployment, Service, and NetworkPolicy on
//! the next deploy. Without that, a deleted connector leaves a pod running with
//! a credential mounted and nothing referencing it -- the kind of leak nobody
//! notices because nothing breaks.

use anyhow::{Context, Result};
use serde_json::Value;

/// Marks every object this command owns, so pruning can find them again.
pub const OWNER_LABEL: &str = "curie.dev/connector-owner";

/// The label value for an agent's connector objects.
pub fn owner_value(agent_name: &str) -> String {
    agent_name.to_string()
}

/// Stamp the owner label onto each rendered object.
///
/// Applied here rather than in the renderer because ownership is a property of
/// *this deploy*, not of the declaration -- the same bundle deployed as two
/// agents must not have one prune the other's objects.
pub fn label_objects(manifests: &[Value], agent_name: &str) -> Vec<Value> {
    manifests
        .iter()
        .cloned()
        .map(|mut obj| {
            if let Some(meta) = obj.get_mut("metadata").and_then(|m| m.as_object_mut()) {
                let labels = meta
                    .entry("labels")
                    .or_insert_with(|| Value::Object(Default::default()));
                if let Some(map) = labels.as_object_mut() {
                    map.insert(
                        OWNER_LABEL.to_string(),
                        Value::String(owner_value(agent_name)),
                    );
                }
            }
            obj
        })
        .collect()
}

/// `kubectl apply` argv for a JSON document supplied on stdin.
///
/// kubectl accepts JSON wherever it accepts YAML, so the manifests travel from
/// the API as JSON and never need a YAML serializer on this side.
pub fn apply_args(namespace: &str) -> Vec<String> {
    vec![
        "kubectl".into(),
        "-n".into(),
        namespace.into(),
        "apply".into(),
        "-f".into(),
        "-".into(),
    ]
}

/// `kubectl delete` argv for owned objects of `kind` that are no longer declared.
///
/// Scoped by the owner label AND by name, so it can only ever remove objects
/// this command created for this agent. A prune that selected on the label
/// alone would delete a concurrently-deploying agent's objects.
pub fn prune_args(namespace: &str, agent_name: &str, keep: &[String]) -> Vec<String> {
    let mut args: Vec<String> = vec![
        "kubectl".into(),
        "-n".into(),
        namespace.into(),
        "delete".into(),
        // `secret` is in here deliberately. Dropping a connector from
        // connectors.yaml must take its credential with it -- a Secret left
        // behind is a live token nothing references and nobody notices.
        "deployment,service,networkpolicy,secret".into(),
        "-l".into(),
        format!("{}={}", OWNER_LABEL, owner_value(agent_name)),
        "--ignore-not-found".into(),
    ];
    // `kubectl delete -l` has no "except these" flag, so exclusion rides as a
    // field selector on name. It MUST be one comma-separated flag: kubectl
    // takes the last --field-selector and silently discards earlier ones, so
    // repeating the flag would exclude only the final object and delete the
    // rest -- pruning away the connectors just applied.
    if !keep.is_empty() {
        let excluded: Vec<String> = keep.iter().map(|n| format!("metadata.name!={n}")).collect();
        args.push(format!("--field-selector={}", excluded.join(",")));
    }
    args
}

/// Object names in a rendered set, used to build the prune exclusion.
pub fn object_names(manifests: &[Value]) -> Vec<String> {
    manifests
        .iter()
        .filter_map(|o| {
            o.get("metadata")
                .and_then(|m| m.get("name"))
                .and_then(|n| n.as_str())
                .map(str::to_string)
        })
        .collect()
}

/// The Secret name and credential keys the rendered Deployments reference.
///
/// Read back off the manifests rather than re-derived from the bundle: the API
/// decides the Secret's name and which keys a connector needs, so re-deriving
/// here would be a second copy of that rule, free to drift from the one that
/// actually rendered the `secretKeyRef`.
pub fn referenced_secrets(manifests: &[Value]) -> Option<(String, Vec<String>)> {
    let mut name: Option<String> = None;
    let mut keys: Vec<String> = Vec::new();
    for obj in manifests {
        let containers = obj
            .pointer("/spec/template/spec/containers")
            .and_then(|c| c.as_array());
        for c in containers.into_iter().flatten() {
            for env in c
                .get("env")
                .and_then(|e| e.as_array())
                .into_iter()
                .flatten()
            {
                let Some(r) = env.pointer("/valueFrom/secretKeyRef") else {
                    continue;
                };
                if let Some(n) = r.get("name").and_then(|n| n.as_str()) {
                    name.get_or_insert_with(|| n.to_string());
                }
                if let Some(k) = r.get("key").and_then(|k| k.as_str()) {
                    if !keys.iter().any(|e| e == k) {
                        keys.push(k.to_string());
                    }
                }
            }
        }
    }
    name.map(|n| {
        keys.sort();
        (n, keys)
    })
}

/// The Secret carrying resolved connector credentials.
///
/// `stringData` so kubectl does the base64; values reach it over stdin, never
/// argv. `kubectl create secret --from-literal` would put every credential in
/// the process table, where any local user can read it off `ps`.
pub fn render_secret(
    name: &str,
    namespace: &str,
    values: &std::collections::BTreeMap<String, String>,
) -> Value {
    serde_json::json!({
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "namespace": namespace},
        "stringData": values,
    })
}

/// `kubectl` argv reading the release's rendered `app.kubernetes.io/name`.
///
/// Read from the cluster, not re-derived from chart values. That label is what
/// Rail 1's default-deny egress selects on, so taking it from the objects the
/// chart actually rendered means the connector's allow-rule matches by
/// construction -- a second derivation could disagree, and a NetworkPolicy that
/// selects nothing fails silently (ADR-0067).
pub fn app_name_args(namespace: &str, release: &str) -> Vec<String> {
    vec![
        "kubectl".into(),
        "-n".into(),
        namespace.into(),
        "get".into(),
        "deployment".into(),
        "-l".into(),
        format!("app.kubernetes.io/instance={release}"),
        "-o".into(),
        r"jsonpath={.items[0].metadata.labels.app\.kubernetes\.io/name}".into(),
    ]
}

/// Serialize a manifest list as a single JSON `List` for one apply invocation.
pub fn as_list_document(manifests: &[Value]) -> Result<String> {
    let doc = serde_json::json!({
        "apiVersion": "v1",
        "kind": "List",
        "items": manifests,
    });
    serde_json::to_string(&doc).context("serializing connector manifests")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn objs() -> Vec<Value> {
        vec![
            json!({"apiVersion":"v1","kind":"Service","metadata":{"name":"r-mcp-g"}}),
            json!({"apiVersion":"apps/v1","kind":"Deployment",
                   "metadata":{"name":"r-mcp-g","labels":{"existing":"kept"}}}),
        ]
    }

    #[test]
    fn owner_label_is_stamped_without_dropping_existing_labels() {
        let out = label_objects(&objs(), "sre-bot");
        let dep = &out[1]["metadata"]["labels"];
        assert_eq!(dep[OWNER_LABEL], "sre-bot");
        assert_eq!(dep["existing"], "kept", "renderer labels must survive");
    }

    #[test]
    fn objects_with_no_labels_map_still_get_the_owner() {
        let out = label_objects(&objs(), "sre-bot");
        assert_eq!(out[0]["metadata"]["labels"][OWNER_LABEL], "sre-bot");
    }

    #[test]
    fn prune_is_scoped_to_this_agent_not_all_connectors() {
        // Selecting on the label alone would delete a concurrently-deploying
        // agent's objects -- both are "connector objects".
        let args = prune_args("ns", "sre-bot", &[]);
        assert!(args.contains(&format!("{OWNER_LABEL}=sre-bot")));
        assert!(!args.iter().any(|a| a == OWNER_LABEL));
    }

    #[test]
    fn prune_excludes_every_applied_object_in_one_flag() {
        // kubectl keeps only the LAST --field-selector and silently drops the
        // rest, so repeating the flag would exclude one object and prune the
        // others -- deleting the connectors this deploy just applied.
        let keep = vec!["a".to_string(), "b".to_string()];
        let args = prune_args("ns", "sre-bot", &keep);
        let selectors: Vec<&String> = args
            .iter()
            .filter(|a| a.starts_with("--field-selector"))
            .collect();
        assert_eq!(selectors.len(), 1, "must be a single flag: {args:?}");
        assert_eq!(
            selectors[0],
            "--field-selector=metadata.name!=a,metadata.name!=b"
        );
    }

    #[test]
    fn prune_with_nothing_declared_removes_everything_owned() {
        // The connector was deleted from connectors.yaml: its Deployment,
        // Service, and NetworkPolicy must go, or a pod keeps running with a
        // credential mounted and nothing referencing it.
        let args = prune_args("ns", "sre-bot", &[]);
        assert!(!args.iter().any(|a| a.starts_with("--field-selector")));
        assert!(args.contains(&"deployment,service,networkpolicy,secret".to_string()));
    }

    #[test]
    fn manifests_serialize_as_one_list_document() {
        let doc = as_list_document(&objs()).unwrap();
        let parsed: Value = serde_json::from_str(&doc).unwrap();
        assert_eq!(parsed["kind"], "List");
        assert_eq!(parsed["items"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn prune_removes_the_secret_too_when_a_connector_is_dropped() {
        // A Secret left behind is a live credential nothing references. Deleting
        // the Deployment but keeping its token is the leak this whole prune
        // exists to close, so `secret` must be in the kind list.
        let args = prune_args("ns", "sre-bot", &[]);
        let kinds = args.iter().find(|a| a.contains("deployment")).unwrap();
        assert!(kinds.contains("secret"), "kinds were: {kinds}");
    }

    #[test]
    fn secret_refs_are_read_back_off_the_rendered_manifests() {
        // Re-deriving the Secret's name here would be a second copy of a rule
        // the API already applied, free to drift from the secretKeyRef that was
        // actually rendered -- and a mismatch means the pod never starts.
        let dep = json!({"kind":"Deployment","spec":{"template":{"spec":{"containers":[{
        "env":[
            {"name":"GRAFANA_URL","value":"https://g"},
            {"name":"TOKEN","valueFrom":{"secretKeyRef":{"name":"r-a-connector-secrets","key":"TOKEN"}}},
            {"name":"OTHER","valueFrom":{"secretKeyRef":{"name":"r-a-connector-secrets","key":"OTHER"}}}
        ]}]}}}});
        let (name, keys) = referenced_secrets(&[dep]).unwrap();
        assert_eq!(name, "r-a-connector-secrets");
        assert_eq!(keys, vec!["OTHER".to_string(), "TOKEN".to_string()]);
    }

    #[test]
    fn a_connector_with_no_secrets_needs_no_secret_object() {
        let dep = json!({"kind":"Deployment","spec":{"template":{"spec":{"containers":[{
            "env":[{"name":"GRAFANA_URL","value":"https://g"}]}]}}}});
        assert!(referenced_secrets(&[dep]).is_none());
    }

    #[test]
    fn secret_values_ride_string_data_never_argv() {
        // `kubectl create secret --from-literal` puts every credential in the
        // process table, readable by any local user off `ps`. This travels on
        // stdin instead, so the value appears in no argv at all.
        let mut v = std::collections::BTreeMap::new();
        v.insert("TOKEN".to_string(), "s3cret".to_string());
        let obj = render_secret("r-a-connector-secrets", "ns", &v);
        assert_eq!(obj["stringData"]["TOKEN"], "s3cret");
        assert!(!apply_args("ns").iter().any(|a| a.contains("s3cret")));
    }

    #[test]
    fn the_secret_is_kept_not_pruned_by_the_deploy_that_wrote_it() {
        let mut v = std::collections::BTreeMap::new();
        v.insert("TOKEN".to_string(), "x".to_string());
        let objs = vec![render_secret("r-a-connector-secrets", "ns", &v)];
        let keep = object_names(&label_objects(&objs, "a"));
        let args = prune_args("ns", "a", &keep);
        assert!(args
            .iter()
            .any(|a| a.contains("metadata.name!=r-a-connector-secrets")));
    }

    #[test]
    fn app_name_is_read_from_the_release_not_guessed() {
        // Rail 1's default-deny selects on the label the chart rendered. Taking
        // it from the cluster makes the connector's allow-rule match by
        // construction; a second derivation could disagree, and a NetworkPolicy
        // that selects nothing fails silently.
        let args = app_name_args("ns", "myrel");
        assert!(args.contains(&"app.kubernetes.io/instance=myrel".to_string()));
        assert!(args
            .iter()
            .any(|a| a.contains("app\\.kubernetes\\.io/name")));
    }

    #[test]
    fn apply_reads_from_stdin_so_nothing_touches_disk() {
        let args = apply_args("ns");
        assert_eq!(args.last().unwrap(), "-");
    }
}

// --------------------------------------------------------------------------- #
// Execution
// --------------------------------------------------------------------------- #

/// Run a kubectl argv, optionally writing `stdin` to it, capturing output.
///
/// Separate from `ops::run_capture` because that has no stdin channel, and
/// stdin is the whole point here: the applied document carries credentials, so
/// it must not become a `-f <file>` on disk or a `--from-literal` in argv.
async fn run(argv: &[String], stdin: Option<&str>) -> Result<(bool, String, String)> {
    use tokio::io::AsyncWriteExt;
    let (program, args) = argv.split_first().context("empty command")?;
    let mut cmd = tokio::process::Command::new(program);
    cmd.args(args)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .stdin(if stdin.is_some() {
            std::process::Stdio::piped()
        } else {
            std::process::Stdio::null()
        });
    let mut child = cmd
        .spawn()
        .with_context(|| format!("failed to invoke `{program}`; is it on PATH?"))?;
    if let Some(doc) = stdin {
        let mut pipe = child.stdin.take().context("stdin pipe missing")?;
        pipe.write_all(doc.as_bytes()).await?;
        pipe.shutdown().await?;
    }
    let out = child.wait_with_output().await?;
    Ok((
        out.status.success(),
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
    ))
}

/// What a connector sync did, for the deploy summary.
#[derive(Debug, Default)]
pub struct ConnectorSync {
    pub applied: Vec<String>,
    pub urls: std::collections::BTreeMap<String, String>,
}

/// Discover the release's rendered app name (the chart's nameOverride).
pub async fn discover_app_name(namespace: &str, release: &str) -> Result<String> {
    let (ok, out, err) = run(&app_name_args(namespace, release), None).await?;
    let name = out.trim();
    if !ok || name.is_empty() {
        anyhow::bail!(
            "could not read the app name for release {release} in namespace {namespace}: no \
             Deployment carries app.kubernetes.io/instance={release}. Connectors need it to \
             target the sandbox pods Rail 1 denies by default. Confirm the release is installed \
             with `curie cluster status`.{}",
            if err.trim().is_empty() {
                String::new()
            } else {
                format!(" kubectl said: {}", err.trim())
            }
        );
    }
    Ok(name.to_string())
}

/// Resolve each declared credential: environment first, then the host vault.
///
/// Fails naming every gap at once rather than one per run. A connector that
/// starts without its credential does not fail at apply -- it comes up and
/// returns 401s to the agent, which reads as "the tool is broken".
fn resolve_secret_values(keys: &[String]) -> Result<std::collections::BTreeMap<String, String>> {
    let mut values = std::collections::BTreeMap::new();
    let mut missing = Vec::new();
    for k in keys {
        match std::env::var(k)
            .ok()
            .filter(|v| !v.is_empty())
            .or(crate::secrets::get_value(k)?)
        {
            Some(v) => {
                values.insert(k.clone(), v);
            }
            None => missing.push(k.clone()),
        }
    }
    if !missing.is_empty() {
        return Err(crate::exit::usage(format!(
            "connectors.yaml declares secret(s) with no value available: {}. Export each in the \
             environment, or store it once with `curie secrets set <NAME>`.",
            missing.join(", ")
        )));
    }
    Ok(values)
}

/// Apply an agent's declared connectors, and prune what it no longer declares.
///
/// Called after the bundle is deployed, so the objects exist before the next
/// turn reaches for them.
pub async fn sync(
    manifests: &[Value],
    mcp_entries: &std::collections::BTreeMap<String, Value>,
    namespace: &str,
    agent_name: &str,
) -> Result<ConnectorSync> {
    let ui = crate::ui::ui();
    let mut objects = Vec::new();

    if let Some((secret_name, keys)) = referenced_secrets(manifests) {
        if !keys.is_empty() {
            objects.push(render_secret(
                &secret_name,
                namespace,
                &resolve_secret_values(&keys)?,
            ));
        }
    }
    objects.extend_from_slice(manifests);

    let mut sync = ConnectorSync::default();
    for (name, entry) in mcp_entries {
        if let Some(url) = entry.get("url").and_then(|u| u.as_str()) {
            sync.urls.insert(name.clone(), url.to_string());
        }
    }

    let labelled = label_objects(&objects, agent_name);
    let keep = object_names(&labelled);

    if !labelled.is_empty() {
        let doc = as_list_document(&labelled)?;
        let (ok, _out, err) = run(&apply_args(namespace), Some(&doc)).await?;
        if !ok {
            anyhow::bail!("applying connectors failed: {}", err.trim());
        }
        sync.applied = keep.clone();
        ui.note(&format!(
            "connectors: applied {} object(s) for {agent_name}",
            keep.len()
        ));
    }

    // Runs even with nothing declared -- that is the case where a connector was
    // REMOVED, and the whole reason this is not a bare `kubectl apply`.
    let (ok, _out, err) = run(&prune_args(namespace, agent_name, &keep), None).await?;
    if !ok {
        ui.warn(&format!(
            "connectors: pruning stale objects for {agent_name} failed: {}",
            err.trim()
        ));
    }
    Ok(sync)
}
