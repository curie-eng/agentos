//! `curie cluster github-app`: give the platform its own GitHub identity
//! (ADR-0092), so an agent repository needs no deploy workflow and no
//! per-repository credential.
//!
//! The private key is passed with `--set-file`, not `--set`. That is not a
//! style choice: a PEM is multi-line, and more importantly `--set` puts the
//! value in `argv`, where `ps` can read it and a subprocess error can echo it.
//! `--set-file` puts only the *path* there. This is the one credential in the
//! chart that can mint tokens for every repository in the installation, so it
//! is the one that most deserves never being in a process list.
//!
//! `--existing-secret` (#1255) goes one better and hands helm nothing at all:
//! the release records a Secret NAME, the chart resolves it with a
//! `secretKeyRef`, and no path and no PEM ever reach helm -- so the key cannot
//! land in retained release history the way #1236 found it. That path is only
//! reachable when the operator asks for it; `--set-file` above is still what a
//! chart-held connect does.

use anyhow::Result;

use crate::ops::{
    fetch_release_values, plain, require_on_path, resolve_existing_secret_ref, run_step,
    CommonOpts, OpsCommand,
};

#[derive(Debug, Clone)]
pub struct GithubAppOpts {
    pub common: CommonOpts,
    pub chart: String,
    /// The App's numeric id, from its settings page. Not secret.
    pub app_id: String,
    /// Path to the App's PEM private key. The path, never the contents.
    pub private_key_path: String,
    /// Name of an operator-managed Secret holding the PEM. The chart only
    /// REFERENCES it, so the key never passes through helm values and cannot
    /// land in retained release history. Empty means the chart-held path.
    pub existing_secret: String,
    /// Which data key inside `existing_secret` holds the PEM. Always emitted
    /// alongside `existing_secret` so `--existing-secret X` is deterministic
    /// rather than silently inheriting a stale custom key from a previous run.
    pub existing_secret_key: String,
    /// Clear the App credentials and fall back to `api.githubToken`.
    pub disconnect: bool,
}

/// Where the platform clones from. Set alongside the App because an empty base
/// makes git-flow fail before it ever reaches a credential -- it derives
/// `<base>/<repo>.git`, and with no base that is a path with no scheme, which
/// is rejected as a configuration error. An operator wiring up the App has
/// exactly the wrong context to debug that, so we set both together.
pub const DEFAULT_CLONE_BASE: &str = "https://github.com";

/// The data key the chart defaults to inside a BYO Secret
/// (`charts/curie/values.yaml`: `api.githubAppExistingSecretKey: privateKey`,
/// and the fallback `charts/curie/templates/api.yaml` renders). Mirrored here
/// so `--existing-secret-key` has a discoverable default in `--help` and in the
/// command manifest. If the two ever drift, `--existing-secret X` with no
/// `--existing-secret-key` writes a key name the chart never defaults to and
/// the api pod fails to start on a Secret that is perfectly correct.
pub const DEFAULT_APP_KEY_DATA_KEY: &str = "privateKey";

pub fn connect_commands(opts: &GithubAppOpts, clone_base: &str) -> Vec<OpsCommand> {
    let mut args = vec![
        plain("upgrade"),
        plain(&opts.common.release),
        plain(&opts.chart),
        plain("-n"),
        plain(&opts.common.namespace),
        plain("--reuse-values"),
        // --set-string, NOT --set. A numeric App ID round-trips through
        // helm's stored values as a float64, and `| quote` then renders it
        // in scientific notation: app id 1234567 reaches the API as
        // "1.234567e+06", the JWT's `iss` claim is wrong, and GitHub answers
        // 401 on every call. Found on a live cluster; a chart-render test
        // cannot see it, because it only appears once a real numeric value
        // has been through a --reuse-values round trip.
        plain("--set-string"),
        plain(format!("api.githubAppId={}", opts.app_id)),
    ];
    if opts.existing_secret.trim().is_empty() {
        // The key's CONTENTS never enter argv; helm reads the file itself.
        args.push(plain("--set-file"));
        args.push(plain(format!(
            "api.githubAppPrivateKey={}",
            opts.private_key_path
        )));
    } else {
        // The BYO path emits no --set-file at all: helm is never told where
        // the PEM lives, so it cannot copy the contents into the release the
        // way #1236 found them sitting in revision 15 of a live install. The
        // release holds a Secret NAME, and the chart resolves it at pod start.
        //
        // --set-string for BOTH entries, never --set. `1234567` is a valid
        // RFC-1123 label and a valid Secret data key; under --set helm parses
        // it as a number, a --reuse-values round trip stores it as a float64,
        // and the next upgrade renders `1.234567e+06` -- the secretKeyRef then
        // names a Secret that does not exist and the api pod never starts.
        // That is #1236's App-ID float bug transplanted into a new field, and
        // a chart-render test cannot see it because it only appears after a
        // real round trip.
        args.push(plain("--set-string"));
        args.push(plain(format!(
            "api.githubAppExistingSecret={}",
            opts.existing_secret
        )));
        args.push(plain("--set-string"));
        args.push(plain(format!(
            "api.githubAppExistingSecretKey={}",
            opts.existing_secret_key
        )));
        // Clear the inline key while adopting the Secret. --reuse-values
        // copies a still-set api.githubAppPrivateKey into every future
        // revision forever -- including the ones `curie cluster up` runs --
        // so leaving it gives the operator the ceremony of the recommended
        // path and none of its benefit. Harmless to the running pod: the
        // chart's BYO branch wins, so it was already reading the Secret.
        args.push(plain("--set"));
        args.push(plain("api.githubAppPrivateKey="));
    }
    args.push(plain("--set"));
    args.push(plain(format!("api.githubCloneBase={clone_base}")));
    vec![OpsCommand::new("helm", args)]
}

pub fn disconnect_commands(opts: &GithubAppOpts) -> Vec<OpsCommand> {
    vec![OpsCommand::new(
        "helm",
        vec![
            plain("upgrade"),
            plain(&opts.common.release),
            plain(&opts.chart),
            plain("-n"),
            plain(&opts.common.namespace),
            plain("--reuse-values"),
            plain("--set"),
            plain("api.githubAppId="),
            plain("--set"),
            plain("api.githubAppPrivateKey="),
            // Only the Secret NAME is cleared. Setting
            // api.githubAppExistingSecretKey="" would NOT restore the chart
            // default (`privateKey`) -- --reuse-values re-supplies the empty
            // string on every later upgrade, so the release overrides the
            // default permanently. An operator who later hand-set
            // githubAppExistingSecret with no key would then render `key: ""`
            // and the api pod would sit in CreateContainerConfigError with
            // nothing in the release to explain why. The field is inert while
            // the name is empty, so leaving it alone is strictly safer.
            plain("--set"),
            plain("api.githubAppExistingSecret="),
        ],
    )]
}

/// Roll the API so the Secret-backed key is actually read. Without this the
/// upgrade succeeds and nothing changes until the next unrelated restart --
/// the operator sees "configured" and pushes still fail to clone.
pub fn rollout_commands(namespace: &str, release: &str) -> Vec<OpsCommand> {
    let target = format!("deployment/{release}-api");
    vec![
        OpsCommand::new(
            "kubectl",
            vec![
                plain("-n"),
                plain(namespace),
                plain("rollout"),
                plain("restart"),
                plain(&target),
            ],
        ),
        OpsCommand::new(
            "kubectl",
            vec![
                plain("-n"),
                plain(namespace),
                plain("rollout"),
                plain("status"),
                plain(&target),
                plain("--timeout=180s"),
            ],
        ),
    ]
}

/// What the RELEASE's raw `api.githubAppExistingSecret` leaf means to the
/// CHART, judged by Helm's own truthiness rather than by Rust's idea of a
/// string.
///
/// The chart's BYO branch is `{{- if .Values.api.githubAppExistingSecret }}` --
/// plain Go-template truthiness, which sees far more than strings.
/// [`configured_existing_secret`] delegates to `resolve_existing_secret_ref`,
/// which reads the leaf with `.as_str()` and so answers `None` for ANY
/// non-string value. On its own that makes the conflict guard fail OPEN: a
/// release configured by hand as `--set api.githubAppExistingSecret=true`, or
/// with an all-digit Secret name (which helm stores as a float64 -- #1236), is
/// genuinely BYO to the chart while the guard concludes "no BYO configured".
/// The next `--private-key` then writes an ignored PEM, rolls the API and
/// reports success over the OLD key, which is exactly the false-success bug
/// #1255 exists to remove.
///
/// Classified here, locally, rather than by widening `resolve_existing_secret_ref`:
/// that helper is shared with the eight other direct-passthrough credentials
/// (#1759), so changing its contract would silently change behaviour for all of
/// them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ByoSecretField {
    /// Falsy to Helm's `if` -- absent, `null`, `""`, `false`, `0`, or an empty
    /// list/map. The chart really does take the chart-held branch, so a
    /// `--private-key` connect is legitimate here and must NOT be refused:
    /// refusing would brick the documented chart-held rotation.
    ChartHeld,
    /// A non-empty string: the chart takes the BYO branch and the CLI can say
    /// which Secret it resolves.
    Named,
    /// Truthy to Helm, but not a name. The chart WILL take the BYO branch while
    /// the CLI cannot determine which Secret the API is reading. Fail closed.
    Opaque,
}

/// Classify the raw `api.githubAppExistingSecret` leaf by Helm's truthiness.
///
/// A small local walk of the values JSON, deliberately not
/// `resolve_existing_secret_ref` -- see [`ByoSecretField`] for why the shared
/// helper must not learn about non-string values.
pub(crate) fn classify_existing_secret_field(
    existing: Option<&serde_json::Value>,
) -> ByoSecretField {
    let Some(value) = existing
        .and_then(|v| v.get("api"))
        .and_then(|api| api.get("githubAppExistingSecret"))
    else {
        return ByoSecretField::ChartHeld;
    };
    match value {
        serde_json::Value::Null => ByoSecretField::ChartHeld,
        serde_json::Value::Bool(true) => ByoSecretField::Opaque,
        serde_json::Value::Bool(false) => ByoSecretField::ChartHeld,
        serde_json::Value::String(s) if s.is_empty() => ByoSecretField::ChartHeld,
        serde_json::Value::String(_) => ByoSecretField::Named,
        // A Go template treats zero as false. Compared as f64 because a
        // --reuse-values round trip stores every number as one (#1236).
        serde_json::Value::Number(n) if n.as_f64() == Some(0.0) => ByoSecretField::ChartHeld,
        // An EMPTY list or map is falsy to a Go template exactly as `""` is, so
        // it leaves the chart on the chart-held branch; only a populated one
        // reaches the BYO branch we cannot read a name out of.
        serde_json::Value::Array(a) if a.is_empty() => ByoSecretField::ChartHeld,
        serde_json::Value::Object(o) if o.is_empty() => ByoSecretField::ChartHeld,
        _ => ByoSecretField::Opaque,
    }
}

/// The JSON type of the leaf, for a refusal that tells the operator what shape
/// their release is actually in. The type only, never the value.
fn json_type_name(value: &serde_json::Value) -> &'static str {
    match value {
        serde_json::Value::Null => "null",
        serde_json::Value::Bool(_) => "boolean",
        serde_json::Value::Number(_) => "number",
        serde_json::Value::String(_) => "string",
        serde_json::Value::Array(_) => "list",
        serde_json::Value::Object(_) => "map",
    }
}

/// The BYO Secret the RELEASE currently resolves the App private key from, if
/// any: `(secret name, data key)`.
///
/// Pure over the values JSON `helm get values -o json` returns; the async read
/// stays in the caller. Delegates to [`resolve_existing_secret_ref`] rather
/// than re-deriving the precedence, because a CLI read that disagreed with the
/// chart's own BYO-wins rule would report a plausible but wrong answer, which
/// is worse than reporting none (#1759). The three literals are the exact
/// strings `charts/curie/templates/api.yaml` reads.
pub(crate) fn configured_existing_secret(
    existing: Option<&serde_json::Value>,
) -> Option<(String, String)> {
    resolve_existing_secret_ref(
        existing,
        "api.githubAppExistingSecret",
        "api.githubAppExistingSecretKey",
        DEFAULT_APP_KEY_DATA_KEY,
    )
}

/// True when this invocation could silently write a key nothing reads: a
/// chart-held connect (`--private-key`, no `--existing-secret`) that we have
/// not yet checked against the release.
///
/// Cheap predicate so a `--disconnect` or an explicit BYO connect never pays
/// for a `helm get values` round trip -- neither needs anything from the
/// release, and on a real run the read would add a hard failure when the
/// cluster is unreachable to two paths that never had one.
pub(crate) fn needs_byo_conflict_check(opts: &GithubAppOpts) -> bool {
    !opts.disconnect
        && opts.existing_secret.trim().is_empty()
        && !opts.private_key_path.trim().is_empty()
}

/// True when THIS invocation must read the release's values before it acts.
///
/// A `--dry-run` answers false unconditionally. `cli/CLAUDE.md` makes it a
/// load-bearing invariant that pure argv builders never fetch and that
/// `--dry-run` never touches the network, and that invariant outranks the
/// convenience of refusing a plan: a best-effort read that silently degrades to
/// "no conflict" the moment `helm get values` fails is worse than no read at
/// all, because automation cannot tell a conflict-checked plan from a plan
/// whose check was skipped. Nothing is lost -- [`guard_byo_key_conflict`] runs
/// on the real invocation BEFORE any mutation, so a misconfigured release is
/// never written to either way.
pub(crate) fn needs_release_read(opts: &GithubAppOpts) -> bool {
    !opts.common.dry_run && needs_byo_conflict_check(opts)
}

/// Refuse rather than report success over an unchanged live key.
///
/// The chart resolves `GITHUB_APP_PRIVATE_KEY` from the BYO Secret whenever
/// `api.githubAppExistingSecret` is non-empty, so `--set-file
/// api.githubAppPrivateKey=...` on such a release writes a value nothing
/// reads, rolls the API, and prints "GitHub App configured" while the pod
/// keeps signing with the OLD key. The README's next rotation step is "delete
/// the first key on GitHub", at which point every clone 401s and nothing the
/// CLI printed ever hinted at it.
///
/// We refuse instead of writing into the Secret: it is operator-managed
/// precisely so External Secrets or Sealed Secrets can own it, and a CLI write
/// there would be reverted on the next reconcile -- a second, subtler
/// misreport.
///
/// Judged in two stages, because "non-empty" is a Rust question and the chart
/// asks a Helm one. [`classify_existing_secret_field`] answers what the CHART
/// will do with the raw leaf, and only a leaf that is a real string goes on to
/// [`configured_existing_secret`] to be named. A leaf that is truthy to Helm
/// but not a string is refused without a name rather than read as "nothing
/// configured", which is the same false success one layer down.
///
/// Called with `existing = None` under `--dry-run` (see [`needs_release_read`]),
/// where it is a no-op: the plan is offline, and the refusal comes on the real
/// invocation before helm is ever run.
pub(crate) fn guard_byo_key_conflict(
    opts: &GithubAppOpts,
    existing: Option<&serde_json::Value>,
) -> Result<()> {
    if !needs_byo_conflict_check(opts) {
        return Ok(());
    }
    match classify_existing_secret_field(existing) {
        // Falsy to the chart's own `{{- if }}`, so the API really is on the
        // chart-held key and this rotation is exactly what it looks like.
        ByoSecretField::ChartHeld => return Ok(()),
        // Truthy to the chart, unreadable to us. Refusing is the only honest
        // answer: guessing "not configured" here is the #1255 bug itself.
        ByoSecretField::Opaque => return Err(opaque_byo_field_error(opts, existing)),
        ByoSecretField::Named => {}
    }
    let Some((name, key)) = configured_existing_secret(existing) else {
        return Ok(());
    };
    // CliError::failure + with_fix rather than bail!, so the --json path emits
    // an actionable `fix` alongside `error` (ADR-0021) instead of an untyped
    // anyhow the agent driving the CLI cannot act on.
    Err(crate::exit::CliError::failure(format!(
        "release {} already reads the GitHub App private key from Secret {name} (key {key}); \
         --private-key would write a value the API never reads and report success over the OLD key",
        opts.common.release
    ))
    .with_fix(format!(
        "update Secret {name} yourself, then re-run with --existing-secret {name} \
         --existing-secret-key {key} to roll the API onto it; or run --disconnect first \
         to go back to the chart-held key"
    ))
    .into())
}

/// The refusal for a release whose `api.githubAppExistingSecret` is truthy to
/// the chart but not a string.
///
/// An error on the centralized error emit, never a new `GithubAppOutput`
/// variant: `cli/schema/github-app.schema.json` is a frozen committed contract,
/// and a refusal is not a success-path value.
fn opaque_byo_field_error(
    opts: &GithubAppOpts,
    existing: Option<&serde_json::Value>,
) -> anyhow::Error {
    let kind = existing
        .and_then(|v| v.get("api"))
        .and_then(|api| api.get("githubAppExistingSecret"))
        .map(json_type_name)
        .unwrap_or("non-string value");
    crate::exit::CliError::failure(format!(
        "release {} stores api.githubAppExistingSecret as a {kind}, not a string. The chart's \
         BYO branch is plain truthiness, so the API IS reading its private key from a Secret -- \
         but the CLI cannot determine WHICH Secret, and it will not guess by writing a \
         --private-key the API may never read",
        opts.common.release
    ))
    .with_fix(
        "re-set the field as a string, e.g. helm upgrade --reuse-values --set-string \
         api.githubAppExistingSecret=<secret-name>, then re-run; or run --disconnect first to go \
         back to the chart-held key",
    )
    .into()
}

pub enum GithubAppOutput {
    DryRun(crate::ui::DryRunPlan),
    Done { configured: bool },
}

impl crate::ui::CliOutput for GithubAppOutput {
    fn to_json(&self) -> serde_json::Value {
        match self {
            GithubAppOutput::DryRun(plan) => plan.to_json(),
            GithubAppOutput::Done { configured } => {
                serde_json::json!({"github_app_configured": configured})
            }
        }
    }

    fn render(&self, ui: &crate::ui::Ui) {
        if let GithubAppOutput::DryRun(plan) = self {
            plan.render(ui);
        }
    }
}

pub async fn github_app(opts: GithubAppOpts, clone_base: &str) -> Result<GithubAppOutput> {
    let ui = crate::ui::ui();
    require_connect_inputs(&opts)?;

    // On a real run helm must be on PATH before either the values read below
    // or the upgrade further down; a --dry-run has always worked with no
    // tooling and no cluster, so it stays exempt.
    if !opts.common.dry_run {
        require_on_path("helm")?;
    }
    let existing = if needs_release_read(&opts) {
        // The typed error propagates: the `helm upgrade` two steps later would
        // fail identically, and failing before any mutation is strictly better
        // than failing part-way through one.
        fetch_release_values(&opts.common).await?
    } else {
        None
    };
    // Under `--dry-run` this is a no-op by construction: `needs_release_read`
    // answered false, so `existing` is None and the guard has nothing to judge.
    // The plan therefore stays offline, and the guard still runs on the REAL
    // invocation above -- before any mutation -- so nothing is ever written to
    // a release whose key the API would ignore.
    guard_byo_key_conflict(&opts, existing.as_ref())?;

    let cmds = if opts.disconnect {
        disconnect_commands(&opts)
    } else {
        connect_commands(&opts, clone_base)
    };
    let rollout = rollout_commands(&opts.common.namespace, &opts.common.release);

    if opts.common.dry_run {
        return Ok(GithubAppOutput::DryRun(crate::ui::DryRunPlan {
            lines: cmds
                .iter()
                .chain(rollout.iter())
                .map(|cmd| cmd.display())
                .collect(),
        }));
    }

    require_on_path("kubectl")?;
    let cl = ui.checklist();
    let label = if opts.disconnect {
        format!(
            "clearing the GitHub App from release {}",
            opts.common.release
        )
    } else {
        format!(
            "configuring the GitHub App on release {}",
            opts.common.release
        )
    };
    let ok_detail = if opts.disconnect {
        "cleared"
    } else {
        "configured"
    };
    for cmd in &cmds {
        run_step(&cl, &label, ok_detail, cmd).await?;
    }
    // A secretKeyRef env var is resolved once at pod start, so the Secret
    // change alone leaves the running API on the old credential.
    let roll_label = format!("rolling {} to pick up the credential", opts.common.release);
    for cmd in &rollout {
        run_step(&cl, &roll_label, "rolled", cmd).await?;
    }
    if opts.disconnect {
        ui.note("GitHub App cleared; the platform falls back to api.githubToken");
    } else if !opts.existing_secret.trim().is_empty() {
        // The Secret NAME is safe to print -- it is a name, not a credential,
        // and the CLI never reads the Secret's contents on this path at all.
        // Naming it is the point: the operator has to know which Secret and
        // which data key they now own the rotation of.
        ui.note(&format!(
            "GitHub App configured to read its private key from Secret {} (key {}); \
             the API has been rolled onto it. You own that Secret's contents -- rotate \
             by updating it and re-running this command.",
            opts.existing_secret, opts.existing_secret_key
        ));
    } else {
        ui.note(
            "GitHub App configured. Install it on the repositories you deploy from, \
             then a push to your dev/main branch deploys with no workflow in the agent repo.",
        );
    }
    Ok(GithubAppOutput::Done {
        configured: !opts.disconnect,
    })
}

/// The Kubernetes cap on an RFC-1123 subdomain, which is what a Secret name is.
const MAX_SECRET_NAME_LEN: usize = 253;

/// True when `value` is a syntactically valid Kubernetes Secret NAME: an
/// RFC-1123 subdomain.
///
/// Validated positively, against the syntax the flag names, because
/// `--set-string` is NOT an escaping mechanism. It stops helm *typing* a value,
/// but helm still splits the expression on commas STRUCTURALLY, so
/// `--existing-secret-key 'privateKey,api.githubAppExistingSecret='` is one
/// argv entry that helm reads as TWO assignments -- the second blanking the BYO
/// reference the same command just set. The run then also clears the inline
/// key, rolls the API and reports success on a release with no usable key at
/// all. An unvalidated name here is an EXPRESSION-INJECTION vector, not merely
/// an invalid name, and k8s never gets to reject it with its own message
/// because the injected assignment blanked the field before it ever rendered.
///
/// A positive charset rather than a blocklist of dangerous characters: `,`,
/// `=`, `\`, spaces and newlines fall out as rejected by construction, along
/// with whatever a future helm decides is structural.
fn is_rfc1123_subdomain(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_SECRET_NAME_LEN
        && value.split('.').all(is_rfc1123_label)
}

/// One dot-separated label of an RFC-1123 subdomain: lowercase alphanumerics
/// and `-`, starting and ending alphanumeric, never empty.
fn is_rfc1123_label(label: &str) -> bool {
    let alnum = |c: char| c.is_ascii_lowercase() || c.is_ascii_digit();
    let (Some(first), Some(last)) = (label.chars().next(), label.chars().next_back()) else {
        return false;
    };
    alnum(first) && alnum(last) && label.chars().all(|c| alnum(c) || c == '-')
}

/// True when `value` is a syntactically valid Secret DATA key: `[-._a-zA-Z0-9]+`
/// and neither `.` nor `..`, which is what the API server accepts inside a
/// Secret's `data` map. Same injection reasoning as [`is_rfc1123_subdomain`];
/// the charset is wider because the key names a map entry, not a DNS name.
fn is_secret_data_key(value: &str) -> bool {
    !value.is_empty()
        && value != "."
        && value != ".."
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
}

/// Render a rejected value for an error message without echoing something that
/// might be key material.
///
/// Both flags take short names, so a short value is quoted back verbatim (with
/// `{:?}`, so an embedded newline or tab shows as an escape rather than
/// mangling the message). Anything long enough to be a pasted PEM is described
/// by its length only -- the operator knows what they typed, and the terminal,
/// the shell history and the `--json` error payload do not need a copy of it.
fn describe_rejected_value(value: &str) -> String {
    const MAX_ECHO: usize = 63;
    let len = value.chars().count();
    if len <= MAX_ECHO {
        format!("{value:?}")
    } else {
        format!("<{len} characters, not shown>")
    }
}

/// Validate the flag combination before anything reaches helm.
///
/// Takes the whole record rather than five positional `&str`/`bool` arguments:
/// the old three-argument form was already one argument swap away from a
/// silent bug, and the rules below now read five of the fields.
///
/// All eight refusals here are deterministic input errors -- the identical argv
/// fails identically every time -- so they exit 2 (ADR-0021 Usage) with a
/// non-null `fix` naming the flag to correct (#1261). A bare `bail!` classified
/// them as exit 1 with a null fix -- indistinguishable to an agent from the helm
/// upgrade itself failing, which is retryable and these are not. clap gives
/// every one of these flags a `default_value`, so clap never raises its own exit
/// 2 for them and this is the only place the class is set. That covers the three
/// original refusals (a missing `--app-id`, a missing `--private-key`, a
/// `--private-key` path that is not a file) and the five `--existing-secret`
/// rules #1255 adds below; they are the same category and take the same class.
///
/// The refusals that are NOT here stay `CliError::failure` on purpose:
/// [`guard_byo_key_conflict`] and [`opaque_byo_field_error`] judge the state of
/// the DEPLOYED release, so the same argv succeeds once the operator updates it.
pub fn require_connect_inputs(opts: &GithubAppOpts) -> Result<()> {
    // The one fact every rule below branches on: an empty --existing-secret is
    // the chart-held path, a non-empty one is BYO. Bound once so a new rule
    // cannot pick the wrong polarity.
    let byo = !opts.existing_secret.trim().is_empty();
    // The path itself is used verbatim for the stat and the message, as it
    // has been since #1223.
    let key_path = &opts.private_key_path;

    // "--disconnect --existing-secret X" reads as "point at X while
    // disconnecting". Accepting it would clear the release and leave the
    // operator believing a reference to X was set. Every OTHER connect input
    // stays silently tolerated under --disconnect, as it has been since #1223.
    if opts.disconnect {
        if byo {
            return Err(anyhow::Error::from(
                crate::exit::CliError::usage(
                    "--existing-secret contradicts --disconnect: --disconnect clears the App \
                     credentials, so there is nothing left to point at a Secret. Run \
                     --disconnect on its own, or drop it to configure the Secret.",
                )
                .with_fix(
                    "drop --existing-secret to clear the App, or drop --disconnect to point \
                     the release at that Secret",
                ),
            ));
        }
        return Ok(());
    }
    // Syntax-checked BEFORE any command is constructed, because helm splits a
    // --set-string expression on commas structurally: an unvalidated name is an
    // expression-injection vector, not merely an invalid name. See
    // `is_rfc1123_subdomain` for the full mechanism.
    //
    // The checks below use the RAW value, not `byo`: `connect_commands` formats
    // the raw field into argv, so a name with surrounding whitespace would pass
    // a trimmed check and still reach helm -- and k8s -- wrong.
    if byo {
        if !is_rfc1123_subdomain(&opts.existing_secret) {
            return Err(anyhow::Error::from(
                crate::exit::CliError::usage(format!(
                    "--existing-secret {} is not a Kubernetes Secret name. It must be an \
                     RFC-1123 subdomain: lowercase letters, digits, '-' and '.', starting and \
                     ending with a letter or digit, at most {MAX_SECRET_NAME_LEN} characters. \
                     It names a Secret you already created; to hand the PEM itself to the \
                     chart, use --private-key.",
                    describe_rejected_value(&opts.existing_secret)
                ))
                .with_fix(
                    "rerun with --existing-secret <the name of a Secret you already created>, \
                     or use --private-key <path to the PEM> to hand the key to the chart",
                ),
            ));
        }
        if !is_secret_data_key(&opts.existing_secret_key) {
            return Err(anyhow::Error::from(
                crate::exit::CliError::usage(format!(
                    "--existing-secret-key {} is not a Kubernetes Secret data key. It must be \
                     one or more of [-._a-zA-Z0-9] and cannot be '.' or '..'. It names a key \
                     INSIDE that Secret -- not the PEM, and not a helm expression.",
                    describe_rejected_value(&opts.existing_secret_key)
                ))
                .with_fix(
                    "rerun with --existing-secret-key <the data key inside that Secret>, made \
                     only of [-._a-zA-Z0-9]",
                ),
            ));
        }
    }
    if byo && !key_path.trim().is_empty() {
        return Err(anyhow::Error::from(
            crate::exit::CliError::usage(
                "--existing-secret and --private-key are two mutually exclusive ways to supply \
                 the App's private key; pick one. --existing-secret references a Secret you \
                 manage, --private-key hands the PEM to the chart.",
            )
            .with_fix("pass either --existing-secret <name> or --private-key <path>, not both"),
        ));
    }
    if !byo && opts.existing_secret_key.trim() != DEFAULT_APP_KEY_DATA_KEY {
        return Err(anyhow::Error::from(
            crate::exit::CliError::usage(
                "--existing-secret-key configures nothing without --existing-secret: the chart \
                 only reads a data key once a Secret name is set. Pass --existing-secret <name>, \
                 or drop --existing-secret-key.",
            )
            .with_fix(
                "rerun with --existing-secret <the Secret that holds the PEM>, or drop \
                 --existing-secret-key to stay on the chart-held key",
            ),
        ));
    }
    if opts.app_id.trim().is_empty() {
        return Err(anyhow::Error::from(
            crate::exit::CliError::usage(
                "--app-id is required. Find it on the App's settings page \
                 (Settings -> Developer settings -> GitHub Apps -> your app).",
            )
            .with_fix("rerun with --app-id <numeric app id from the App's settings page>"),
        ));
    }
    // Both remaining checks are chart-held only: a BYO run supplies no PEM by
    // design, so it must never be asked for one and must never stat a path it
    // was never given.
    if !byo {
        if key_path.trim().is_empty() {
            return Err(anyhow::Error::from(
                crate::exit::CliError::usage(
                    "--private-key is required: the path to the App's PEM file, \
                     downloaded from the App's settings page under 'Private keys'.",
                )
                .with_fix("rerun with --private-key <path to the App's PEM file>"),
            ));
        }
        if !std::path::Path::new(key_path).is_file() {
            return Err(anyhow::Error::from(
                crate::exit::CliError::usage(format!("--private-key: no such file: {key_path}"))
                    .with_fix("rerun with --private-key pointing at an existing PEM file"),
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn opts(disconnect: bool) -> GithubAppOpts {
        GithubAppOpts {
            common: CommonOpts {
                namespace: "curie".into(),
                release: "curie".into(),
                dry_run: true,
            },
            chart: "charts/curie".into(),
            app_id: "12345".into(),
            private_key_path: "/tmp/app.pem".into(),
            existing_secret: String::new(),
            existing_secret_key: DEFAULT_APP_KEY_DATA_KEY.into(),
            disconnect,
        }
    }

    /// A valid BYO connect: the operator owns the Secret, so no PEM path is
    /// supplied at all. Supplying both is what `require_connect_inputs` refuses
    /// (`existing_secret_with_a_private_key_is_refused`), so this is the shape
    /// a real `--existing-secret` invocation actually has.
    fn byo_opts() -> GithubAppOpts {
        let mut o = opts(false);
        o.private_key_path = String::new();
        o.existing_secret = "my-github-app".into();
        o
    }

    fn argv(cmd: &OpsCommand) -> Vec<String> {
        cmd.argv()
    }

    /// True when `args` carries `value` as a WHOLE argv entry.
    ///
    /// Whole entries, never `contains` on the joined string:
    /// `contains("api.githubAppExistingSecret=")` is also satisfied by
    /// `api.githubAppExistingSecret=something-else`, so it tests for a prefix
    /// rather than for the value that was actually set (#1263).
    fn has_entry(args: &[String], value: &str) -> bool {
        args.iter().any(|a| a == value)
    }

    /// True when any whole entry begins with `prefix`. Only ever used to assert
    /// ABSENCE of a whole value family, which is the one question a prefix
    /// legitimately answers.
    fn has_entry_starting(args: &[String], prefix: &str) -> bool {
        args.iter().any(|a| a.starts_with(prefix))
    }

    /// The whole argv entry immediately preceding `value`.
    ///
    /// Panics rather than returning an Option: a test that silently skips its
    /// own assertion because the entry moved is the decoration #1263 found.
    fn flag_before(args: &[String], value: &str) -> String {
        let at = args
            .iter()
            .position(|a| a == value)
            .unwrap_or_else(|| panic!("no argv entry equal to `{value}`: {args:?}"));
        assert!(at > 0, "`{value}` has no preceding flag: {args:?}");
        args[at - 1].clone()
    }

    /// The ADR-0021 `fix` hint an error carries, recovered through the very
    /// `exit::classify` the `--json` error emitter uses. A refusal whose fix
    /// does not survive that path is invisible to the agent driving the CLI,
    /// which is the consumer this ticket exists to stop misleading.
    fn fix_of(err: &anyhow::Error) -> String {
        let (class, fix) = crate::exit::classify(err);
        assert_eq!(
            class,
            crate::exit::ExitClass::Failure,
            "the refusal must exit non-zero as a real classification: {err}"
        );
        fix.unwrap_or_else(|| panic!("the refusal must carry an actionable fix: {err}"))
    }

    #[test]
    fn the_app_id_is_set_as_a_string() {
        // helm's `--set` parses a bare number, and a --reuse-values round trip
        // turns it into a float64. App id 1234567 then renders as
        // "1.234567e+06", the JWT's `iss` claim is wrong, and EVERY GitHub call
        // answers 401. Found on a live cluster -- a chart-render test cannot
        // see it, because it only appears once a real numeric value has been
        // through helm's stored values.
        let cmds = connect_commands(&opts(false), DEFAULT_CLONE_BASE);
        let flat = argv(&cmds[0]).join(" ");
        assert!(
            flat.contains("--set-string api.githubAppId="),
            "app id must use --set-string, not --set: {flat}"
        );
    }

    /// A real file holding a PEM-shaped body, so "the contents never reach
    /// argv" is checked against contents that exist.
    ///
    /// The previous version pointed at `/tmp/app.pem`, which was never created.
    /// Inlining `read_to_string(path)` into argv therefore stayed green -- it
    /// read `""` -- so the assertion guarded a literal `BEGIN` and not the
    /// realistic regression (#1263).
    fn key_fixture() -> (tempfile::TempDir, String) {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("app.pem");
        std::fs::write(
            &path,
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAtestkeymaterial\n-----END RSA PRIVATE KEY-----\n",
        )
        .expect("write fixture");
        let as_str = path.to_string_lossy().into_owned();
        (dir, as_str)
    }

    #[test]
    fn the_private_key_contents_never_reach_argv() {
        // The whole reason for --set-file. A PEM in argv is readable by `ps`
        // and can be echoed by a subprocess error.
        let (_dir, path) = key_fixture();
        let body = std::fs::read_to_string(&path).expect("fixture readable");
        let mut o = opts(false);
        o.private_key_path = path.clone();

        let flat = argv(&connect_commands(&o, DEFAULT_CLONE_BASE)[0]).join(" ");
        assert!(flat.contains("--set-file"), "{flat}");
        assert!(
            flat.contains(&format!("api.githubAppPrivateKey={path}")),
            "{flat}"
        );
        // The real assertion: no line of the file's CONTENT appears anywhere.
        for line in body.lines().filter(|l| !l.trim().is_empty()) {
            assert!(!flat.contains(line), "key material reached argv: {line}");
        }
    }

    #[test]
    fn connecting_also_sets_the_clone_base() {
        // An empty base fails git-flow before a credential is ever consulted,
        // with an error about schemes that reads like a bug rather than a
        // missing setting.
        let flat = argv(&connect_commands(&opts(false), DEFAULT_CLONE_BASE)[0]).join(" ");
        assert!(flat.contains("api.githubCloneBase=https://github.com"));
    }

    #[test]
    fn a_custom_clone_base_is_honoured() {
        // Passing DEFAULT_CLONE_BASE and asserting the default appears also
        // passes when the parameter is ignored entirely (#1263). A GitHub
        // Enterprise install would silently get github.com and every clone
        // would fail an origin check.
        let flat = argv(&connect_commands(&opts(false), "https://ghe.example.com")[0]).join(" ");
        assert!(
            flat.contains("api.githubCloneBase=https://ghe.example.com"),
            "the supplied clone base was ignored: {flat}"
        );
    }

    #[test]
    fn the_upgrade_reuses_existing_values() {
        // Dropping --reuse-values resets every other value to chart defaults:
        // Slack tokens, the model credential, the connector reconciler flag.
        // Silent, destructive, and uncaught (#1263). The BYO branch is a third
        // command builder and carries the same obligation.
        for cmds in [
            connect_commands(&opts(false), DEFAULT_CLONE_BASE),
            connect_commands(&byo_opts(), DEFAULT_CLONE_BASE),
            disconnect_commands(&opts(true)),
        ] {
            let flat = argv(&cmds[0]).join(" ");
            assert!(
                flat.contains("--reuse-values"),
                "would reset other values: {flat}"
            );
        }
    }

    #[test]
    fn disconnect_clears_both_app_fields_and_touches_nothing_else() {
        // Asserted as whole argv entries, not with `contains` on the joined
        // string: `contains("api.githubAppId=")` is also satisfied by
        // `api.githubAppId=999`, so it checked for the presence of a prefix
        // rather than for clearing (#1263).
        let args = argv(&disconnect_commands(&opts(true))[0]);
        assert!(
            args.iter().any(|a| a == "api.githubAppId="),
            "the App id was not cleared to empty: {args:?}"
        );
        assert!(
            args.iter().any(|a| a == "api.githubAppPrivateKey="),
            "the private key was not cleared to empty: {args:?}"
        );
        let flat = args.join(" ");
        // The PAT fallback must survive: clearing the App is how an operator
        // goes back to it.
        assert!(!flat.contains("api.githubToken"));
    }

    #[test]
    fn the_api_is_rolled_so_the_new_key_is_actually_read() {
        let cmds = rollout_commands("curie", "curie");
        let flat: Vec<String> = cmds.iter().map(|c| argv(c).join(" ")).collect();
        assert!(flat[0].contains("rollout restart deployment/curie-api"));
        assert!(flat[1].contains("rollout status deployment/curie-api"));
    }

    // ---- T1: the BYO connect names the Secret and its data key -------------

    #[test]
    fn the_byo_connect_names_the_secret_and_the_data_key_as_whole_entries() {
        // AC1. If either entry is missing, the chart falls back to the inline
        // key that this same command just cleared: GITHUB_APP_PRIVATE_KEY
        // resolves to nothing, the api pod mints no JWT, and every clone 401s
        // -- while the CLI still reports "GitHub App configured".
        let args = argv(&connect_commands(&byo_opts(), DEFAULT_CLONE_BASE)[0]);
        assert!(
            has_entry(&args, "api.githubAppExistingSecret=my-github-app"),
            "the BYO Secret name never reached helm: {args:?}"
        );
        assert!(
            has_entry(&args, "api.githubAppExistingSecretKey=privateKey"),
            "the BYO data key never reached helm: {args:?}"
        );
        assert_eq!(
            flag_before(&args, "api.githubAppExistingSecret=my-github-app"),
            "--set-string",
            "the Secret name must not be helm-typed: {args:?}"
        );
        assert_eq!(
            flag_before(&args, "api.githubAppExistingSecretKey=privateKey"),
            "--set-string",
            "the data key must not be helm-typed: {args:?}"
        );
    }

    // ---- T2: a custom data key is honoured ---------------------------------

    #[test]
    fn a_custom_existing_secret_key_is_honoured() {
        // Passing the default (`privateKey`) and asserting the default appears
        // also passes when the parameter is ignored entirely (#1263). An
        // operator whose ESO-managed Secret stores the PEM under `app-pem`
        // would get `key: privateKey`, a key that does not exist in that
        // Secret, and the api pod would sit in CreateContainerConfigError.
        let mut o = byo_opts();
        o.existing_secret_key = "app-pem".into();
        let args = argv(&connect_commands(&o, DEFAULT_CLONE_BASE)[0]);
        assert!(
            has_entry(&args, "api.githubAppExistingSecretKey=app-pem"),
            "the supplied data key was ignored: {args:?}"
        );
        assert!(
            !has_entry(&args, "api.githubAppExistingSecretKey=privateKey"),
            "the chart default was emitted over the supplied key: {args:?}"
        );
    }

    // ---- T3: the security property -----------------------------------------

    #[test]
    fn the_byo_connect_never_passes_the_pem_path_to_helm() {
        // THE security property of this path. `--set-file` makes helm read the
        // file and write its CONTENTS into the release, where every retained
        // revision keeps them and `helm get values` prints them back (#1236
        // found the PEM in revision 15 of a live release). On the BYO path the
        // release holds a Secret NAME only, so the PEM's path must not be in
        // the plan at all -- helm must never be told where the file is.
        //
        // These opts also carry a real key path, a combination
        // `require_connect_inputs` refuses. That is deliberate: the input
        // check must not be the only thing standing between a PEM and helm, so
        // the builder is proven to drop the path on its own.
        let (_dir, path) = key_fixture();
        let body = std::fs::read_to_string(&path).expect("fixture readable");
        let mut o = byo_opts();
        o.private_key_path = path.clone();

        let args = argv(&connect_commands(&o, DEFAULT_CLONE_BASE)[0]);
        assert!(
            !has_entry(&args, "--set-file"),
            "the BYO plan makes helm read a file off disk: {args:?}"
        );
        assert!(
            !args.iter().any(|a| a.contains(&path)),
            "the PEM's path reached the BYO plan: {args:?}"
        );
        assert!(
            !has_entry_starting(&args, "api.githubAppPrivateKey=/"),
            "the BYO plan carries a filesystem path as the key: {args:?}"
        );
        for line in body.lines().filter(|l| !l.trim().is_empty()) {
            assert!(
                !args.iter().any(|a| a.contains(line)),
                "key material reached argv: {line}"
            );
        }
    }

    // ---- T4: adopting BYO clears the inline key ----------------------------

    #[test]
    fn the_byo_connect_clears_the_inline_private_key() {
        // Decision 2, half one, and the whole reason the BYO path exists.
        // `--reuse-values` copies a still-set api.githubAppPrivateKey into
        // every future revision forever, and `curie cluster up` runs exactly
        // that. Adopting the recommended path while leaving the PEM in release
        // history gives the operator its ceremony and none of its benefit.
        let args = argv(&connect_commands(&byo_opts(), DEFAULT_CLONE_BASE)[0]);
        assert!(
            has_entry(&args, "api.githubAppPrivateKey="),
            "the inline key rides every later revision unless cleared: {args:?}"
        );
    }

    // ---- T5: the chart-held branch must not leak the BYO fields ------------

    #[test]
    fn the_chart_held_connect_never_mentions_the_byo_fields() {
        // Sibling path. If the branch leaks, a plain `--private-key` run writes
        // api.githubAppExistingSecret= into the release and, through
        // --reuse-values, permanently overrides an operator's hand-set BYO
        // reference -- silently moving a working install off the Secret their
        // External Secrets Operator owns.
        let args = argv(&connect_commands(&opts(false), DEFAULT_CLONE_BASE)[0]);
        assert!(
            !has_entry_starting(&args, "api.githubAppExistingSecret"),
            "the BYO branch leaked into the chart-held path: {args:?}"
        );
    }

    // ---- T6: the chart-held plan is unchanged ------------------------------

    #[test]
    fn the_chart_held_connect_plan_is_byte_identical_to_before() {
        // The chart-held path is what every existing install already runs;
        // this ticket adds a branch beside it and must not perturb it. An
        // exact whole-vector comparison pins order, flags, values and the
        // absence of any extra entry at once -- a `contains` sweep cannot see
        // an ADDED entry, which is exactly how a leaked BYO clear arrives.
        let args = argv(&connect_commands(&opts(false), DEFAULT_CLONE_BASE)[0]);
        assert_eq!(
            args,
            vec![
                "upgrade",
                "curie",
                "charts/curie",
                "-n",
                "curie",
                "--reuse-values",
                "--set-string",
                "api.githubAppId=12345",
                "--set-file",
                "api.githubAppPrivateKey=/tmp/app.pem",
                "--set",
                "api.githubCloneBase=https://github.com",
            ]
        );
    }

    // ---- T7 / T8: disconnect (AC3) ----------------------------------------

    #[test]
    fn disconnect_clears_the_byo_secret_name() {
        // AC3. Without this, `--disconnect` leaves api.githubAppExistingSecret
        // set: the CLI reports "GitHub App cleared", the chart still resolves
        // GITHUB_APP_PRIVATE_KEY from the operator's Secret, and the platform
        // keeps authenticating as an App the operator believes is gone.
        let args = argv(&disconnect_commands(&opts(true))[0]);
        assert!(
            has_entry(&args, "api.githubAppExistingSecret="),
            "the BYO Secret reference survived the disconnect: {args:?}"
        );
        assert!(
            has_entry(&args, "api.githubAppId="),
            "the App id was not cleared to empty: {args:?}"
        );
        assert!(
            has_entry(&args, "api.githubAppPrivateKey="),
            "the private key was not cleared to empty: {args:?}"
        );
    }

    #[test]
    fn disconnect_does_not_clear_the_byo_data_key_name() {
        // Decision 2, half two, and the test that stops a future "for
        // symmetry, clear both" refactor.
        //
        // api.githubAppExistingSecretKey has a chart default of `privateKey`.
        // Setting it to "" does NOT restore that default -- `--reuse-values`
        // re-supplies the empty string on every later upgrade, so the release
        // overrides the default permanently. An operator who later hand-sets
        // githubAppExistingSecret with no key then renders `key: ""`, and the
        // api pod sits in CreateContainerConfigError with nothing in the
        // release to explain why. The field is inert while the Secret NAME is
        // empty, so leaving it alone is both correct and strictly safer.
        let args = argv(&disconnect_commands(&opts(true))[0]);
        assert!(
            !has_entry_starting(&args, "api.githubAppExistingSecretKey"),
            "clearing the data key overrides the chart default forever: {args:?}"
        );
    }

    // ---- T9: --set-string, not --set ---------------------------------------

    #[test]
    fn an_all_digit_secret_name_and_data_key_are_set_as_strings() {
        // `1234567` is a valid RFC-1123 label and a valid Secret data key.
        // Under `--set`, helm parses it as a number and a --reuse-values round
        // trip stores it as a float64: the next upgrade renders
        // `1.234567e+06`, the secretKeyRef names a Secret that does not exist,
        // and the api pod never starts. This is #1236's App-ID float bug
        // transplanted into a new field, and a chart-render test cannot see it
        // because it only appears after a real round trip.
        let mut o = byo_opts();
        o.existing_secret = "1234567".into();
        o.existing_secret_key = "8901234".into();
        let args = argv(&connect_commands(&o, DEFAULT_CLONE_BASE)[0]);
        assert_eq!(
            flag_before(&args, "api.githubAppExistingSecret=1234567"),
            "--set-string",
            "an all-digit Secret name must not go through --set: {args:?}"
        );
        assert_eq!(
            flag_before(&args, "api.githubAppExistingSecretKey=8901234"),
            "--set-string",
            "an all-digit data key must not go through --set: {args:?}"
        );
    }

    // ---- T10: the AC2 guard ------------------------------------------------

    #[test]
    fn a_configured_byo_secret_refuses_a_chart_held_private_key() {
        // THIS IS THE TICKET. Without the guard the CLI prints "GitHub App
        // configured", returns {"github_app_configured": true} and rolls the
        // API, while the pod keeps signing with the OLD key -- because the
        // chart resolves GITHUB_APP_PRIVATE_KEY from the BYO Secret whenever
        // api.githubAppExistingSecret is non-empty, so --set-file writes a
        // value nothing reads. The README's next rotation step is "delete the
        // first key on GitHub", at which point every clone 401s and nothing
        // the CLI printed ever hinted at it.
        let existing = serde_json::json!({"api": {"githubAppExistingSecret": "my-github-app"}});
        let refusal = guard_byo_key_conflict(&opts(false), Some(&existing));
        let err = refusal.expect_err("a BYO release must refuse --private-key");
        let msg = err.to_string();
        assert!(
            msg.contains("my-github-app"),
            "the refusal must name the Secret the release actually reads: {msg}"
        );
        assert!(
            msg.contains("privateKey"),
            "the refusal must name the data key inside it: {msg}"
        );
        let fix = fix_of(&err);
        assert!(
            fix.contains("--existing-secret"),
            "the fix must name the way forward: {fix}"
        );
        assert!(
            fix.contains("--disconnect"),
            "the fix must name the way back: {fix}"
        );
        assert!(
            fix.contains("my-github-app"),
            "the fix must name the Secret the operator has to update: {fix}"
        );
    }

    #[test]
    fn a_present_but_empty_byo_secret_does_not_refuse() {
        // `--disconnect` writes api.githubAppExistingSecret="", so the key is
        // PRESENT and empty on every disconnected release. A guard that fires
        // on presence rather than on a non-empty value bricks the documented
        // recovery path: after a disconnect the operator could never return to
        // a chart-held key through the CLI at all.
        let existing = serde_json::json!({"api": {"githubAppExistingSecret": ""}});
        let outcome = guard_byo_key_conflict(&opts(false), Some(&existing));
        assert!(
            outcome.is_ok(),
            "an empty BYO reference is not a BYO release: {:?}",
            outcome.err()
        );
    }

    #[test]
    fn an_absent_release_does_not_refuse() {
        // `fetch_release_values` returns Ok(None) only when helm positively
        // reports the release does not exist. Refusing there would make the
        // verb unusable on a fresh install, and helm's own "release not found"
        // two lines later is the honest error.
        let outcome = guard_byo_key_conflict(&opts(false), None);
        assert!(
            outcome.is_ok(),
            "a release that does not exist configures nothing: {:?}",
            outcome.err()
        );
    }

    #[test]
    fn a_release_with_null_values_does_not_refuse() {
        // helm prints `null` for an existing release with no user-supplied
        // values -- the shape of a default install. Reading that as "a BYO
        // Secret is configured" would refuse the very first github-app run on
        // every such cluster.
        let existing = serde_json::Value::Null;
        let outcome = guard_byo_key_conflict(&opts(false), Some(&existing));
        assert!(
            outcome.is_ok(),
            "null values configure nothing: {:?}",
            outcome.err()
        );
    }

    #[test]
    fn a_custom_data_key_is_echoed_in_the_refusal() {
        // The operator must be told WHICH data key to update, not the chart
        // default. Naming `privateKey` when the release reads `app-pem` sends
        // them to write the PEM into a key nothing reads -- the same
        // misreport one layer down. Non-default value, per #1263.
        let existing = serde_json::json!({
            "api": {"githubAppExistingSecret": "s", "githubAppExistingSecretKey": "app-pem"}
        });
        let refusal = guard_byo_key_conflict(&opts(false), Some(&existing));
        let err = refusal.expect_err("a BYO release must refuse --private-key");
        let both = format!("{}\n{}", err, fix_of(&err));
        assert!(
            both.contains("app-pem"),
            "the refusal must name the release's own data key: {both}"
        );
        assert!(
            !both.contains("privateKey"),
            "the refusal named the chart default over the real key: {both}"
        );
    }

    #[test]
    fn the_guard_does_not_fire_on_an_explicit_byo_connect() {
        // Re-running `--existing-secret` on a BYO release IS the supported
        // rotation path: the operator updated the Secret and needs the rollout
        // this verb performs. A guard that refuses here leaves them with no
        // CLI way to roll the API at all.
        let existing = serde_json::json!({"api": {"githubAppExistingSecret": "my-github-app"}});
        let outcome = guard_byo_key_conflict(&byo_opts(), Some(&existing));
        assert!(
            outcome.is_ok(),
            "re-pointing at the same Secret is the rotation path: {:?}",
            outcome.err()
        );
    }

    #[test]
    fn the_guard_does_not_fire_on_disconnect() {
        // Clearing a reference must always be possible. A guard that refuses
        // `--disconnect` on a BYO release makes that release unrecoverable
        // through the CLI -- the operator would have to hand-run helm, which
        // is the thing this verb exists to avoid.
        let existing = serde_json::json!({"api": {"githubAppExistingSecret": "my-github-app"}});
        let outcome = guard_byo_key_conflict(&opts(true), Some(&existing));
        assert!(
            outcome.is_ok(),
            "a disconnect must never be blocked by what it clears: {:?}",
            outcome.err()
        );
    }

    #[test]
    fn only_a_chart_held_connect_pays_for_the_values_read() {
        // `needs_byo_conflict_check` decides whether the verb makes a `helm
        // get values` round trip at all. Answering `true` for a disconnect or
        // an explicit BYO connect adds a cluster read -- and on a real run a
        // hard failure when helm is unreachable -- to two paths that need
        // nothing from the release. Answering `false` for a chart-held connect
        // disables the guard entirely and restores the bug.
        assert!(
            needs_byo_conflict_check(&opts(false)),
            "the chart-held connect is the only path that can misreport"
        );
        assert!(
            !needs_byo_conflict_check(&opts(true)),
            "a disconnect must not pay for a values read"
        );
        assert!(
            !needs_byo_conflict_check(&byo_opts()),
            "an explicit BYO connect must not pay for a values read"
        );
    }

    #[test]
    fn the_configured_secret_is_read_with_the_charts_own_key_names() {
        // The three literals must be the exact strings
        // charts/curie/templates/api.yaml reads. A CLI that looked up a
        // different values key would resolve a different Secret than the
        // workload's own env and report a plausible but wrong answer, which is
        // worse than reporting none (#1759).
        let custom = serde_json::json!({
            "api": {"githubAppExistingSecret": "s", "githubAppExistingSecretKey": "app-pem"}
        });
        assert_eq!(
            configured_existing_secret(Some(&custom)),
            Some(("s".to_string(), "app-pem".to_string()))
        );
        let defaulted = serde_json::json!({"api": {"githubAppExistingSecret": "s"}});
        assert_eq!(
            configured_existing_secret(Some(&defaulted)),
            Some(("s".to_string(), DEFAULT_APP_KEY_DATA_KEY.to_string())),
            "an unset data key must fall back to the chart's own default"
        );
        assert_eq!(configured_existing_secret(None), None);
    }

    #[test]
    fn the_default_data_key_mirrors_the_chart_default() {
        // DEFAULT_APP_KEY_DATA_KEY mirrors charts/curie/values.yaml's
        // api.githubAppExistingSecretKey. If the two drift, `--existing-secret
        // X` with no `--existing-secret-key` writes a key name the chart never
        // defaults to, and the api pod fails to start on a Secret that is
        // perfectly correct.
        assert_eq!(DEFAULT_APP_KEY_DATA_KEY, "privateKey");
    }

    // ---- T11: input validation ---------------------------------------------

    #[test]
    fn missing_inputs_say_where_to_find_them() {
        let mut o = opts(false);
        o.app_id = String::new();
        let err = require_connect_inputs(&o).unwrap_err();
        assert!(err.to_string().contains("Developer settings"));

        let mut o = opts(false);
        o.private_key_path = String::new();
        let err = require_connect_inputs(&o).unwrap_err();
        assert!(err.to_string().contains("Private keys"));
    }

    #[test]
    fn a_key_path_that_does_not_exist_fails_before_helm_runs() {
        // helm's own error for a missing --set-file is opaque, and by then the
        // upgrade has already started.
        let mut o = opts(false);
        o.private_key_path = "/nope/missing.pem".into();
        let err = require_connect_inputs(&o).unwrap_err();
        assert!(err.to_string().contains("no such file"));
    }

    /// Both halves of the #1261 contract for one refusal, checked through the
    /// very `exit::classify` and `exit::error_json` the `--json` error emitter
    /// uses: it classifies as Usage (exit 2), and it carries a non-null `fix`
    /// naming the flag to correct. A revert to `bail!` classifies as
    /// (Failure, None), so the class assertion fails and so does the `expect`.
    fn assert_usage_with_a_fix_naming(err: &anyhow::Error, flag: &str, case: &str) {
        let (class, fix) = crate::exit::classify(err);
        assert_eq!(
            class,
            crate::exit::ExitClass::Usage,
            "a deterministic input error must exit 2 ({case}): {err:#}"
        );
        let fix = fix.expect("a usage refusal must carry a fix, not a null one");
        assert!(!fix.trim().is_empty(), "the fix must not be empty ({case})");
        assert!(
            fix.contains(flag),
            "the fix must name the offending flag {flag} ({case}): {fix}"
        );
        let json = crate::exit::error_json(err);
        assert!(
            json["fix"] != serde_json::Value::Null,
            "the rendered payload must not have a null fix ({case}): {json}"
        );
    }

    // Each of the three arms is a deterministic input error: the same argv fails
    // identically, so it is exit 2 and must name the flag to fix (#1261).
    #[test]
    fn each_missing_input_is_a_usage_error_with_a_flag_naming_fix() {
        for (app_id, key_path, flag) in [
            ("", "/tmp/app.pem", "--app-id"),
            ("1", "", "--private-key"),
            ("1", "/nope/missing.pem", "--private-key"),
        ] {
            let mut o = opts(false);
            o.app_id = app_id.into();
            o.private_key_path = key_path.into();
            let err = require_connect_inputs(&o).unwrap_err();
            assert_usage_with_a_fix_naming(
                &err,
                flag,
                &format!("--app-id {app_id:?}, --private-key {key_path:?}"),
            );
        }
    }

    // The five refusals #1255 adds are the same category as the three above:
    // argv-only input errors where the identical argv fails identically. Class
    // them as the retryable exit 1 and the agent driving the CLI is told to
    // retry a command that can never succeed (#1261). The two refusals that
    // stay `failure` -- `guard_byo_key_conflict` and the non-string field --
    // are deliberately absent: they judge the deployed release, not the argv.
    #[test]
    fn each_new_flag_refusal_is_a_usage_error_with_a_flag_naming_fix() {
        let (_dir, path) = key_fixture();

        let mut disconnect_and_byo = opts(true);
        disconnect_and_byo.existing_secret = "my-github-app".into();

        let mut injected_name = byo_opts();
        injected_name.existing_secret = "my-github-app,api.githubAppId=999".into();

        let mut injected_data_key = byo_opts();
        injected_data_key.existing_secret_key = "privateKey,api.githubAppExistingSecret=".into();

        let mut both_key_sources = byo_opts();
        both_key_sources.private_key_path = path;

        let mut orphan_data_key = opts(false);
        orphan_data_key.existing_secret_key = "app-pem".into();

        for (case, o, flag) in [
            (
                "--disconnect with a Secret name",
                disconnect_and_byo,
                "--existing-secret",
            ),
            (
                "a Secret name that is not an RFC-1123 subdomain",
                injected_name,
                "--existing-secret",
            ),
            (
                "a data key that is not a Secret data key",
                injected_data_key,
                "--existing-secret-key",
            ),
            (
                "both ways of supplying the key at once",
                both_key_sources,
                "--private-key",
            ),
            (
                "a data key with no Secret name",
                orphan_data_key,
                "--existing-secret",
            ),
        ] {
            let err = require_connect_inputs(&o).expect_err(&format!("{case} must be refused"));
            assert_usage_with_a_fix_naming(&err, flag, case);
        }
    }

    #[test]
    fn disconnect_needs_no_inputs() {
        let mut o = opts(true);
        o.app_id = String::new();
        o.private_key_path = String::new();
        assert!(require_connect_inputs(&o).is_ok());
    }

    #[test]
    fn existing_secret_with_a_private_key_is_refused() {
        // Two mutually exclusive ways to supply one key. Picking one silently
        // is a guess about operator intent on the single credential that can
        // mint read tokens for every repository in the installation -- and
        // whichever we guessed, the other would look configured and not be.
        let (_dir, path) = key_fixture();
        let mut o = byo_opts();
        o.private_key_path = path;
        let err = require_connect_inputs(&o).unwrap_err();
        let msg = err.to_string();
        assert!(
            msg.contains("--existing-secret"),
            "the refusal must name the flag that was accepted: {msg}"
        );
        assert!(
            msg.contains("--private-key"),
            "the refusal must name the flag that was ignored: {msg}"
        );
    }

    #[test]
    fn existing_secret_with_disconnect_is_refused() {
        // "--disconnect --existing-secret X" reads as "point at X while
        // disconnecting". Accepting it clears the release and leaves the
        // operator believing a reference to X was set.
        let mut o = opts(true);
        o.existing_secret = "my-github-app".into();
        let err = require_connect_inputs(&o).unwrap_err();
        let msg = err.to_string();
        assert!(
            msg.contains("--existing-secret"),
            "the refusal must name the contradicting flag: {msg}"
        );
        assert!(
            msg.contains("--disconnect"),
            "the refusal must name what it contradicts: {msg}"
        );
    }

    #[test]
    fn a_data_key_without_a_secret_name_is_refused() {
        // A non-default data key with no Secret name configures nothing at
        // all: the BYO branch never runs, and the operator who typed
        // `--existing-secret-key app-pem` gets a chart-held connect reported
        // as success. Silently doing nothing is this ticket's own defect
        // class, so it must not be reintroduced by the new flag pair.
        let mut o = opts(false);
        o.existing_secret_key = "app-pem".into();
        let outcome = require_connect_inputs(&o);
        assert!(
            outcome.is_err(),
            "a data key with no Secret name configures nothing"
        );

        // The DEFAULT key with no Secret name is the ordinary chart-held run
        // and must stay accepted, or every existing invocation breaks.
        let (_dir, path) = key_fixture();
        let mut o = opts(false);
        o.private_key_path = path;
        let outcome = require_connect_inputs(&o);
        assert!(
            outcome.is_ok(),
            "the chart-held default must stay accepted: {:?}",
            outcome.err()
        );
    }

    #[test]
    fn the_app_id_is_still_required_on_the_byo_path() {
        // The chart needs BOTH githubAppId and a key; the App id is not secret
        // and "set both, or neither" is the existing contract. Without the id
        // the JWT carries no `iss` and every GitHub call 401s -- with a
        // perfectly configured Secret sitting right there.
        let mut o = byo_opts();
        o.app_id = String::new();
        let err = require_connect_inputs(&o).unwrap_err();
        assert!(err.to_string().contains("Developer settings"));
    }

    #[test]
    fn a_private_key_is_not_required_on_the_byo_path() {
        // Directly falsifies "we forgot to move the two chart-held checks
        // under the branch". Left where they are, the empty path trips
        // "--private-key is required" (and then the is_file check), so EVERY
        // BYO invocation dies before helm ever runs and the recommended path
        // stays unreachable from the CLI -- which is this ticket.
        let outcome = require_connect_inputs(&byo_opts());
        assert!(
            outcome.is_ok(),
            "the BYO path supplies no PEM by design: {:?}",
            outcome.err()
        );
    }

    #[test]
    fn an_empty_existing_secret_degrades_to_the_chart_held_path() {
        // `--existing-secret ""` is indistinguishable from omitting it. There
        // is no third mode: it must still require --private-key and still emit
        // --set-file, rather than take the BYO branch with an empty name and
        // write api.githubAppExistingSecret= over an operator's real one.
        let mut o = opts(false);
        o.existing_secret = String::new();
        o.private_key_path = String::new();
        let err = require_connect_inputs(&o).unwrap_err();
        assert!(err.to_string().contains("Private keys"), "{err}");

        let args = argv(&connect_commands(&opts(false), DEFAULT_CLONE_BASE)[0]);
        assert!(
            has_entry(&args, "--set-file"),
            "an empty --existing-secret must still read the PEM: {args:?}"
        );
        assert!(
            !has_entry_starting(&args, "api.githubAppExistingSecret"),
            "an empty --existing-secret took the BYO branch: {args:?}"
        );
    }

    // ---- T12: the new flags are Kubernetes syntax, not helm expressions -----

    #[test]
    fn the_comma_injection_through_the_data_key_is_refused() {
        // The literal attack from the #1255 review. `--set-string` stops helm
        // TYPING a value, but helm still splits the expression on commas
        // STRUCTURALLY: this one argv entry is read as TWO assignments, and the
        // second blanks api.githubAppExistingSecret that the entry before it
        // just set. The run then also clears the inline private key, rolls the
        // API and reports success on a release with NO usable key at all -- and
        // k8s never gets to reject an invalid name, because the injected
        // assignment blanked the field before it could ever render.
        let mut o = byo_opts();
        o.existing_secret_key = "privateKey,api.githubAppExistingSecret=".into();
        let err = require_connect_inputs(&o).unwrap_err();
        let msg = err.to_string();
        assert!(
            msg.contains("--existing-secret-key"),
            "the refusal must name the offending flag: {msg}"
        );
        assert!(
            msg.contains("[-._a-zA-Z0-9]"),
            "the refusal must say what the allowed form is: {msg}"
        );
    }

    #[test]
    fn a_comma_in_the_secret_name_is_refused() {
        // Same injection, other flag. Here the second assignment would point
        // the chart-held key at an attacker-chosen path via --set-string, so
        // the Secret NAME field is no less structural than the data key.
        let mut o = byo_opts();
        o.existing_secret = "my-github-app,api.githubAppId=999".into();
        let err = require_connect_inputs(&o).unwrap_err();
        let msg = err.to_string();
        assert!(
            msg.contains("--existing-secret"),
            "the refusal must name the offending flag: {msg}"
        );
        assert!(
            msg.contains("RFC-1123"),
            "the refusal must say what the allowed form is: {msg}"
        );
    }

    #[test]
    fn realistic_secret_names_and_data_keys_are_accepted() {
        // The other half of the validator's contract, and the more dangerous
        // half to get wrong: a validator that rejected a LEGAL Kubernetes name
        // would break real installs while looking like hardening. A dotted
        // Secret name and a data key carrying '.', '_' and '-' are both
        // ordinary, and so is the all-digit pair #1236's float coercion is
        // about -- none of them may be refused.
        for (name, key) in [
            ("curie.github-app.prod", "app.private-key_2026"),
            ("1234567", "8901234"),
            ("my-github-app", DEFAULT_APP_KEY_DATA_KEY),
        ] {
            let mut o = byo_opts();
            o.existing_secret = name.into();
            o.existing_secret_key = key.into();
            assert!(
                require_connect_inputs(&o).is_ok(),
                "a legal Secret name/key pair was refused ({name}, {key}): {:?}",
                require_connect_inputs(&o).err()
            );
            let args = argv(&connect_commands(&o, DEFAULT_CLONE_BASE)[0]);
            assert!(
                has_entry(&args, &format!("api.githubAppExistingSecret={name}")),
                "the accepted Secret name did not reach helm verbatim: {args:?}"
            );
            assert!(
                has_entry(&args, &format!("api.githubAppExistingSecretKey={key}")),
                "the accepted data key did not reach helm verbatim: {args:?}"
            );
        }
    }

    #[test]
    fn a_pem_pasted_into_existing_secret_is_refused_without_echoing_it() {
        // --existing-secret and --private-key sit next to each other in --help,
        // so the PEM lands in the wrong one eventually. It must be refused (it
        // is not a Secret name), the refusal must point at the flag that DOES
        // take a PEM, and it must not print the key material back into the
        // terminal, the shell history or the --json error payload.
        let (_dir, path) = key_fixture();
        let body = std::fs::read_to_string(&path).expect("fixture readable");
        let mut o = byo_opts();
        o.existing_secret = body.clone();
        let err = require_connect_inputs(&o).unwrap_err();
        let msg = err.to_string();
        assert!(
            msg.contains("--private-key"),
            "the refusal must name the flag that takes a PEM: {msg}"
        );
        for line in body.lines().filter(|l| !l.trim().is_empty()) {
            assert!(
                !msg.contains(line),
                "key material was echoed back in the refusal: {line}"
            );
        }
    }

    // ---- T13: the guard fails CLOSED on a non-string stored value -----------

    #[test]
    fn a_truthy_non_string_byo_field_refuses_rather_than_guessing() {
        // The guard's inputs come from `helm get values -o json`, and helm
        // stores what it was given: `--set api.githubAppExistingSecret=true`
        // lands as a bool, and an all-digit Secret name lands as a float64
        // (#1236). The chart's BYO branch is plain truthiness, so both ARE
        // reading the operator's Secret -- but a guard that reads the leaf with
        // .as_str() sees None and concludes "no BYO configured". It then writes
        // an ignored PEM, rolls the API and reports success over the OLD key,
        // which is #1255 itself.
        for value in [
            serde_json::json!(true),
            serde_json::json!(42),
            serde_json::json!(1234567.0),
        ] {
            let existing = serde_json::json!({"api": {"githubAppExistingSecret": value}});
            let err = guard_byo_key_conflict(&opts(false), Some(&existing))
                .expect_err("a truthy non-string BYO field must refuse");
            let msg = err.to_string();
            assert!(
                msg.contains("api.githubAppExistingSecret"),
                "the refusal must name the field the operator has to fix: {msg}"
            );
            let fix = fix_of(&err);
            assert!(
                fix.contains("--set-string"),
                "the fix must name the way to make it a string: {fix}"
            );
            assert!(
                fix.contains("--disconnect"),
                "the fix must name the way back: {fix}"
            );
        }
    }

    #[test]
    fn a_falsy_byo_field_does_not_refuse() {
        // The mirror obligation, and the one that keeps the fail-closed rule
        // from bricking a legitimate rotation. `false`, `0` and `""` are all
        // FALSY to the chart's `{{- if .Values.api.githubAppExistingSecret }}`,
        // so the release genuinely is on the chart-held key and --private-key
        // is exactly the right command. Refusing here would leave an operator
        // with no CLI way to rotate at all.
        for value in [
            serde_json::json!(false),
            serde_json::json!(0),
            serde_json::json!(0.0),
            serde_json::json!(""),
            serde_json::Value::Null,
        ] {
            let existing = serde_json::json!({"api": {"githubAppExistingSecret": value}});
            let outcome = guard_byo_key_conflict(&opts(false), Some(&existing));
            assert!(
                outcome.is_ok(),
                "a value the chart treats as falsy is not a BYO release: {:?}",
                outcome.err()
            );
        }
    }

    #[test]
    fn a_string_byo_field_still_resolves_exactly_as_before() {
        // Fail-closed must not disturb the ordinary path: a non-empty string
        // still delegates to resolve_existing_secret_ref, and the refusal still
        // names the real Secret and the real data key rather than the generic
        // non-string message.
        let existing = serde_json::json!({
            "api": {
                "githubAppExistingSecret": "my-github-app",
                "githubAppExistingSecretKey": "app-pem"
            }
        });
        assert_eq!(
            classify_existing_secret_field(Some(&existing)),
            ByoSecretField::Named
        );
        assert_eq!(
            configured_existing_secret(Some(&existing)),
            Some(("my-github-app".to_string(), "app-pem".to_string()))
        );
        let err = guard_byo_key_conflict(&opts(false), Some(&existing))
            .expect_err("a BYO release must refuse --private-key");
        let msg = err.to_string();
        assert!(
            msg.contains("my-github-app") && msg.contains("app-pem"),
            "the string path must still name the Secret and its data key: {msg}"
        );
    }

    // ---- T14: --dry-run is offline --------------------------------------

    #[test]
    fn a_dry_run_never_reads_the_release() {
        // cli/CLAUDE.md: pure argv builders never fetch, and --dry-run never
        // touches the network. A best-effort `helm get values` under --dry-run
        // also degrades silently, so automation cannot tell a conflict-checked
        // plan from one whose check was skipped because the read failed --
        // false assurance, which is worse than none. The guard still runs on
        // the real invocation, before any mutation.
        //
        // `opts(_)` is already a dry run; the real-run cases flip the flag.
        assert!(
            !needs_release_read(&opts(false)),
            "a dry run must make no cluster read at all"
        );
        let mut real = opts(false);
        real.common.dry_run = false;
        assert!(
            needs_release_read(&real),
            "a real chart-held connect is the one path that must read the release"
        );
        let mut real_byo = byo_opts();
        real_byo.common.dry_run = false;
        assert!(
            !needs_release_read(&real_byo),
            "an explicit BYO connect needs nothing from the release"
        );
        let mut real_disconnect = opts(true);
        real_disconnect.common.dry_run = false;
        assert!(
            !needs_release_read(&real_disconnect),
            "a disconnect needs nothing from the release"
        );
        // What a dry run therefore hands the guard, and the guard's answer to
        // it: no read, no refusal, an offline plan.
        assert!(
            guard_byo_key_conflict(&opts(false), None).is_ok(),
            "a dry run must still produce a plan with no release knowledge"
        );
    }
}
