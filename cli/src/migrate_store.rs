//! `curie cluster migrate-store`: move bundle objects across a store rename.
//!
//! Chart 0.6.0 renamed the in-cluster object store from `minio` to `rustfs`.
//! Helm does a FULL upgrade, so the old StatefulSet is deleted -- and the
//! bundles with it. That store is on the hot path of every turn: each Slack
//! thread creates a sandbox whose bundle-fetch init container downloads the
//! bundle before the runner starts, so an empty store stops the bot answering
//! rather than merely breaking rollbacks (issue #1324).
//!
//! The two stores cannot coexist -- one chart renders one of them -- so the
//! objects have to live somewhere across the upgrade. That somewhere is a
//! **staging pod**: a plain `kubectl run` pod on the image the chart already
//! uses for bundle-fetch, holding the objects in its `emptyDir`. Helm does not
//! own that pod, so the upgrade does not touch it.
//!
//! Two properties are load-bearing:
//!
//! - **No S3 client in the CLI.** Every transfer is `kubectl exec` into the
//!   staging pod running the same `aws` CLI the chart's bundle-fetch uses. This
//!   keeps the verb a thin wrapper over `kubectl`, matching every other
//!   operator verb, and adds no signing code or SDK dependency.
//! - **Credentials never reach argv.** Store passwords are read inside the pod
//!   from the mounted release Secret, so they cannot land in `ps`, shell
//!   history, or a printed plan.

use anyhow::{bail, Result};

use crate::ops::{plain, CommonOpts, OpsCommand};

/// Which in-cluster object store a release runs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StoreKind {
    Minio,
    Rustfs,
}

impl StoreKind {
    /// The StatefulSet suffix, and the `<release>-<suffix>` Service name.
    pub fn suffix(self) -> &'static str {
        match self {
            StoreKind::Minio => "minio",
            StoreKind::Rustfs => "rustfs",
        }
    }

    /// The access key the chart configures for this store.
    pub fn access_key(self) -> &'static str {
        match self {
            StoreKind::Minio => "minio",
            StoreKind::Rustfs => "rustfs",
        }
    }

    /// The key in the release Secret holding this store's password.
    pub fn secret_key(self) -> &'static str {
        match self {
            StoreKind::Minio => "minioRootPassword",
            StoreKind::Rustfs => "rustfsSecretKey",
        }
    }
}

/// Identify the store among a release's StatefulSet COMPONENTS.
///
/// Component, never resource name: names embed the chart fullname, so
/// `nameOverride` renames every one of them. `ops::live_stateful_components`
/// documents why the guard learned this the hard way; the same reasoning
/// applies here, and getting it wrong would point the copy at a Service that
/// does not exist.
pub fn detect_store(components: &[String]) -> Option<StoreKind> {
    [StoreKind::Minio, StoreKind::Rustfs]
        .into_iter()
        .find(|kind| components.iter().any(|c| c == kind.suffix()))
}

/// What a migration would do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MigrationPlan {
    /// Source and target are the same store: nothing to move.
    NotNeeded { store: StoreKind },
    /// The upgrade renames the store; objects must be carried across.
    Migrate { from: StoreKind, to: StoreKind },
}

/// Compare the live store against the one the target chart renders.
///
/// Pure, so the decision this verb turns on is testable with no cluster. A
/// missing store on either side is an error rather than a guess: migrating from
/// or to something we cannot name would move data into the void.
pub fn plan(live: &[String], rendered: &[String]) -> Result<MigrationPlan> {
    let from = detect_store(live).ok_or_else(|| {
        anyhow::anyhow!(
            "no known object store StatefulSet found in the live release. Looked for \
             components `minio` and `rustfs` among: {}",
            if live.is_empty() {
                "(none)".to_string()
            } else {
                live.join(", ")
            }
        )
    })?;
    let to = detect_store(rendered).ok_or_else(|| {
        anyhow::anyhow!(
            "the target chart renders no known object store. Looked for \
             components `minio` and `rustfs` among: {}",
            if rendered.is_empty() {
                "(none)".to_string()
            } else {
                rendered.join(", ")
            }
        )
    })?;
    if from == to {
        return Ok(MigrationPlan::NotNeeded { store: from });
    }
    Ok(MigrationPlan::Migrate { from, to })
}

/// Name of the staging pod. Fixed per release so a re-run adopts the pod a
/// previous interrupted run left behind rather than stranding its data.
pub fn staging_pod(release: &str) -> String {
    format!("{release}-store-migration")
}

/// `kubectl run` the staging pod: the chart's own aws-cli image, idle, with the
/// release Secret mounted so passwords are read in-pod and never pass through
/// argv.
pub fn create_staging_pod_cmd(o: &CommonOpts, image: &str, secret_name: &str) -> OpsCommand {
    let overrides = serde_json::json!({
        "spec": {
            "containers": [{
                "name": "stage",
                "image": image,
                "command": ["sleep", "86400"],
                "volumeMounts": [
                    {"name": "stage", "mountPath": "/stage"},
                    {"name": "release-secret", "mountPath": "/secret", "readOnly": true}
                ]
            }],
            "volumes": [
                {"name": "stage", "emptyDir": {}},
                {"name": "release-secret", "secret": {"secretName": secret_name}}
            ]
        }
    });
    OpsCommand::new(
        "kubectl",
        vec![
            plain("run"),
            plain(staging_pod(&o.release)),
            plain("-n"),
            plain(&o.namespace),
            plain("--image"),
            plain(image),
            plain("--restart=Never"),
            plain("--overrides"),
            plain(overrides.to_string()),
            plain("--command"),
            plain("--"),
            plain("sleep"),
            plain("86400"),
        ],
    )
}

/// Wait for a Secret key to appear in the staging pod's mount.
///
/// The pod mounts the release Secret when it is CREATED, before the upgrade
/// exists. A mounted Secret is refreshed by kubelet on a sync period, not
/// instantly, so the key the NEW store uses is typically absent for up to a
/// minute after the upgrade adds it. The first live run of this verb failed
/// exactly there: `cat: /secret/rustfsSecretKey: No such file or directory`,
/// with the staged copy intact.
///
/// Waiting rather than re-creating the pod: its `emptyDir` holds the only copy
/// of the objects at that moment, so recreating it would destroy them.
fn await_secret_key(key: &str) -> String {
    format!(
        "for i in $(seq 1 90); do [ -s /secret/{key} ] && break; sleep 2; done; \
         [ -s /secret/{key} ] || {{ echo \"secret key {key} never appeared in the \
         staging pod mount; the staged copy is intact -- retry the import\" >&2; exit 1; }}; "
    )
}

/// The in-pod shell for one leg of the copy.
///
/// `direction` is the `aws s3 sync` argument pair. The password is read from
/// the mounted Secret inside the pod, so it never appears here.
fn sync_script(store: StoreKind, endpoint: &str, from: &str, to: &str) -> String {
    format!(
        "set -e; {wait}\
         export AWS_DEFAULT_REGION=us-east-1; \
         aws configure set default.s3.addressing_style path; \
         export AWS_ACCESS_KEY_ID={access}; \
         export AWS_SECRET_ACCESS_KEY=$(cat /secret/{secret}); \
         aws s3 sync {from} {to} --endpoint-url {endpoint} --only-show-errors; \
         echo synced",
        wait = await_secret_key(store.secret_key()),
        access = store.access_key(),
        secret = store.secret_key(),
    )
}

/// The store's in-cluster S3 endpoint, from the Service the chart actually
/// created. Looked up by component rather than constructed from the release
/// name, for the `nameOverride` reason above.
///
/// The component lives in the Service's `spec.selector`, NOT in its
/// `metadata.labels` -- the chart's `selectorLabels` helper puts it there, and
/// the object-level labels carry only name/instance/version. A `-l` label
/// selector therefore matches nothing, which is how the first live run of this
/// verb failed: `array index out of bounds: index 0, length 0`. Filtering on
/// `spec.selector` is what actually finds it.
pub fn store_service_cmd(o: &CommonOpts, store: StoreKind) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("get"),
            plain("svc"),
            plain("-n"),
            plain(&o.namespace),
            plain("-o"),
            plain(format!(
                r#"jsonpath={{.items[?(@.spec.selector.app\.kubernetes\.io/component=="{}")].metadata.name}}"#,
                store.suffix()
            )),
        ],
    )
}

/// Build the endpoint URL once the Service name is known.
pub fn endpoint_for(service: &str, namespace: &str) -> String {
    format!("http://{service}.{namespace}.svc.cluster.local:9000")
}

/// Copy every object out of the live store into the staging pod's volume.
pub fn export_cmd(o: &CommonOpts, from: StoreKind, bucket: &str, endpoint: &str) -> OpsCommand {
    exec_cmd(
        o,
        &sync_script(from, endpoint, &format!("s3://{bucket}"), "/stage"),
    )
}

/// Copy the staged objects into the new store, creating the bucket first.
///
/// `mb` on an existing bucket is a no-op error, so it is tolerated: a re-run
/// after a partial import must not fail on the bucket already being there.
pub fn import_cmd(o: &CommonOpts, to: StoreKind, bucket: &str, endpoint: &str) -> OpsCommand {
    let script = format!(
        "set -e; {wait}\
         export AWS_DEFAULT_REGION=us-east-1; \
         aws configure set default.s3.addressing_style path; \
         export AWS_ACCESS_KEY_ID={access}; \
         export AWS_SECRET_ACCESS_KEY=$(cat /secret/{secret}); \
         aws s3 mb s3://{bucket} --endpoint-url {endpoint} || true; \
         aws s3 sync /stage s3://{bucket} --endpoint-url {endpoint} --only-show-errors; \
         echo synced",
        wait = await_secret_key(to.secret_key()),
        access = to.access_key(),
        secret = to.secret_key(),
    );
    exec_cmd(o, &script)
}

/// Count staged objects, for the before/after comparison.
pub fn staged_count_cmd(o: &CommonOpts) -> OpsCommand {
    exec_cmd(o, "find /stage -type f | wc -l")
}

/// List `<size> <key>` for every object in a store, for a per-object diff.
///
/// Per object rather than a byte total on purpose: a concurrent `git push` can
/// legitimately add an object mid-migration, so counts and totals can differ
/// for a benign reason. Only a per-object comparison separates that from loss.
pub fn store_listing_cmd(
    o: &CommonOpts,
    store: StoreKind,
    bucket: &str,
    endpoint: &str,
) -> OpsCommand {
    let script = format!(
        "{wait}export AWS_DEFAULT_REGION=us-east-1; \
         aws configure set default.s3.addressing_style path; \
         export AWS_ACCESS_KEY_ID={access}; \
         export AWS_SECRET_ACCESS_KEY=$(cat /secret/{secret}); \
         aws s3 ls s3://{bucket} --recursive --endpoint-url {endpoint} \
         | awk '{{print $3, $4}}' | sort",
        wait = await_secret_key(store.secret_key()),
        access = store.access_key(),
        secret = store.secret_key(),
    );
    exec_cmd(o, &script)
}

/// The staged files as `<size> <key>`, comparable with a store listing.
pub fn staged_listing_cmd(o: &CommonOpts) -> OpsCommand {
    exec_cmd(o, "cd /stage && find . -type f -printf '%s %P\\n' | sort")
}

pub fn delete_staging_pod_cmd(o: &CommonOpts) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("delete"),
            plain("pod"),
            plain(staging_pod(&o.release)),
            plain("-n"),
            plain(&o.namespace),
            plain("--ignore-not-found"),
        ],
    )
}

fn exec_cmd(o: &CommonOpts, script: &str) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("exec"),
            plain("-n"),
            plain(&o.namespace),
            plain(staging_pod(&o.release)),
            plain("--"),
            plain("sh"),
            plain("-c"),
            plain(script),
        ],
    )
}

/// Compare two `<size> <key>` listings.
///
/// Returns the keys present in `before` but missing or differently sized in
/// `after`. An object only in `after` is NOT a problem -- that is the
/// concurrent-push case -- so it is reported separately by the caller.
pub fn missing_after(before: &str, after: &str) -> Vec<String> {
    let parse = |s: &str| -> Vec<(String, String)> {
        s.lines()
            .filter_map(|l| {
                let mut it = l.split_whitespace();
                let size = it.next()?;
                let key = it.next()?;
                Some((key.to_string(), size.to_string()))
            })
            .collect()
    };
    let after_pairs = parse(after);
    parse(before)
        .into_iter()
        .filter(|(key, size)| !after_pairs.iter().any(|(k, s)| k == key && s == size))
        .map(|(key, _)| key)
        .collect()
}

/// Objects in `after` that were not in `before` -- benign, but worth naming.
pub fn added_after(before: &str, after: &str) -> Vec<String> {
    let keys = |s: &str| -> Vec<String> {
        s.lines()
            .filter_map(|l| l.split_whitespace().nth(1).map(str::to_string))
            .collect()
    };
    let before_keys = keys(before);
    keys(after)
        .into_iter()
        .filter(|k| !before_keys.contains(k))
        .collect()
}

/// Refuse a plan that cannot be carried out safely.
pub fn ensure_migratable(plan: &MigrationPlan) -> Result<(StoreKind, StoreKind)> {
    match plan {
        MigrationPlan::NotNeeded { store } => bail!(
            "nothing to migrate: the release already runs {} and the target chart \
             renders the same store. A plain `curie apply` or `helm upgrade` is all \
             this upgrade needs.",
            store.suffix()
        ),
        MigrationPlan::Migrate { from, to } => Ok((*from, *to)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn opts() -> CommonOpts {
        CommonOpts {
            namespace: "sre-bot".into(),
            release: "sre-bot".into(),
            dry_run: false,
        }
    }

    #[test]
    fn detects_each_store_by_component() {
        assert_eq!(
            detect_store(&["postgres".into(), "minio".into()]),
            Some(StoreKind::Minio)
        );
        assert_eq!(detect_store(&["rustfs".into()]), Some(StoreKind::Rustfs));
        assert_eq!(detect_store(&["postgres".into()]), None);
    }

    /// The reason detection is component-keyed rather than name-keyed: a
    /// release installed with `nameOverride` renames every resource, so
    /// matching `<release>-minio` would miss the store entirely and the copy
    /// would be pointed at a Service that does not exist.
    #[test]
    fn a_name_override_does_not_hide_the_store() {
        // Components are stable under nameOverride; resource names are not.
        let components = vec!["postgres".to_string(), "minio".to_string()];
        assert_eq!(detect_store(&components), Some(StoreKind::Minio));
    }

    /// The endpoint comes from the Service the chart actually made.
    #[test]
    fn the_endpoint_uses_the_resolved_service_name() {
        assert_eq!(
            endpoint_for("acme-minio", "acme"),
            "http://acme-minio.acme.svc.cluster.local:9000"
        );
    }

    /// The case this verb exists for.
    #[test]
    fn a_renamed_store_plans_a_migration() {
        let live = vec!["minio".into(), "postgres".into()];
        let rendered = vec!["rustfs".into(), "postgres".into()];
        assert_eq!(
            plan(&live, &rendered).unwrap(),
            MigrationPlan::Migrate {
                from: StoreKind::Minio,
                to: StoreKind::Rustfs
            }
        );
    }

    /// An ordinary upgrade must say "nothing to do" rather than shuffling data
    /// through a staging pod for no reason.
    #[test]
    fn an_unchanged_store_needs_no_migration() {
        let live = vec!["rustfs".to_string()];
        let rendered = vec!["rustfs".to_string()];
        let p = plan(&live, &rendered).unwrap();
        assert_eq!(
            p,
            MigrationPlan::NotNeeded {
                store: StoreKind::Rustfs
            }
        );
        let err = format!("{:#}", ensure_migratable(&p).unwrap_err());
        assert!(err.contains("nothing to migrate"), "{err}");
    }

    /// Guessing here would move data into the void.
    #[test]
    fn an_unrecognisable_store_is_an_error_not_a_guess() {
        let err = plan(&[], &["rustfs".to_string()]).unwrap_err();
        assert!(format!("{err:#}").contains("live release"), "{err:#}");
        let err = plan(&["minio".to_string()], &[]).unwrap_err();
        assert!(format!("{err:#}").contains("target chart"), "{err:#}");
    }

    /// The whole point of reading the password in-pod.
    #[test]
    fn no_command_carries_a_credential_in_argv() {
        let o = opts();
        for cmd in [
            export_cmd(&o, StoreKind::Minio, "curie-bundles", "http://s:9000"),
            import_cmd(&o, StoreKind::Rustfs, "curie-bundles", "http://s:9000"),
            store_listing_cmd(&o, StoreKind::Minio, "curie-bundles", "http://s:9000"),
        ] {
            let joined = format!("{:?}", cmd.args);
            assert!(
                joined.contains("/secret/"),
                "the password must be read from the mounted Secret: {joined}"
            );
            for leaked in [
                "minioRootPassword=",
                "rustfsSecretKey=",
                "AWS_SECRET_ACCESS_KEY=x",
            ] {
                assert!(!joined.contains(leaked), "{leaked} in argv: {joined}");
            }
        }
    }

    #[test]
    fn export_and_import_target_the_right_endpoints() {
        let o = opts();
        let ep = endpoint_for("sre-bot-minio", "sre-bot");
        let export = format!(
            "{:?}",
            export_cmd(&o, StoreKind::Minio, "curie-bundles", &ep).args
        );
        assert!(
            export.contains("sre-bot-minio.sre-bot.svc.cluster.local:9000"),
            "{export}"
        );
        assert!(export.contains("s3://curie-bundles /stage"), "{export}");

        let ep = endpoint_for("sre-bot-rustfs", "sre-bot");
        let import = format!(
            "{:?}",
            import_cmd(&o, StoreKind::Rustfs, "curie-bundles", &ep).args
        );
        assert!(
            import.contains("sre-bot-rustfs.sre-bot.svc.cluster.local:9000"),
            "{import}"
        );
        assert!(import.contains("/stage s3://curie-bundles"), "{import}");
    }

    /// A partial import must be resumable, so bucket-already-exists cannot fail.
    #[test]
    fn import_tolerates_an_existing_bucket() {
        let import = format!(
            "{:?}",
            import_cmd(&opts(), StoreKind::Rustfs, "curie-bundles", "http://s:9000").args
        );
        assert!(import.contains("mb s3://curie-bundles"), "{import}");
        assert!(
            import.contains("|| true"),
            "re-run must not fail on mb: {import}"
        );
    }

    /// The staging pod name is stable so an interrupted run is adoptable rather
    /// than orphaning its data under a fresh random name.
    #[test]
    fn the_staging_pod_name_is_deterministic() {
        assert_eq!(staging_pod("sre-bot"), "sre-bot-store-migration");
    }

    /// Found by the first live run. The component is in the Service's
    /// `spec.selector`, not `metadata.labels`, so a `-l` label selector matched
    /// nothing and the run died with `array index out of bounds: index 0,
    /// length 0` before copying anything.
    #[test]
    fn the_service_lookup_filters_on_spec_selector_not_labels() {
        let line = store_service_cmd(&opts(), StoreKind::Minio).display();
        assert!(
            line.contains("spec.selector"),
            "must filter on spec.selector, where the chart puts the component: {line}"
        );
        assert!(
            !line.contains(" -l "),
            "a label selector matches nothing here: {line}"
        );
        assert!(line.contains("minio"), "{line}");
    }

    /// Also found live. The staging pod mounts the release Secret when it is
    /// CREATED -- before the upgrade exists -- and kubelet refreshes a mounted
    /// Secret on a sync period, so the NEW store's key is absent for up to a
    /// minute after the upgrade adds it. The import must wait rather than fail
    /// on `cat: /secret/rustfsSecretKey: No such file or directory`.
    #[test]
    fn the_import_waits_for_the_new_stores_secret_key() {
        let line =
            import_cmd(&opts(), StoreKind::Rustfs, "curie-bundles", "http://s:9000").display();
        assert!(
            line.contains("rustfsSecretKey") && line.contains("seq 1"),
            "import must poll for the key the upgrade adds: {line}"
        );
    }

    #[test]
    fn a_lost_object_is_reported() {
        let before = "100 a.tar\n200 b.tar";
        let after = "100 a.tar";
        assert_eq!(missing_after(before, after), vec!["b.tar"]);
    }

    /// Same key, different size, is loss too -- a truncated copy must not pass.
    #[test]
    fn a_resized_object_counts_as_missing() {
        assert_eq!(missing_after("100 a.tar", "40 a.tar"), vec!["a.tar"]);
    }

    /// The real 22-vs-23 case: a push landed mid-migration. Benign, and must not
    /// read as loss -- but it should still be named.
    #[test]
    fn an_object_added_during_the_migration_is_not_loss() {
        let before = "100 a.tar";
        let after = "100 a.tar\n92160 new.tar";
        assert!(missing_after(before, after).is_empty());
        assert_eq!(added_after(before, after), vec!["new.tar"]);
    }

    #[test]
    fn an_identical_pair_reports_nothing() {
        let both = "100 a.tar\n200 b.tar";
        assert!(missing_after(both, both).is_empty());
        assert!(added_after(both, both).is_empty());
    }
}

// -- execution ----------------------------------------------------------------

/// What a phase did, as one object (the `--json` contract allows exactly one).
#[derive(Debug)]
pub enum MigrateStoreOutput {
    DryRun(crate::ui::DryRunPlan),
    Exported {
        from: String,
        to: String,
        objects: usize,
    },
    Imported {
        store: String,
        objects: usize,
        missing: Vec<String>,
        added: Vec<String>,
        staging_kept: bool,
    },
}

impl crate::ui::CliOutput for MigrateStoreOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            MigrateStoreOutput::DryRun(plan) => {
                <crate::ui::DryRunPlan as crate::ui::CliOutput>::to_json(plan)
            }
            MigrateStoreOutput::Exported { from, to, objects } => serde_json::json!({
                "phase": "export", "from": from, "to": to, "objects": objects,
            }),
            MigrateStoreOutput::Imported {
                store,
                objects,
                missing,
                added,
                staging_kept,
            } => serde_json::json!({
                "phase": "import",
                "store": store,
                "objects": objects,
                "missing": missing,
                "added": added,
                "verified": missing.is_empty(),
                "staging_kept": staging_kept,
            }),
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        match self {
            MigrateStoreOutput::DryRun(plan) => {
                <crate::ui::DryRunPlan as crate::ui::CliOutput>::render(plan, ui)
            }
            MigrateStoreOutput::Exported { from, to, objects } => {
                ui.payload_plain(&format!("staged {objects} object(s) from {from}"));
                ui.note(&format!(
                    "now upgrade the release (the store becomes {to}), then run \
                     `curie cluster migrate-store --phase import`. The staging pod holds \
                     the only copy until then -- do not delete it."
                ));
            }
            MigrateStoreOutput::Imported {
                store,
                objects,
                missing,
                added,
                staging_kept,
            } => {
                ui.payload_plain(&format!("imported {objects} object(s) into {store}"));
                if !added.is_empty() {
                    ui.payload_plain(&format!(
                        "{} object(s) appeared during the migration (a deploy landed \
                         mid-flight); not a loss",
                        added.len()
                    ));
                }
                if missing.is_empty() {
                    ui.payload_plain("verified: every staged object is present at the same size");
                } else {
                    ui.payload_plain(&format!("MISSING {} object(s):", missing.len()));
                    for k in missing {
                        ui.payload_plain(&format!("  {k}"));
                    }
                }
                if *staging_kept {
                    ui.note("staging pod kept; delete it once a real turn has succeeded.");
                }
            }
        }
    }
}

/// Phase one: stage every object while the old store is still up.
pub async fn run_export(o: &CommonOpts, chart: &str, bucket: &str) -> Result<MigrateStoreOutput> {
    let live: Vec<String> = crate::ops::live_stateful_components(o)
        .await?
        .into_iter()
        .map(|(component, _)| component)
        .collect();
    // migrate_store reasons about COMPONENTS only (which store is which); the
    // resource names the guard also needs are irrelevant here.
    let rendered: Vec<String> = crate::ops::chart_stateful_components(chart, o, &[])
        .await?
        .into_iter()
        .map(|(component, _)| component)
        .collect();
    let (from, to) = ensure_migratable(&plan(&live, &rendered)?)?;

    let image = "amazon/aws-cli:2.32.6";
    // Discovered, not computed. `<release>-secrets` is only the chart Secret's
    // name when the release name contains the chart name; a default install
    // renders `<release>-curie-secrets`, and the staging pod would then mount a
    // Secret that does not exist -- failing mid-migration, with the export
    // already taken and the store half moved.
    let secret = crate::ops::release_secret_name_or_default(&o.namespace, &o.release).await;
    if o.dry_run {
        let endpoint = endpoint_for("<store-service>", &o.namespace);
        let cmds = [
            store_service_cmd(o, from),
            delete_staging_pod_cmd(o),
            create_staging_pod_cmd(o, image, &secret),
            export_cmd(o, from, bucket, &endpoint),
            staged_count_cmd(o),
        ];
        return Ok(MigrateStoreOutput::DryRun(crate::ui::DryRunPlan {
            lines: cmds.iter().map(|c| c.display()).collect(),
        }));
    }

    let (ok, service, err) = crate::ops::run_capture(&store_service_cmd(o, from)).await?;
    let service = service.trim().to_string();
    if !ok || service.is_empty() {
        bail!(
            "could not find the {} Service to copy from: {err}",
            from.suffix()
        );
    }
    let endpoint = endpoint_for(&service, &o.namespace);

    crate::ops::run_capture(&delete_staging_pod_cmd(o))
        .await
        .ok();
    let (ok, _, err) = crate::ops::run_capture(&create_staging_pod_cmd(o, image, &secret)).await?;
    if !ok {
        bail!("could not create the staging pod: {err}");
    }
    let (ok, _, err) = crate::ops::run_capture(&wait_ready_cmd(o)).await?;
    if !ok {
        bail!("the staging pod never became ready: {err}");
    }
    let (ok, _, err) = crate::ops::run_capture(&export_cmd(o, from, bucket, &endpoint)).await?;
    if !ok {
        bail!("the export copy failed; the old store is untouched: {err}");
    }
    let (_, count, _) = crate::ops::run_capture(&staged_count_cmd(o)).await?;
    let objects: usize = count.trim().parse().unwrap_or(0);
    if objects == 0 {
        bail!(
            "staged 0 objects from {}. Refusing to call that a successful export -- \
             upgrading now would leave the new store empty. Check --bucket (currently \
             {bucket}) and the store credentials.",
            from.suffix()
        );
    }
    Ok(MigrateStoreOutput::Exported {
        from: from.suffix().to_string(),
        to: to.suffix().to_string(),
        objects,
    })
}

/// `kubectl wait` for the staging pod, so the exec cannot race pod startup.
pub fn wait_ready_cmd(o: &CommonOpts) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("wait"),
            plain("-n"),
            plain(&o.namespace),
            plain(format!("pod/{}", staging_pod(&o.release))),
            plain("--for=condition=Ready"),
            plain("--timeout=180s"),
        ],
    )
}

/// Phase two: load the staged objects into the new store and verify per object.
pub async fn run_import(
    o: &CommonOpts,
    bucket: &str,
    keep_staging: bool,
) -> Result<MigrateStoreOutput> {
    let live: Vec<String> = crate::ops::live_stateful_components(o)
        .await?
        .into_iter()
        .map(|(component, _)| component)
        .collect();
    let to = detect_store(&live).ok_or_else(|| {
        anyhow::anyhow!(
            "no object store StatefulSet in the release. Run the upgrade before \
             `--phase import`."
        )
    })?;

    if o.dry_run {
        let endpoint = endpoint_for("<store-service>", &o.namespace);
        let cmds = [
            store_service_cmd(o, to),
            staged_listing_cmd(o),
            import_cmd(o, to, bucket, &endpoint),
            store_listing_cmd(o, to, bucket, &endpoint),
            delete_staging_pod_cmd(o),
        ];
        return Ok(MigrateStoreOutput::DryRun(crate::ui::DryRunPlan {
            lines: cmds.iter().map(|c| c.display()).collect(),
        }));
    }

    let (ok, service, err) = crate::ops::run_capture(&store_service_cmd(o, to)).await?;
    let service = service.trim().to_string();
    if !ok || service.is_empty() {
        bail!(
            "could not find the {} Service to copy into: {err}",
            to.suffix()
        );
    }
    let endpoint = endpoint_for(&service, &o.namespace);

    let (ok, staged, err) = crate::ops::run_capture(&staged_listing_cmd(o)).await?;
    if !ok {
        bail!(
            "could not read the staging pod. `--phase export` must run before \
             `--phase import`, and its pod must still exist: {err}"
        );
    }
    if staged.trim().is_empty() {
        bail!("the staging pod holds no objects; nothing to import");
    }
    let (ok, _, err) = crate::ops::run_capture(&import_cmd(o, to, bucket, &endpoint)).await?;
    if !ok {
        bail!("the import copy failed; the staged copy is intact, so retry is safe: {err}");
    }
    let (_, listing, _) =
        crate::ops::run_capture(&store_listing_cmd(o, to, bucket, &endpoint)).await?;

    let missing = missing_after(&staged, &listing);
    let added = added_after(&staged, &listing);
    let objects = listing.lines().filter(|l| !l.trim().is_empty()).count();

    // Keep the staging pod whenever anything is missing: it holds the only
    // remaining copy, and deleting it would turn a recoverable partial import
    // into permanent loss.
    let kept = keep_staging || !missing.is_empty();
    if !kept {
        crate::ops::run_capture(&delete_staging_pod_cmd(o))
            .await
            .ok();
    }
    Ok(MigrateStoreOutput::Imported {
        store: to.suffix().to_string(),
        objects,
        missing,
        added,
        staging_kept: kept,
    })
}

// -- one-command path ---------------------------------------------------------

/// `helm get values <release> -n <ns> -o yaml`, the upgrade's whole intent.
///
/// A file, never `--reuse-values`: reuse does not merge the NEW chart's
/// defaults, so a chart that adds a value key fails outright on a nil pointer.
/// Passing the captured values merges them OVER the new defaults, which also
/// re-supplies the generated store passwords -- they must be re-passed or the
/// upgrade rotates them out from under a running database.
pub fn get_values_cmd(o: &CommonOpts) -> OpsCommand {
    OpsCommand::new(
        "helm",
        vec![
            plain("get"),
            plain("values"),
            plain(&o.release),
            plain("-n"),
            plain(&o.namespace),
            plain("-o"),
            plain("yaml"),
        ],
    )
}

/// `helm upgrade` with the captured values.
pub fn upgrade_cmd(o: &CommonOpts, chart: &str, values_path: &str) -> OpsCommand {
    OpsCommand::new(
        "helm",
        vec![
            plain("upgrade"),
            plain(&o.release),
            plain(chart),
            plain("-n"),
            plain(&o.namespace),
            plain("-f"),
            plain(values_path),
            plain("--timeout"),
            plain("10m"),
        ],
    )
}

/// Wait for the new store's StatefulSet to be ready before importing into it.
pub fn wait_store_cmd(o: &CommonOpts, store: StoreKind) -> OpsCommand {
    OpsCommand::new(
        "kubectl",
        vec![
            plain("rollout"),
            plain("status"),
            plain(format!("statefulset/{}", store.suffix())),
            plain("-n"),
            plain(&o.namespace),
        ],
    )
}

/// Write helm values to a fresh 0600 file, created with restrictive permissions
/// atomically so the secrets inside are never briefly world-readable.
fn write_private_values(body: &str) -> Result<String> {
    let mut path = std::env::temp_dir();
    path.push(format!(
        "curie-migrate-values-{}.yaml",
        uuid::Uuid::new_v4()
    ));
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut file = opts.open(&path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
    }
    use std::io::Write;
    file.write_all(body.as_bytes())?;
    Ok(path.to_string_lossy().into_owned())
}

/// Removes the values file even when the upgrade fails.
struct ValuesFileCleanup(String);

impl Drop for ValuesFileCleanup {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

/// Export, upgrade, import and verify as one operation.
///
/// The whole point of the verb. Split phases exist for recovery, but a
/// procedure an operator runs by hand is one they can stop halfway -- and the
/// halfway state here is an empty store, which stops the bot answering.
///
/// This path also removes the need to pass `--allow-stateful-removal`: that
/// override exists so a human confirms the data is safe, and here the command
/// staged it itself moments earlier. Making an operator bypass a safety check
/// as a routine step teaches them to bypass it.
pub async fn run_auto(o: &CommonOpts, chart: &str, bucket: &str) -> Result<MigrateStoreOutput> {
    let live: Vec<String> = crate::ops::live_stateful_components(o)
        .await?
        .into_iter()
        .map(|(component, _)| component)
        .collect();
    // migrate_store reasons about COMPONENTS only (which store is which); the
    // resource names the guard also needs are irrelevant here.
    let rendered: Vec<String> = crate::ops::chart_stateful_components(chart, o, &[])
        .await?
        .into_iter()
        .map(|(component, _)| component)
        .collect();
    let (from, to) = ensure_migratable(&plan(&live, &rendered)?)?;

    let image = "amazon/aws-cli:2.32.6";
    let secret = format!("{}-secrets", o.release);
    if o.dry_run {
        let ep = endpoint_for("<store-service>", &o.namespace);
        let cmds = [
            store_service_cmd(o, from),
            create_staging_pod_cmd(o, image, &secret),
            export_cmd(o, from, bucket, &ep),
            get_values_cmd(o),
            upgrade_cmd(o, chart, "<captured values>"),
            store_service_cmd(o, to),
            import_cmd(o, to, bucket, &ep),
            store_listing_cmd(o, to, bucket, &ep),
            delete_staging_pod_cmd(o),
        ];
        return Ok(MigrateStoreOutput::DryRun(crate::ui::DryRunPlan {
            lines: cmds.iter().map(|c| c.display()).collect(),
        }));
    }

    let ui = crate::ui::ui();

    // 1. Stage, before anything is destroyed.
    let exported = run_export(o, chart, bucket).await?;
    let staged = match &exported {
        MigrateStoreOutput::Exported { objects, .. } => *objects,
        _ => 0,
    };
    ui.note(&format!("staged {staged} object(s) from {}", from.suffix()));

    // 2. Capture whole intent, then upgrade with it.
    let (ok, values, err) = crate::ops::run_capture(&get_values_cmd(o)).await?;
    if !ok {
        bail!("could not read the release's current values: {err}");
    }
    // The captured values carry the generated store passwords, so the file gets
    // the same 0600-at-creation treatment `ops::SecretValuesFileGuard` gives the
    // model credential, and is removed on the way out.
    let values_path = write_private_values(&values)?;
    let _cleanup = ValuesFileCleanup(values_path.clone());

    ui.note("upgrading the release (the staged copy is safe in the staging pod)");
    let (ok, _, err) = crate::ops::run_capture(&upgrade_cmd(o, chart, &values_path)).await?;
    if !ok {
        bail!(
            "the upgrade failed, so the old store is still in place and nothing was \
             lost. The staging pod still holds the export; delete it with \
             `kubectl delete pod {} -n {}` once you have resolved: {err}",
            staging_pod(&o.release),
            o.namespace
        );
    }

    // 3. Load the staged objects into whatever the upgrade created, and verify.
    ui.note(&format!("importing into {}", to.suffix()));
    run_import(o, bucket, false).await
}

#[cfg(test)]
mod auto_tests {
    use super::*;

    fn opts() -> CommonOpts {
        CommonOpts {
            namespace: "acme".into(),
            release: "acme".into(),
            dry_run: false,
        }
    }

    /// The upgrade must pass a values FILE. `--reuse-values` does not merge the
    /// new chart's defaults, so a chart that adds a value key fails outright --
    /// which is exactly what a store rename does.
    #[test]
    fn the_upgrade_passes_a_values_file_never_reuse_values() {
        let cmd = upgrade_cmd(&opts(), "/charts/curie", "/tmp/v.yaml");
        let line = cmd.display();
        assert!(line.contains("-f /tmp/v.yaml"), "{line}");
        assert!(
            !line.contains("--reuse-values"),
            "reuse-values drops the new chart's defaults: {line}"
        );
    }

    /// Captured values carry generated store passwords; the file must not be
    /// readable by other users, and must not survive the run.
    #[test]
    fn the_values_file_is_private_and_removed() {
        let path = write_private_values("postgres:\n  auth:\n    password: s3cret\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(&path).unwrap().permissions().mode();
            assert_eq!(
                mode & 0o777,
                0o600,
                "values file must be 0600, got {mode:o}"
            );
        }
        {
            let _cleanup = ValuesFileCleanup(path.clone());
        }
        assert!(
            !std::path::Path::new(&path).exists(),
            "the values file must be removed when the run ends"
        );
    }
}
