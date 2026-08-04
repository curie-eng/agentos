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

use anyhow::{bail, Result};

use crate::ops::{plain, require_on_path, run_step, CommonOpts, OpsCommand};

#[derive(Debug, Clone)]
pub struct GithubAppOpts {
    pub common: CommonOpts,
    pub chart: String,
    /// The App's numeric id, from its settings page. Not secret.
    pub app_id: String,
    /// Path to the App's PEM private key. The path, never the contents.
    pub private_key_path: String,
    /// Clear the App credentials and fall back to `api.githubToken`.
    pub disconnect: bool,
}

/// Where the platform clones from. Set alongside the App because an empty base
/// makes git-flow fail before it ever reaches a credential -- it derives
/// `<base>/<repo>.git`, and with no base that is a path with no scheme, which
/// is rejected as a configuration error. An operator wiring up the App has
/// exactly the wrong context to debug that, so we set both together.
pub const DEFAULT_CLONE_BASE: &str = "https://github.com";

pub fn connect_commands(opts: &GithubAppOpts, clone_base: &str) -> Vec<OpsCommand> {
    vec![OpsCommand::new(
        "helm",
        vec![
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
            // The key's CONTENTS never enter argv; helm reads the file itself.
            plain("--set-file"),
            plain(format!("api.githubAppPrivateKey={}", opts.private_key_path)),
            plain("--set"),
            plain(format!("api.githubCloneBase={clone_base}")),
        ],
    )]
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
    require_connect_inputs(opts.disconnect, &opts.app_id, &opts.private_key_path)?;

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

    require_on_path("helm")?;
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

pub fn require_connect_inputs(disconnect: bool, app_id: &str, key_path: &str) -> Result<()> {
    if disconnect {
        return Ok(());
    }
    if app_id.trim().is_empty() {
        bail!(
            "--app-id is required. Find it on the App's settings page \
             (Settings -> Developer settings -> GitHub Apps -> your app)."
        );
    }
    if key_path.trim().is_empty() {
        bail!(
            "--private-key is required: the path to the App's PEM file, \
             downloaded from the App's settings page under 'Private keys'."
        );
    }
    if !std::path::Path::new(key_path).is_file() {
        bail!("--private-key: no such file: {key_path}");
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
            disconnect,
        }
    }

    fn argv(cmd: &OpsCommand) -> Vec<String> {
        cmd.argv()
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

    #[test]
    fn the_private_key_contents_never_reach_argv() {
        // The whole reason for --set-file. A PEM in argv is readable by `ps`
        // and can be echoed by a subprocess error.
        let cmds = connect_commands(&opts(false), DEFAULT_CLONE_BASE);
        let flat = argv(&cmds[0]).join(" ");
        assert!(flat.contains("--set-file"));
        assert!(flat.contains("api.githubAppPrivateKey=/tmp/app.pem"));
        assert!(!flat.contains("BEGIN"));
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
    fn disconnect_clears_both_app_fields_and_touches_nothing_else() {
        let flat = argv(&disconnect_commands(&opts(true))[0]).join(" ");
        assert!(flat.contains("api.githubAppId="));
        assert!(flat.contains("api.githubAppPrivateKey="));
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

    #[test]
    fn missing_inputs_say_where_to_find_them() {
        let err = require_connect_inputs(false, "", "/tmp/app.pem").unwrap_err();
        assert!(err.to_string().contains("Developer settings"));
        let err = require_connect_inputs(false, "1", "").unwrap_err();
        assert!(err.to_string().contains("Private keys"));
    }

    #[test]
    fn a_key_path_that_does_not_exist_fails_before_helm_runs() {
        // helm's own error for a missing --set-file is opaque, and by then the
        // upgrade has already started.
        let err = require_connect_inputs(false, "1", "/nope/missing.pem").unwrap_err();
        assert!(err.to_string().contains("no such file"));
    }

    #[test]
    fn disconnect_needs_no_inputs() {
        assert!(require_connect_inputs(true, "", "").is_ok());
    }
}
