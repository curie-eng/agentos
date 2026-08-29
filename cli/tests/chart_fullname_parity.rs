//! The CLI's release-name rule, checked against the chart's own render.
//!
//! The chart is the contract: every Curie-owned object is named
//! `{{ include "curie.fullname" . }}-<component>`
//! (`charts/curie/templates/_helpers.tpl:16-26`). `curie::ops::chart_fullname`
//! is a Rust restatement of that rule, and a restatement can drift from the
//! thing it restates without any unit test noticing -- a unit test can only
//! compare the rule against literals someone typed while reading the chart.
//!
//! So this file renders the chart with `helm template` and compares the CLI's
//! answer to the names helm actually produced. It is the one test family that
//! cannot be satisfied by a tautology.
//!
//! It also pins the LIMIT of the pure rule. `nameOverride` and
//! `fullnameOverride` both move the rendered name somewhere the rule cannot
//! compute, which is precisely why the live path discovers the fullname from
//! the cluster instead of computing it. Those two tests exist so that nobody
//! later reads discovery as redundant machinery and deletes it.
//!
//! **Why a line read rather than a YAML parse.** The render carries genuine
//! duplicate keys inside `spec.template.metadata.labels` (`app.kubernetes.io/name`
//! and `app.kubernetes.io/instance` appear twice on every Deployment and
//! StatefulSet the chart emits). Kubernetes tolerates that; strict YAML
//! deserializers may not, and a parser that rejects those documents would
//! silently drop the worker -- which has no Service -- from the comparison set.
//! Everything this file needs lives in the column-anchored top-level `kind:` and
//! `metadata:` block, so it reads those directly and depends on no YAML crate.

use std::path::PathBuf;
use std::process::Command;

use curie::ops::chart_fullname;

/// A non-default release name that does not contain the chart name, so the
/// chart's `contains` branch does NOT fire and the fullname takes the suffix.
/// This is the shape the reported bug is about.
const RELEASE: &str = "platform";

/// Components whose names the CLI derives from the release name. `dispatcher`
/// renders only when Slack is configured, which the default values do not do.
const COMPONENTS: [&str; 6] = ["api", "ui", "langfuse-web", "valkey", "dispatcher", "worker"];

fn chart() -> PathBuf {
    PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/../charts/curie"))
}

/// helm is absent on plenty of dev machines and its absence is not a
/// regression in the chart. `.github/workflows/helm-ci.yaml` is where this
/// gates for real; skipping here narrows where the check runs locally, never
/// where it blocks. Same posture as `cli/tests/chart_check.rs:363-366`.
fn helm_is_absent() -> bool {
    if Command::new("helm").arg("version").output().is_err() {
        eprintln!("skipping: helm is not on PATH");
        return true;
    }
    false
}

/// `helm template <release> <chart> [--set ...]`.
///
/// helm 3 syntax: the release name is a POSITIONAL argument. helm 2's
/// `--release-name` flag is silently ignored by helm 3, which then renders
/// under the default release name -- a test written that way passes while
/// proving nothing about a non-default release.
fn render(release: &str, sets: &[&str]) -> String {
    let mut command = Command::new("helm");
    command.arg("template").arg(release).arg(chart());
    for expression in sets {
        command.arg("--set").arg(expression);
    }
    let output = command.output().expect("run helm template");
    assert!(
        output.status.success(),
        "helm template {release} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).expect("a helm render is UTF-8")
}

/// One rendered manifest, reduced to what a name check needs.
struct Manifest {
    kind: String,
    name: String,
    labels: Vec<(String, String)>,
}

impl Manifest {
    fn label(&self, key: &str) -> Option<&str> {
        self.labels
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, value)| value.as_str())
    }
}

fn unquote(value: &str) -> String {
    value
        .trim()
        .trim_start_matches('"')
        .trim_end_matches('"')
        .to_string()
}

/// Read the top-level `kind` and `metadata` block out of one rendered document.
///
/// Column-anchored on purpose: `kind:` and `metadata:` at indent 0 belong to
/// the manifest itself, while the same words inside an embedded ConfigMap
/// payload or a pod template are always indented. Matching them anywhere would
/// invent objects the chart never created.
fn parse_manifest(document: &str) -> Option<Manifest> {
    let mut kind: Option<String> = None;
    let mut name: Option<String> = None;
    let mut labels: Vec<(String, String)> = Vec::new();
    let mut in_metadata = false;
    let mut in_labels = false;

    for line in document.lines() {
        let line = line.trim_end();
        if line.is_empty() || line.trim_start().starts_with('#') {
            continue;
        }
        let indent = line.len() - line.trim_start().len();
        match indent {
            0 => {
                in_metadata = line == "metadata:";
                in_labels = false;
                if let Some(value) = line.strip_prefix("kind: ") {
                    kind = Some(unquote(value));
                }
            }
            2 if in_metadata => {
                in_labels = line == "  labels:";
                if let Some(value) = line.strip_prefix("  name: ") {
                    name = Some(unquote(value));
                }
            }
            4 if in_metadata && in_labels => {
                if let Some((key, value)) = line[4..].split_once(": ") {
                    labels.push((key.trim().to_string(), unquote(value)));
                }
            }
            _ => {}
        }
    }

    Some(Manifest {
        kind: kind?,
        name: name?,
        labels,
    })
}

fn manifests(rendered: &str) -> Vec<Manifest> {
    rendered.split("\n---").filter_map(parse_manifest).collect()
}

/// The named objects a release's components are actually reachable through.
/// The worker has no Service, so Deployments are as load-bearing here as
/// Services.
fn workload_names(rendered: &str) -> Vec<String> {
    let names: Vec<String> = manifests(rendered)
        .into_iter()
        .filter(|m| m.kind == "Service" || m.kind == "Deployment")
        .map(|m| m.name)
        .collect();
    assert!(
        names.len() > 5,
        "the render yielded almost no workloads ({names:?}); the read is broken, \
         and every name assertion below would be vacuous"
    );
    names
}

/// The positive control: for every component the chart renders, the CLI's rule
/// must produce the name helm produced.
///
/// The expectation on each side comes from a different place -- helm's own
/// output versus `chart_fullname` -- so this cannot pass by comparing the rule
/// to itself. An implementation that kept the old `{release}-{component}` form
/// would look for `platform-api`, which this render does not contain.
#[test]
fn cli_naming_rule_matches_the_charts_own_render() {
    if helm_is_absent() {
        return;
    }

    let rendered = render(RELEASE, &[]);
    let names = workload_names(&rendered);

    for component in COMPONENTS {
        let suffix = format!("-{component}");
        let candidates: Vec<&String> =
            names.iter().filter(|n| n.ends_with(&suffix)).collect();

        if candidates.is_empty() {
            // Only the dispatcher is allowed to be absent: it renders solely
            // when Slack is configured. Any other component vanishing from the
            // render is a chart change this test should surface, not skip.
            assert_eq!(
                component, "dispatcher",
                "the chart renders no object for `{component}`; either the chart \
                 changed or the manifest read is broken"
            );
            eprintln!(
                "skipping dispatcher: the chart renders it only when Slack is configured"
            );
            continue;
        }

        let expected = chart_fullname(RELEASE).resource(component);
        assert!(
            candidates.iter().any(|name| **name == expected),
            "the CLI would ask for `{expected}`, but the chart rendered {candidates:?}"
        );
    }
}

/// The whole-sweep regression guard, at the render. `curie` contains the chart
/// name, so the chart's `contains` branch fires and every derived name is the
/// unprefixed one the CLI has always used. Every install anyone actually runs
/// locally, in CI, and on the parity ladder is this one.
#[test]
fn the_default_release_renders_the_unprefixed_names() {
    if helm_is_absent() {
        return;
    }

    let rendered = render("curie", &[]);
    let names = workload_names(&rendered);

    assert!(
        names.iter().any(|name| name == "curie-api"),
        "the default release must still render `curie-api`: {names:?}"
    );
    assert_eq!(
        chart_fullname("curie").resource("api"),
        "curie-api",
        "the default release must be a byte-identical no-op"
    );
}

/// The rule's limit, in executable form.
///
/// `nameOverride` and `fullnameOverride` both move the rendered name to
/// `platform-api`, which the pure rule cannot compute from the release name
/// alone -- it has no way to know an override was set. This is exactly why the
/// live path discovers the fullname from the cluster and treats
/// `chart_fullname` as an offline fallback.
///
/// If someone later concludes discovery is redundant machinery and deletes it,
/// this test is the record of why it was there.
#[test]
fn the_pure_rule_is_wrong_under_overrides_which_is_why_discovery_exists() {
    if helm_is_absent() {
        return;
    }

    for override_expression in ["fullnameOverride=platform", "nameOverride=platform"] {
        let rendered = render(RELEASE, &[override_expression]);
        let names = workload_names(&rendered);
        assert!(
            names.iter().any(|name| name == "platform-api"),
            "with --set {override_expression} the chart renders `platform-api`: {names:?}"
        );
    }

    assert_ne!(
        chart_fullname(RELEASE).resource("api"),
        "platform-api",
        "the pure rule cannot see an override, so it must NOT be trusted on a \
         live path; if this ever becomes equal the rule has stopped following \
         the chart's no-override branch"
    );
}

/// The load-bearing premise of live discovery.
///
/// Discovery selects on `app.kubernetes.io/instance` and
/// `app.kubernetes.io/component` (`curie.selectorLabels`,
/// `charts/curie/templates/_helpers.tpl:40-44`). Neither of those two labels
/// reads `nameOverride` or `fullnameOverride`, which is the only reason
/// discovery can find an object whose NAME an override moved.
///
/// If the chart ever stops emitting them, discovery starts silently resolving
/// nothing and falling back to a rule that is wrong on exactly these installs.
/// This test is what says so.
#[test]
fn overrides_preserve_the_discovery_labels() {
    if helm_is_absent() {
        return;
    }

    for override_expression in ["fullnameOverride=platform", "nameOverride=platform"] {
        let rendered = render(RELEASE, &[override_expression]);
        let service = manifests(&rendered)
            .into_iter()
            .find(|m| m.kind == "Service" && m.name == "platform-api")
            .unwrap_or_else(|| {
                panic!("--set {override_expression} must render a Service named platform-api")
            });

        assert_eq!(
            service.label("app.kubernetes.io/instance"),
            Some(RELEASE),
            "the instance label is how discovery finds the release under \
             --set {override_expression}"
        );
        assert_eq!(
            service.label("app.kubernetes.io/component"),
            Some("api"),
            "the component label is how discovery picks the api Service under \
             --set {override_expression}"
        );
    }
}
