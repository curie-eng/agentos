//! #2375 binary regression for namespace adoption and failed-install cleanup.
//!
//! The fake executables model Kubernetes at the command/API boundary while the
//! test drives the real `curie` binary. Namespace replies mirror the documented
//! Namespace metadata shape (`labels`, `uid`, and `resourceVersion`), and an
//! ignored missing object is exit 0 with empty stdout:
//! https://kubernetes.io/docs/reference/using-api/api-concepts/
//! Hook Jobs use Helm's documented `helm.sh/hook` annotation:
//! https://helm.sh/docs/topics/charts_hooks/

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::{json, Value};

const NS: &str = "agent-ns";
const RELEASE: &str = "prod-release";

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_curie")
}

fn chart() -> &'static str {
    concat!(env!("CARGO_MANIFEST_DIR"), "/../charts/curie")
}

fn write_exec(dir: &Path, name: &str, body: &str) {
    let path = dir.join(name);
    fs::write(&path, body).expect("write fake executable");
    let mut permissions = fs::metadata(&path).expect("stat fake").permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(path, permissions).expect("chmod fake executable");
}

const FAKE_CLUSTER: &str = r###"#!/usr/bin/env python3
import json, os, pathlib, sys

state_path = pathlib.Path(os.environ["CURIE_TEST_CLUSTER_STATE"])
log_path = pathlib.Path(os.environ["CURIE_TEST_CLUSTER_LOG"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with log_path.open("a") as log:
    log.write(pathlib.Path(sys.argv[0]).name + " " + " ".join(args) + "\n")

def save():
    state_path.write_text(json.dumps(state))

def fail(message, code=1):
    print(message, file=sys.stderr)
    save()
    raise SystemExit(code)

def option(*names):
    for index, arg in enumerate(args):
        for name in names:
            if arg == name and index + 1 < len(args):
                return args[index + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None

def ns_json(name, record):
    return {"apiVersion":"v1", "kind":"Namespace", "metadata":{
        "name":name, "labels":record.get("labels", {}), "uid":record["uid"],
        "resourceVersion":record["resourceVersion"]}}

def matches(labels, selector):
    if not selector:
        return True
    for term in selector.split(","):
        key, _, value = term.partition("=")
        if not value or labels.get(key) != value:
            return False
    return True

def guarded(payload, record):
    def find(value, key, expected):
        if isinstance(value, dict):
            if value.get(key) == expected:
                return True
            if str(value.get("path", "")).endswith("/" + key) and value.get("value") == expected:
                return True
            return any(find(child, key, expected) for child in value.values())
        if isinstance(value, list):
            return any(find(child, key, expected) for child in value)
        return False
    return find(payload, "uid", record["uid"]) and find(
        payload, "resourceVersion", record["resourceVersion"])

def desired_labels(payload):
    rendered = json.dumps(payload)
    return ("curietech.ai" in rendered and "created-by" in rendered
            and RELEASE in rendered and "created-in" in rendered and NS in rendered)

program = pathlib.Path(sys.argv[0]).name
if program == "helm":
    if args[:2] == ["get", "values"]:
        fail("Error: release: not found")
    if args and args[0] == "template":
        shown = option("--show-only") or "template"
        fail(f"Error: could not find template {shown} in chart")
    if args[:2] == ["upgrade", "--install"]:
        state["helm_upgrades"] += 1
        namespaces = state["namespaces"]
        if NS not in namespaces:
            namespaces[NS] = {"labels":{}, "uid":"uid-agent-ns",
                              "resourceVersion":"17", "objects":state["defaults"]}
        state["labels_at_upgrade"] = namespaces[NS].get("labels", {}).copy()
        state["release_exists"] = True
        state["jobs"].setdefault(NS, []).append({"apiVersion":"batch/v1", "kind":"Job",
            "metadata":{"name":"failed-hook", "namespace":NS,
                "labels":{"app.kubernetes.io/instance":RELEASE,
                          "app.kubernetes.io/managed-by":"Helm"},
                "annotations":{"helm.sh/hook":"pre-install"}}})
        fail("Error: INSTALLATION FAILED: pre-install hook failed: BackoffLimitExceeded")
    if args and args[0] == "status":
        print('{"version":1,"info":{"status":"failed"},"hooks":[]}')
        raise SystemExit(0)
    if args[:2] == ["get", "manifest"]:
        print('{"apiVersion":"apps/v1","kind":"Deployment","metadata":{"name":"acme-probe"}}')
        raise SystemExit(0)
    if args and args[0] == "uninstall":
        if state["release_exists"]:
            state["release_exists"] = False
            save(); print(f'release "{RELEASE}" uninstalled'); raise SystemExit(0)
        fail(f'Error: uninstall: Release not loaded: {RELEASE}: release: not found')
    fail("unexpected helm invocation: " + " ".join(args), 64)

namespaces = state["namespaces"]
if args[:2] in (["get", "namespace"], ["get", "namespaces"]):
    name = args[2]
    record = namespaces.get(name)
    if record is None:
        if "--ignore-not-found" in args:
            raise SystemExit(0)
        fail(f'Error from server (NotFound): namespaces "{name}" not found')
    print(json.dumps(ns_json(name, record)))
    raise SystemExit(0)

if args and args[0] == "api-resources":
    if state.get("inventory_error"):
        fail("Error from server (Forbidden): inventory forbidden")
    print("\n".join(state["resources"]))
    state["discovery_calls"] += 1; save(); raise SystemExit(0)

if args[:2] in (["get", "deployment"], ["get", "priorityclass"]):
    raise SystemExit(0)

if args and args[0] == "get" and len(args) >= 2:
    resource = args[1]
    namespace = option("-n", "--namespace") or NS
    record = namespaces.get(namespace)
    if record is None:
        fail(f'Error from server (NotFound): namespaces "{namespace}" not found')
    selector = option("-l", "--selector")
    if resource in ("jobs", "job", "jobs.batch"):
        items = [item for item in state["jobs"].get(namespace, [])
                 if matches(item.get("metadata", {}).get("labels", {}), selector)]
    else:
        items = record.get("objects", {}).get(resource, [])
        state["inventory_seen"].append(resource)
    save(); print(json.dumps({"apiVersion":"v1", "kind":"List", "items":items}))
    raise SystemExit(0)

if args[:2] == ["create", "-f"] and option("-f") == "-":
    payload = json.load(sys.stdin)
    metadata = payload.get("metadata", {})
    name = metadata.get("name")
    if payload.get("kind") != "Namespace" or name in namespaces:
        fail("Error from server (AlreadyExists): namespace already exists")
    labels = metadata.get("labels", {})
    if labels.get("curietech.ai/created-by") != RELEASE or labels.get("curietech.ai/created-in") != NS:
        fail("namespace create was not atomically pair-labelled")
    namespaces[name] = {"labels":labels, "uid":"uid-agent-ns",
                        "resourceVersion":"17", "objects":state["defaults"]}
    state["created_atomically"] = True
    save(); print(f'namespace/{name} created'); raise SystemExit(0)

if args and args[0] in ("patch", "replace") and "namespace" in args:
    name = args[args.index("namespace") + 1] if "namespace" in args else NS
    record = namespaces[name]
    raw = option("-p", "--patch")
    payload = json.loads(raw) if raw else json.load(sys.stdin)
    if not guarded(payload, record) or not desired_labels(payload):
        fail("adoption omitted the namespace uid/resourceVersion ownership precondition")
    if state.get("version_conflict"):
        fail(f'Error from server (Conflict): Operation cannot be fulfilled on namespaces "{name}": the object has been modified')
    record["labels"] = {"curietech.ai/created-by":RELEASE, "curietech.ai/created-in":NS}
    record["resourceVersion"] = str(int(record["resourceVersion"]) + 1)
    state["adoption_guarded"] = True
    save(); print(f'namespace/{name} patched'); raise SystemExit(0)

if args[:2] == ["delete", "namespace"]:
    selector = option("-l", "--selector")
    removed = []
    for name, record in list(namespaces.items()):
        if matches(record.get("labels", {}), selector):
            removed.append(name); del namespaces[name]; state["jobs"].pop(name, None)
    save()
    for name in removed: print(f'namespace "{name}" deleted')
    raise SystemExit(0)

if args and args[0] == "delete" and len(args) > 2 and args[1] in ("job", "jobs", "jobs.batch"):
    namespace = option("-n", "--namespace") or NS
    names = [arg for arg in args[2:] if not arg.startswith("-") and arg != namespace]
    kept = []
    for job in state["jobs"].get(namespace, []):
        if job["metadata"]["name"] in names:
            print(f'job.batch "{job["metadata"]["name"]}" deleted')
        else: kept.append(job)
    state["jobs"][namespace] = kept; save(); raise SystemExit(0)

fail("unexpected kubectl invocation: " + " ".join(args), 64)
"###;

struct Fixture {
    _temp: tempfile::TempDir,
    bin_dir: PathBuf,
    state: PathBuf,
    log: PathBuf,
}

impl Fixture {
    fn new(namespaces: Value, jobs: Value, inventory_error: bool, version_conflict: bool) -> Self {
        let temp = tempfile::tempdir().expect("tempdir");
        let bin_dir = temp.path().join("bin");
        fs::create_dir(&bin_dir).expect("create bin dir");
        write_exec(&bin_dir, "helm", FAKE_CLUSTER);
        write_exec(&bin_dir, "kubectl", FAKE_CLUSTER);
        let state = temp.path().join("state.json");
        let log = temp.path().join("commands.log");
        fs::write(&log, "").expect("write log");
        fs::write(&state, serde_json::to_vec(&json!({
            "namespaces": namespaces, "jobs": jobs, "release_exists": false,
            "helm_upgrades": 0, "labels_at_upgrade": {}, "created_atomically": false,
            "adoption_guarded": false, "inventory_error": inventory_error,
            "version_conflict": version_conflict, "discovery_calls": 0,
            "inventory_seen": [],
            "resources": ["serviceaccounts", "configmaps", "secrets", "events", "jobs.batch", "deployments.apps"],
            "defaults": Self::default_furniture()
        })).unwrap()).expect("write state");
        Self {
            _temp: temp,
            bin_dir,
            state,
            log,
        }
    }

    fn default_furniture() -> Value {
        json!({
            "serviceaccounts": [{"apiVersion":"v1", "kind":"ServiceAccount",
                "metadata":{"name":"default", "namespace":NS}}],
            "configmaps": [{"apiVersion":"v1", "kind":"ConfigMap",
                "metadata":{"name":"kube-root-ca.crt", "namespace":NS},
                "data":{"ca.crt":"PLACEHOLDER CERTIFICATE"}}],
            "secrets": [], "events": [], "jobs.batch": [], "deployments.apps": []
        })
    }

    fn namespace(labels: Value, objects: Value) -> Value {
        json!({"labels":labels, "uid":"uid-agent-ns", "resourceVersion":"17", "objects":objects})
    }

    fn command(&self) -> Command {
        let mut paths = vec![self.bin_dir.clone()];
        paths.extend(std::env::split_paths(
            &std::env::var_os("PATH").unwrap_or_default(),
        ));
        let mut command = Command::new(bin());
        command
            .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/.."))
            .env("PATH", std::env::join_paths(paths).unwrap())
            .env("CURIE_TEST_CLUSTER_STATE", &self.state)
            .env("CURIE_TEST_CLUSTER_LOG", &self.log)
            .env("CURIE_CONFIG_DIR", self._temp.path().join("config"))
            .env("CI", "1")
            .env("TERM", "dumb")
            .env("NO_COLOR", "1")
            .env_remove("CURIE_CREDENTIALS")
            .env_remove("CURIE_MODEL_CREDENTIALS")
            .env_remove("CURIE_GITHUB_TOKEN")
            .env_remove("CURIE_MODEL");
        command
    }

    fn up(&self) -> Output {
        self.command()
            .args([
                "--color",
                "never",
                "cluster",
                "up",
                "--chart",
                chart(),
                "--namespace",
                NS,
                "--release",
                RELEASE,
                "--dev",
                "--no-expose",
                "--fake-model",
                "--set",
                "agentSandbox.controller.deploy=false",
                "--set",
                "security.gvisor.mode=off",
            ])
            .output()
            .expect("run cluster up")
    }

    fn down(&self) -> Output {
        self.command()
            .args([
                "--color",
                "never",
                "cluster",
                "down",
                "--namespace",
                NS,
                "--release",
                RELEASE,
                "--yes",
            ])
            .output()
            .expect("run cluster down")
    }

    fn state(&self) -> Value {
        serde_json::from_slice(&fs::read(&self.state).expect("read state")).expect("parse state")
    }
}

fn shown(output: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}

fn assert_blocked_before_helm(fixture: &Fixture, output: &Output, needle: &str) {
    let state = fixture.state();
    assert!(
        !output.status.success(),
        "unsafe adoption unexpectedly succeeded: {}",
        shown(output)
    );
    assert_eq!(
        state["helm_upgrades"],
        0,
        "Helm mutated before the namespace guard: {}",
        shown(output)
    );
    assert!(
        shown(output)
            .to_lowercase()
            .contains(&needle.to_lowercase()),
        "guard error omitted {needle:?}: {}",
        shown(output)
    );
}

fn job(name: &str, labels: Value, hook: bool) -> Value {
    let mut metadata = json!({"name":name, "namespace":NS, "labels":labels});
    if hook {
        metadata["annotations"] = json!({"helm.sh/hook":"pre-install"});
    }
    json!({"apiVersion":"batch/v1", "kind":"Job", "metadata":metadata})
}

#[test]
fn failed_install_cleanup_and_empty_namespace_adoption() {
    let empty = Fixture::default_furniture();

    // A newly created namespace must carry the full ownership pair before Helm;
    // a failed hook install must still be removable by a later `cluster down`.
    let fresh = Fixture::new(json!({}), json!({}), false, false);
    let failed = fresh.up();
    assert!(!failed.status.success(), "the fake Helm hook must fail");
    let state = fresh.state();
    assert_eq!(
        state["created_atomically"], true,
        "namespace was not created atomically"
    );
    assert_eq!(
        state["labels_at_upgrade"],
        json!({
            "curietech.ai/created-by": RELEASE, "curietech.ai/created-in": NS
        }),
        "Helm ran before ownership was established"
    );
    let down = fresh.down();
    assert!(
        down.status.success(),
        "failed install teardown failed: {}",
        shown(&down)
    );
    assert!(
        fresh.state()["namespaces"].get(NS).is_none(),
        "failed-install namespace survived down"
    );

    // An exact, empty preexisting namespace is adopted with identity/version
    // preconditions, and therefore becomes sweepable too.
    let adopt = Fixture::new(
        json!({NS: Fixture::namespace(json!({}), empty.clone())}),
        json!({}),
        false,
        false,
    );
    let failed = adopt.up();
    assert!(
        !failed.status.success(),
        "the fake Helm hook must fail after adoption"
    );
    let state = adopt.state();
    assert_eq!(
        state["adoption_guarded"], true,
        "adoption lacked uid/resourceVersion guards"
    );
    assert_eq!(
        state["discovery_calls"], 1,
        "adoption did not discover the live namespaced API inventory"
    );
    assert_eq!(
        state["inventory_seen"].as_array().unwrap().len(),
        state["resources"].as_array().unwrap().len(),
        "adoption skipped an API resource: {state}"
    );
    assert!(adopt.down().status.success());
    assert!(
        adopt.state()["namespaces"].get(NS).is_none(),
        "adopted namespace survived down"
    );

    // A retained/shared namespace is never swept, but a target-release Helm
    // hook is object-scoped cleanup. Non-hooks and insufficiently labelled hooks stay.
    let target =
        json!({"app.kubernetes.io/instance":RELEASE, "app.kubernetes.io/managed-by":"Helm"});
    let shared_jobs = json!({NS: [
        job("owned-hook", target.clone(), true), job("owned-non-hook", target, false),
        job("spoofed-hook", json!({"app.kubernetes.io/instance":RELEASE}), true),
        job("foreign-hook", json!({"app.kubernetes.io/instance":"other-release",
            "app.kubernetes.io/managed-by":"Helm"}), true)
    ]});
    let shared = Fixture::new(
        json!({NS: Fixture::namespace(json!({}), json!({
            "serviceaccounts":[], "configmaps":[], "secrets":[{"kind":"Secret","metadata":{"name":"foreign"}}],
            "events":[], "jobs.batch":[], "deployments.apps":[]
        }))}),
        shared_jobs,
        false,
        false,
    );
    let down = shared.down();
    assert!(
        down.status.success(),
        "hook cleanup failed: {}",
        shown(&down)
    );
    let state = shared.state();
    assert!(
        state["namespaces"].get(NS).is_some(),
        "shared namespace was deleted"
    );
    let names: Vec<_> = state["jobs"][NS]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| item["metadata"]["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["owned-non-hook", "spoofed-hook", "foreign-hook"]);

    // Foreign content and modified default furniture are both non-empty.
    let foreign = Fixture::new(
        json!({NS: Fixture::namespace(json!({}), json!({
            "serviceaccounts":[], "configmaps":[], "secrets":[{"apiVersion":"v1", "kind":"Secret",
                "metadata":{"name":"foreign-secret", "namespace":NS}}], "events":[], "jobs.batch":[], "deployments.apps":[]
        }))}),
        json!({}),
        false,
        false,
    );
    assert_blocked_before_helm(&foreign, &foreign.up(), "Secret");

    let mut custom = empty.clone();
    custom["serviceaccounts"][0]["imagePullSecrets"] = json!([{"name":"private-registry"}]);
    let custom_default = Fixture::new(
        json!({NS: Fixture::namespace(json!({}), custom)}),
        json!({}),
        false,
        false,
    );
    assert_blocked_before_helm(&custom_default, &custom_default.up(), "ServiceAccount");

    // Incomplete/foreign ownership, unreadable inventory, and a concurrent
    // namespace update all fail closed before Helm. Same release in another
    // install namespace remains out of reach.
    let partial = Fixture::new(
        json!({NS: Fixture::namespace(json!({
        "curietech.ai/created-by": RELEASE
    }), empty.clone())}),
        json!({}),
        false,
        false,
    );
    assert_blocked_before_helm(&partial, &partial.up(), "created-in");

    let inventory = Fixture::new(
        json!({NS: Fixture::namespace(json!({}), empty.clone())}),
        json!({}),
        true,
        false,
    );
    assert_blocked_before_helm(&inventory, &inventory.up(), "inventory forbidden");

    let conflict = Fixture::new(
        json!({NS: Fixture::namespace(json!({}), empty.clone())}),
        json!({}),
        false,
        true,
    );
    assert_blocked_before_helm(&conflict, &conflict.up(), "modified");
    assert_eq!(conflict.state()["namespaces"][NS]["labels"], json!({}));

    let other_ns = Fixture::new(
        json!({NS: Fixture::namespace(json!({
        "curietech.ai/created-by": RELEASE, "curietech.ai/created-in":"other-ns"
    }), empty)}),
        json!({}),
        false,
        false,
    );
    assert_blocked_before_helm(&other_ns, &other_ns.up(), "other-ns");
    let down = other_ns.down();
    assert!(
        down.status.success(),
        "foreign-owner down should be resumably complete: {}",
        shown(&down)
    );
    assert!(
        other_ns.state()["namespaces"].get(NS).is_some(),
        "same release in another namespace was swept"
    );
}
