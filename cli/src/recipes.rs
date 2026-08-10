//! The TUI recipe catalog: what the interactive surface offers.
//!
//! This module owns the data ("what the surface offers") -- the recipe entries,
//! their argument shapes, and the pure `Recipe` -> argv transform. How a recipe
//! is drawn, prompted, and run stays in `interactive.rs`.

use std::collections::BTreeMap;

#[derive(Clone, Debug)]
pub(crate) struct Recipe {
    pub(crate) tabs: &'static [&'static str],
    pub(crate) title: &'static str,
    pub(crate) description: &'static str,
    pub(crate) kind: RecipeKind,
    pub(crate) args: Vec<ArgPart>,
    pub(crate) fields: Vec<Field>,
    pub(crate) notes: &'static [&'static str],
}

#[derive(Clone, Debug)]
pub(crate) enum RecipeKind {
    Command,
    Tui(TuiAction),
    Workflow(Workflow),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum TuiAction {
    SaveSecret,
    ListSecrets,
    RemoveSecret,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum Workflow {
    ExploreExamples,
    ParityLadder,
    DeployToSlack,
}

/// Which platform tier a tier-bearing recipe or workflow drives.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Tier {
    Local,
    Cluster,
}

impl Tier {
    pub(crate) fn verb(self) -> &'static str {
        match self {
            Tier::Local => "local",
            Tier::Cluster => "cluster",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum ArgPart {
    Literal(&'static str),
    /// The tier verb (`local` / `cluster`), resolved from `build_argv`'s
    /// explicit `tier` argument.
    Tier,
    Field(&'static str),
    OptionalFlag {
        flag: &'static str,
        field: &'static str,
    },
}

#[derive(Clone, Debug)]
pub(crate) struct Field {
    pub(crate) key: &'static str,
    pub(crate) label: &'static str,
    pub(crate) default: Option<&'static str>,
    pub(crate) required: bool,
}

pub(crate) fn build_argv(
    recipe: &Recipe,
    tier: Option<Tier>,
    values: &BTreeMap<String, String>,
) -> Vec<String> {
    let mut argv = Vec::new();
    for part in &recipe.args {
        match part {
            ArgPart::Literal(value) => argv.push((*value).to_string()),
            // `None` means no tier answered yet, which today is only the
            // read-only detail-pane preview: render the placeholder so the argv
            // is visibly non-executable rather than a plausible but wrong tier.
            ArgPart::Tier => argv.push(match tier {
                Some(tier) => tier.verb().to_string(),
                None => "<local|cluster>".to_string(),
            }),
            ArgPart::Field(field) => {
                if let Some(value) = values.get(*field) {
                    if !value.is_empty() {
                        argv.push(value.clone());
                    }
                }
            }
            ArgPart::OptionalFlag { flag, field } => {
                if let Some(value) = values.get(*field) {
                    if !value.is_empty() {
                        argv.push((*flag).to_string());
                        argv.push(value.clone());
                    }
                }
            }
        }
    }
    argv
}

/// Placeholder-filled argv for every `Command` recipe -- the seam the CLI/TUI
/// recipe parity gate test consumes to compare the TUI surface against the clap
/// grammar without exposing the whole `Recipe` type surface.
///
/// A tier-bearing recipe expands into one entry per tier, so the gate execs the
/// cluster path too instead of only ever proving the local one.
#[doc(hidden)]
pub fn command_recipe_argvs() -> Vec<(&'static str, Vec<String>)> {
    let mut entries = Vec::new();
    for recipe in recipes()
        .into_iter()
        .filter(|recipe| matches!(recipe.kind, RecipeKind::Command))
    {
        let mut values = BTreeMap::new();
        for field in &recipe.fields {
            values.insert(
                field.key.to_string(),
                field.default.unwrap_or("1").to_string(),
            );
        }
        if recipe.args.contains(&ArgPart::Tier) {
            for tier in [Tier::Local, Tier::Cluster] {
                entries.push((recipe.title, build_argv(&recipe, Some(tier), &values)));
            }
        } else {
            entries.push((recipe.title, build_argv(&recipe, None, &values)));
        }
    }
    entries
}

pub(crate) fn recipes() -> Vec<Recipe> {
    vec![
        // --- Platform: the primary product functions, leading the TUI ---
        Recipe {
            tabs: &["platform"],
            title: "Parity ladder (what curie is)",
            description: "One bundle + one eval suite across skill -> local -> cluster.",
            kind: RecipeKind::Workflow(Workflow::ParityLadder),
            args: vec![],
            fields: vec![],
            notes: &[
                "Read this first: the platform is about running the SAME artifact everywhere and evaluating/observing/governing it.",
            ],
        },
        Recipe {
            tabs: &["platform"],
            title: "Run evals (parity gate)",
            description: "Grade the bundle's evals/cases.json against the running skill runner.",
            kind: RecipeKind::Command,
            args: vec![ArgPart::Literal("skill"), ArgPart::Literal("eval")],
            fields: vec![],
            notes: &["Requires a runner up (Start runner / skill up) with the bundle's evals/cases.json."],
        },
        Recipe {
            tabs: &["platform"],
            title: "Open observability (Console + Langfuse)",
            description: "Open the local Curie Console and Langfuse traces/cost UIs.",
            kind: RecipeKind::Command,
            // Local-only on purpose: there is no `cluster observability` verb in
            // the clap grammar yet (the cluster twin is tracked as issue #460).
            args: vec![ArgPart::Literal("local"), ArgPart::Literal("observability")],
            fields: vec![],
            notes: &["Start the platform first with `curie local up`."],
        },
        Recipe {
            tabs: &["platform"],
            title: "List versions",
            description: "Show an agent's immutable deployed versions (newest first).",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Tier,
                ArgPart::Literal("versions"),
                ArgPart::Field("agent"),
            ],
            fields: vec![Field {
                key: "agent",
                label: "Agent name",
                default: None,
                required: true,
            }],
            notes: &["Every deploy pins a new immutable version; a thread keeps the one it booted with."],
        },
        Recipe {
            tabs: &["platform"],
            title: "Set budget",
            description: "Set an agent's daily USD spend cap (enforced per run).",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Tier,
                ArgPart::Literal("budget"),
                ArgPart::Field("agent"),
                ArgPart::Literal("--limit"),
                ArgPart::Field("limit"),
            ],
            fields: vec![
                Field {
                    key: "agent",
                    label: "Agent name",
                    default: None,
                    required: true,
                },
                Field {
                    key: "limit",
                    label: "Daily cap in USD (e.g. 5)",
                    default: None,
                    required: true,
                },
            ],
            notes: &[],
        },
        Recipe {
            tabs: &["platform"],
            title: "Gate a tool (approvals)",
            description: "Require human approval before an agent may call a named tool.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Tier,
                ArgPart::Literal("approvals"),
                ArgPart::Field("agent"),
                ArgPart::OptionalFlag {
                    flag: "--gate",
                    field: "tool",
                },
            ],
            fields: vec![
                Field {
                    key: "agent",
                    label: "Agent name",
                    default: None,
                    required: true,
                },
                Field {
                    key: "tool",
                    label: "Tool to gate (blank = show current gates)",
                    default: None,
                    required: false,
                },
            ],
            notes: &["e.g. gate mcp__plugin_github-issues_github__create_issue so a write pauses for approval."],
        },
        // The sibling of the gate recipe above, and the reason both exist: the
        // gate one configures WHICH calls pause, this one shows the pending
        // decisions that pausing produced. Offering only the first reads as
        // "approvals means tool gating", which is the misreading `curie guide`'s
        // approvals section exists to correct.
        Recipe {
            tabs: &["platform"],
            title: "Review pending approvals",
            description: "List the human decisions an agent is currently paused on.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Tier,
                ArgPart::Literal("approvals"),
                ArgPart::Field("agent"),
                ArgPart::Literal("--list"),
            ],
            fields: vec![Field {
                key: "agent",
                label: "Agent name",
                default: None,
                required: true,
            }],
            notes: &[
                "Each record reports its id and `card_channel`, the channel its route bound the card to; pass that as `--actor-channel` when resolving: `--resolve <id> --as <user> --actor-channel <card_channel>`.",
                "A null `card_channel` means the record names no route, so the REQUESTING channel is the approver set; pass that one instead.",
                "The server blocks self-approval, so the resolving actor must not be the person whose turn raised the request.",
            ],
        },
        // #1051 item 4, landed by #1086: the end-to-end flow, not another
        // single verb. The two recipes above configure a gate and read what it
        // produced; neither shows an agent author how a declaration becomes a
        // resolved card. That path was not expressible without curl until
        // #1057's --route flags merged, which is why the catalog carried only
        // the --list step.
        //
        // Anchored on the bind because that is the step an operator types and
        // the one the flags are new for; the declaration before it and the
        // drive/resolve after it are ordered in the notes. Deliberately a
        // Command rather than a Workflow: a Workflow is an interactive TUI flow
        // in interactive.rs, which is a feature, not the primer completion this
        // issue is.
        Recipe {
            tabs: &["platform"],
            title: "Approvals end to end (declare, bind, drive, resolve)",
            description: "Take a gated tool from its bundle declaration to a resolved card, with no curl.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Tier,
                ArgPart::Literal("approvals"),
                ArgPart::Field("agent"),
                ArgPart::Literal("--route"),
                ArgPart::Field("route"),
            ],
            fields: vec![
                Field {
                    key: "agent",
                    label: "Agent name",
                    default: None,
                    required: true,
                },
                Field {
                    key: "route",
                    label: "Route binding (name=channel)",
                    default: Some("managers=C0EXAMPLE1"),
                    required: true,
                },
            ],
            notes: &[
                "1. Declare the gate in the bundle: .claude-plugin/plugin.json, approvalPolicy.gates[] = {gate: <tool name>, route: <route name>}. Both keys are required and an incomplete gate is rejected at deploy.",
                "2. Deploy it: `deploy --plugin-dir <dir> --slack-channel <C...>`. The declaration reaches the platform with the bundle, not separately.",
                "3. Bind the route (the command above). `channel` is WHERE the card posts. Add `--route-approvers <name>=users:U1,U2` to say WHO may act; without it the card channel's members are the approver set.",
                "4. Drive a turn that calls the gated tool. The call is denied before it runs and the turn ends awaiting approval, with a card in the bound channel.",
                "5. Resolve it: `approvals <AGENT> --list` for the id, then `--resolve <id> --as <user> --actor-channel <channel>`. Self-approval is refused, so `--as` must name someone other than the author of the turn.",
                "A route write REPLACES the whole map, so pass every route the agent should keep. `--list-routes` shows what is bound now.",
            ],
        },
        Recipe {
            tabs: &["platform"],
            title: "Inspect memory",
            description: "Show what an agent has learned (its memory log).",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Tier,
                ArgPart::Literal("memory"),
                ArgPart::Field("agent"),
            ],
            fields: vec![Field {
                key: "agent",
                label: "Agent name",
                default: None,
                required: true,
            }],
            notes: &[],
        },
        Recipe {
            tabs: &["platform"],
            title: "Kill an agent",
            description: "Stop an agent's runs (the kill switch).",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Tier,
                ArgPart::Literal("kill"),
                ArgPart::Field("agent"),
                ArgPart::Literal("--yes"),
            ],
            fields: vec![Field {
                key: "agent",
                label: "Agent name",
                default: None,
                required: true,
            }],
            notes: &["Resume it later with the 'Resume an agent' action."],
        },
        Recipe {
            tabs: &["platform"],
            title: "Resume an agent",
            description: "Bring a killed agent back online.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Tier,
                ArgPart::Literal("resume"),
                ArgPart::Field("agent"),
            ],
            fields: vec![Field {
                key: "agent",
                label: "Agent name",
                default: None,
                required: true,
            }],
            notes: &[],
        },
        Recipe {
            tabs: &["skill"],
            title: "Start runner",
            description: "Boot the current plugin bundle in a local runner container.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("skill"),
                ArgPart::Literal("up"),
                ArgPart::OptionalFlag {
                    flag: "--plugin-dir",
                    field: "plugin_dir",
                },
                ArgPart::OptionalFlag {
                    flag: "--model",
                    field: "model",
                },
            ],
            fields: vec![
                Field {
                    key: "plugin_dir",
                    label: "Plugin directory",
                    default: Some("."),
                    required: false,
                },
                Field {
                    key: "model",
                    label: "Model id (optional)",
                    default: None,
                    required: false,
                },
            ],
            notes: &[
                "Use --fake-model or --local-model from the regular CLI when you need those modes.",
            ],
        },
        Recipe {
            tabs: &["skill"],
            title: "Send skill message",
            description: "Send a synthetic event to the local runner and stream the reply.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("skill"),
                ArgPart::Literal("message"),
                ArgPart::Field("text"),
            ],
            fields: vec![Field {
                key: "text",
                label: "Message text",
                default: None,
                required: true,
            }],
            notes: &["Requires a running `curie skill up` session."],
        },
        Recipe {
            tabs: &["skill"],
            title: "Run skill eval",
            description: "Run evals/cases.json through the local runner.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("skill"),
                ArgPart::Literal("eval"),
                ArgPart::OptionalFlag {
                    flag: "--cases",
                    field: "cases",
                },
            ],
            fields: vec![Field {
                key: "cases",
                label: "Cases file",
                default: Some("evals/cases.json"),
                required: false,
            }],
            notes: &[],
        },
        Recipe {
            // Empty tabs = reachable only from the All tab (no tier tab lists it).
            tabs: &[],
            title: "Explore examples",
            description: "Choose an example agent, start it, and chat with it interactively.",
            kind: RecipeKind::Workflow(Workflow::ExploreExamples),
            args: vec![],
            fields: vec![],
            notes: &[
                "Requires a saved or environment model credential: ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, or CURIE_CREDENTIALS.",
                "Examples request any additional credentials they need after you choose one.",
                "The runner stays up for a multi-turn conversation and stops when you leave chat.",
            ],
        },
        Recipe {
            tabs: &["local", "cluster"],
            title: "How to deploy to Slack",
            description: "Deploy an agent to a platform tier and connect it to a real Slack workspace.",
            kind: RecipeKind::Workflow(Workflow::DeployToSlack),
            args: vec![],
            fields: vec![],
            notes: &[
                "Asks whether to target the local platform or a deployed cluster release first.",
                "Creating the Slack app is a one-time manual step; the workflow gives you the manifest path and links.",
                "Requires your Slack app (xapp-) + bot (xoxb-) tokens, saved when prompted (plus a model credential for local).",
            ],
        },
        Recipe {
            tabs: &["secrets"],
            title: "Save secret",
            description: "Store a local secret in Curie private storage with hidden input.",
            kind: RecipeKind::Tui(TuiAction::SaveSecret),
            args: vec![],
            fields: vec![],
            notes: &[
                "The value is prompted with hidden input and saved in a mode-0600 config file.",
                "Choose a common env var or enter any env-style custom name.",
            ],
        },
        Recipe {
            tabs: &["secrets"],
            title: "List saved secrets",
            description: "List saved Curie secret names without printing values.",
            kind: RecipeKind::Tui(TuiAction::ListSecrets),
            args: vec![],
            fields: vec![],
            notes: &["Only names are listed; secret values stay in private storage."],
        },
        Recipe {
            tabs: &["secrets"],
            title: "Remove secret",
            description: "Remove a saved secret from Curie private storage.",
            kind: RecipeKind::Tui(TuiAction::RemoveSecret),
            args: vec![],
            fields: vec![],
            notes: &[],
        },
        Recipe {
            tabs: &["local"],
            title: "Start local stack",
            description: "Bring up the compose stack for the local platform loop.",
            kind: RecipeKind::Command,
            args: vec![ArgPart::Literal("local"), ArgPart::Literal("up")],
            fields: vec![],
            notes: &["Use the regular CLI for --minimal, --slack, or --local-model variants."],
        },
        Recipe {
            tabs: &["local"],
            title: "Send local message",
            description: "Drive the compose stack end to end with zero Slack contact.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("local"),
                ArgPart::Literal("message"),
                ArgPart::Field("text"),
                ArgPart::OptionalFlag {
                    flag: "--channel",
                    field: "channel",
                },
            ],
            fields: vec![
                Field {
                    key: "text",
                    label: "Message text",
                    default: None,
                    required: true,
                },
                Field {
                    key: "channel",
                    label: "Slack channel id (optional)",
                    default: None,
                    required: false,
                },
            ],
            notes: &["Requires `curie local up` and a deployed local agent."],
        },
        Recipe {
            tabs: &["local"],
            title: "Local status",
            description: "Show compose service status.",
            kind: RecipeKind::Command,
            args: vec![ArgPart::Literal("local"), ArgPart::Literal("status")],
            fields: vec![],
            notes: &[],
        },
        Recipe {
            tabs: &["cluster"],
            title: "Cluster status",
            description: "Report release health and access URLs.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("cluster"),
                ArgPart::Literal("status"),
                ArgPart::OptionalFlag {
                    flag: "--namespace",
                    field: "namespace",
                },
                ArgPart::OptionalFlag {
                    flag: "--release",
                    field: "release",
                },
            ],
            fields: vec![
                Field {
                    key: "namespace",
                    label: "Namespace",
                    default: Some("curie"),
                    required: false,
                },
                Field {
                    key: "release",
                    label: "Release",
                    default: Some("curie"),
                    required: false,
                },
            ],
            notes: &[],
        },
        Recipe {
            tabs: &["cluster"],
            title: "Send cluster message",
            description: "Drive a deployed release end to end with zero Slack contact.",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("cluster"),
                ArgPart::Literal("message"),
                ArgPart::Field("text"),
                ArgPart::OptionalFlag {
                    flag: "--channel",
                    field: "channel",
                },
            ],
            fields: vec![
                Field {
                    key: "text",
                    label: "Message text",
                    default: None,
                    required: true,
                },
                Field {
                    key: "channel",
                    label: "Slack channel id (optional)",
                    default: None,
                    required: false,
                },
            ],
            notes: &["Requires an installed release and a deployed agent."],
        },
        Recipe {
            tabs: &["dev"],
            title: "Install checkout",
            description: "Bootstrap a dev checkout: deps, CLI build, runner image.",
            kind: RecipeKind::Command,
            args: vec![ArgPart::Literal("install")],
            fields: vec![],
            notes: &["Starts nothing; run once after cloning."],
        },
        Recipe {
            tabs: &["dev"],
            title: "Check contracts",
            description: "Run the frozen contract drift checks.",
            kind: RecipeKind::Command,
            args: vec![ArgPart::Literal("dev"), ArgPart::Literal("contracts")],
            fields: vec![],
            notes: &[],
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The platform governance recipes that must work on BOTH tiers (#463).
    /// Observability is deliberately absent: see the local-only test below.
    const TIERED_PLATFORM_RECIPES: [&str; 7] = [
        "List versions",
        "Set budget",
        "Gate a tool (approvals)",
        "Review pending approvals",
        "Inspect memory",
        "Kill an agent",
        "Resume an agent",
    ];

    fn recipe_named(title: &str) -> Recipe {
        recipes()
            .into_iter()
            .find(|recipe| recipe.title == title)
            .unwrap_or_else(|| panic!("no such recipe: {title}"))
    }

    /// argv for a recipe at an explicit tier, with the given field values.
    fn argv_at_tier(title: &str, tier: Tier, fields: &[(&str, &str)]) -> Vec<String> {
        let recipe = recipe_named(title);
        let mut values = BTreeMap::new();
        for (key, value) in fields {
            values.insert((*key).to_string(), (*value).to_string());
        }
        build_argv(&recipe, Some(tier), &values)
    }

    /// #463: every governance recipe carries a resolvable tier instead of a
    /// hardcoded `local`, which is what made the cluster tier unreachable.
    #[test]
    fn platform_governance_recipes_are_tier_bearing_not_hardcoded_local() {
        for title in TIERED_PLATFORM_RECIPES {
            let recipe = recipe_named(title);
            assert!(
                recipe.args.contains(&ArgPart::Tier),
                "recipe {title:?} must lead with ArgPart::Tier so it can run on cluster"
            );
            assert!(
                !recipe.args.contains(&ArgPart::Literal("local")),
                "recipe {title:?} still pins the `local` literal, so the cluster tier is unreachable"
            );
        }
    }

    /// The tier resolves out of `build_argv`'s explicit tier argument and
    /// produces a real argv on both tiers -- reachability, not a title string.
    #[test]
    fn build_argv_resolves_the_tier_part_on_both_tiers() {
        for (tier, verb) in [(Tier::Local, "local"), (Tier::Cluster, "cluster")] {
            assert_eq!(
                argv_at_tier("List versions", tier, &[("agent", "demo")]),
                vec![verb, "versions", "demo"]
            );
            assert_eq!(
                argv_at_tier("Kill an agent", tier, &[("agent", "demo")]),
                vec![verb, "kill", "demo", "--yes"]
            );
            assert_eq!(
                argv_at_tier("Set budget", tier, &[("agent", "demo"), ("limit", "5")]),
                vec![verb, "budget", "demo", "--limit", "5"]
            );
        }
    }

    /// Observability is the ONE platform recipe that stays local-only: there is
    /// no `cluster observability` verb in the clap grammar yet (tracked as
    /// issue #460). Pinning it here keeps a future tier sweep from making the
    /// Platform tab offer a verb that does not exist.
    #[test]
    fn observability_stays_local_only_until_cluster_gains_the_verb() {
        let recipe = recipe_named("Open observability (Console + Langfuse)");
        assert!(
            !recipe.args.contains(&ArgPart::Tier),
            "observability has no cluster verb (#460); it must not become tier-bearing"
        );
        assert_eq!(
            build_argv(&recipe, None, &BTreeMap::new()),
            vec!["local", "observability"]
        );
    }

    /// The parity gate consumes `command_recipe_argvs`, so a tier-bearing
    /// recipe has to expand into BOTH tier variants there. Otherwise the gate
    /// only ever execs the local path and cluster drift ships unnoticed.
    #[test]
    fn command_recipe_argvs_expands_a_tiered_recipe_into_both_tiers() {
        let argvs = command_recipe_argvs();
        let versions: Vec<&Vec<String>> = argvs
            .iter()
            .filter(|(title, _)| *title == "List versions")
            .map(|(_, argv)| argv)
            .collect();

        assert_eq!(
            versions.len(),
            2,
            "expected one local and one cluster argv for List versions, got {versions:?}"
        );
        for tier in ["local", "cluster"] {
            assert!(
                versions
                    .iter()
                    .any(|argv| argv.first().map(String::as_str) == Some(tier)),
                "no {tier} argv for List versions: {versions:?}"
            );
        }
        for argv in &versions {
            assert_eq!(
                argv.get(1).map(String::as_str),
                Some("versions"),
                "expanded argv lost its verb: {argv:?}"
            );
        }
    }

    #[test]
    fn build_argv_omits_empty_optional_flags() {
        let recipe = Recipe {
            tabs: &["skill"],
            title: "x",
            description: "x",
            kind: RecipeKind::Command,
            args: vec![
                ArgPart::Literal("skill"),
                ArgPart::Literal("message"),
                ArgPart::Field("text"),
                ArgPart::OptionalFlag {
                    flag: "--channel",
                    field: "channel",
                },
            ],
            fields: vec![],
            notes: &[],
        };
        let mut values = BTreeMap::new();
        values.insert("text".to_string(), "hello world".to_string());
        values.insert("channel".to_string(), String::new());
        assert_eq!(
            build_argv(&recipe, None, &values),
            vec!["skill", "message", "hello world"]
        );
    }
}
