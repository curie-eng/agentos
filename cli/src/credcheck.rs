//! Shape checks for credentials a human pastes into a prompt.
//!
//! The workflow used to save whatever was pasted and move on, so a swapped or
//! truncated token surfaced several steps later as an opaque failure from Slack
//! or the model provider -- by which point the operator is debugging the wrong
//! thing.
//!
//! **Shape, not liveness.** These are prefix and format checks, deliberately
//! not an `auth.test` round trip. Three reasons: a network call inside an
//! alternate-screen TUI can hang with nowhere to show a spinner, it makes the
//! prompt fail when the network is down rather than when the paste is wrong,
//! and the errors that actually happen are shape errors. The single most common
//! one is pasting the two Slack tokens into each other's prompt -- both are
//! copied from the same page, minutes apart -- and a prefix catches that
//! instantly and unambiguously.
//!
//! A pass here means "this is plausibly the right kind of thing", never "this
//! works". The deploy still finds out the truth.

/// What went wrong with a pasted value, phrased for the person who pasted it.
pub type CheckResult = Result<(), String>;

/// Credentials whose prefix identifies them, and what to call them.
const KNOWN_PREFIXES: &[(&str, &str, &str)] = &[
    ("SLACK_APP_TOKEN", "xapp-", "app-level token"),
    ("SLACK_BOT_TOKEN", "xoxb-", "bot user token"),
];

/// Every prefix we can name, for the did-you-swap-them diagnosis.
const RECOGNISED: &[(&str, &str)] = &[
    ("xapp-", "a Slack app-level token"),
    ("xoxb-", "a Slack bot user token"),
    ("xoxp-", "a Slack user token"),
    ("sk-ant-oat", "a Claude Code OAuth token"),
    ("sk-ant-", "an Anthropic API key"),
    ("sk-or-", "an OpenRouter key"),
    ("ghp_", "a GitHub personal access token"),
    ("github_pat_", "a GitHub fine-grained token"),
];

fn describe(value: &str) -> Option<&'static str> {
    RECOGNISED
        .iter()
        .find(|(prefix, _)| value.starts_with(prefix))
        .map(|(_, what)| *what)
}

/// Catch the paste artefacts that are never intentional.
///
/// Surrounding quotes are the common one: copying from a doc that shows
/// `SLACK_BOT_TOKEN="xoxb-..."` takes the quotes along, and the value is then
/// wrong in a way no prefix check would see if it ran after trimming.
fn check_paste_hygiene(value: &str) -> CheckResult {
    if value.is_empty() {
        return Err("nothing was entered".to_string());
    }
    if value.trim() != value {
        return Err("that has whitespace around it -- paste the value only".to_string());
    }
    let quoted = (value.starts_with('"') && value.ends_with('"'))
        || (value.starts_with('\'') && value.ends_with('\''));
    if quoted {
        return Err("that includes the surrounding quotes -- paste the value only".to_string());
    }
    if value.contains(char::is_whitespace) {
        return Err("that contains a space or newline -- it looks truncated or joined".to_string());
    }
    Ok(())
}

/// Does this pasted value look like the credential this prompt asked for?
///
/// Unknown names pass hygiene only: this must never become a gate that refuses
/// a credential shape it has simply not been taught.
pub fn check_secret(name: &str, value: &str) -> CheckResult {
    check_paste_hygiene(value)?;

    let Some((_, want_prefix, want_what)) = KNOWN_PREFIXES.iter().find(|(n, _, _)| *n == name)
    else {
        return Ok(());
    };
    if value.starts_with(want_prefix) {
        return Ok(());
    }
    // Name what WAS pasted when we can. "expected xapp-, got xoxb-" is a
    // one-second fix; "invalid token" is a support ticket.
    match describe(value) {
        Some(actual) => Err(format!(
            "that looks like {actual}. {name} wants the {want_what}, which starts with `{want_prefix}`"
        )),
        None => Err(format!(
            "{name} wants the {want_what}, which starts with `{want_prefix}`"
        )),
    }
}

/// A model credential, whichever of the accepted names it arrives under.
pub fn check_model_credential(value: &str) -> CheckResult {
    check_paste_hygiene(value)?;
    const OK: &[&str] = &["sk-ant-", "sk-or-"];
    if OK.iter().any(|p| value.starts_with(p)) {
        return Ok(());
    }
    match describe(value) {
        Some(actual) => Err(format!(
            "that looks like {actual}, not a model credential. An Anthropic key starts \
             with `sk-ant-`"
        )),
        None => Err(
            "a model credential starts with `sk-ant-` (Anthropic) or `sk-or-` (OpenRouter)"
                .to_string(),
        ),
    }
}

/// A Slack channel ID, not a channel name.
///
/// Pasting `#sre` instead of `C0…` is the mistake the one-page guide lists and
/// the runbook warns about twice. It binds an agent to a channel that does not
/// exist, and nothing complains until the bot never answers.
pub fn check_channel_id(value: &str) -> CheckResult {
    check_paste_hygiene(value)?;
    if let Some(name) = value.strip_prefix('#') {
        return Err(format!(
            "that is a channel NAME. Curie binds by id: right-click #{name} -> View channel \
             details, and copy the id at the bottom (it starts with C)"
        ));
    }
    let looks_like_id = value.starts_with('C')
        && value.len() >= 9
        && value[1..]
            .chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit());
    if !looks_like_id {
        return Err(
            "a Slack channel id starts with C and is upper case letters and digits, \
             e.g. C0EXAMPLE1"
                .to_string(),
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The mistake this module exists for: both tokens are copied from the same
    /// page minutes apart, and swapping them is the single most common error.
    #[test]
    fn swapping_the_two_slack_tokens_is_named_precisely() {
        let err = check_secret("SLACK_APP_TOKEN", "xoxb-EXAMPLE-not-a-real-token")
            .expect_err("must reject");
        assert!(
            err.contains("bot user token"),
            "must say what was pasted: {err}"
        );
        assert!(err.contains("xapp-"), "must say what was wanted: {err}");

        let err = check_secret("SLACK_BOT_TOKEN", "xapp-EXAMPLE-not-a-real-token")
            .expect_err("must reject");
        assert!(err.contains("app-level token"), "{err}");
        assert!(err.contains("xoxb-"), "{err}");
    }

    #[test]
    fn the_right_token_passes() {
        check_secret("SLACK_APP_TOKEN", "xapp-EXAMPLE-not-a-real-token").expect("valid app token");
        check_secret("SLACK_BOT_TOKEN", "xoxb-EXAMPLE-not-a-real-token").expect("valid bot token");
    }

    /// Copying from a doc takes the quotes along.
    #[test]
    fn paste_artefacts_are_caught_before_the_prefix_check() {
        for bad in [
            "\"xoxb-EXAMPLE\"",
            "'xoxb-EXAMPLE'",
            " xoxb-EXAMPLE",
            "xoxb-EXAMPLE ",
        ] {
            let err = check_secret("SLACK_BOT_TOKEN", bad).expect_err("must reject");
            assert!(
                err.contains("quotes") || err.contains("whitespace"),
                "{bad:?} -> {err}"
            );
        }
    }

    #[test]
    fn an_embedded_space_reads_as_truncated_or_joined() {
        let err = check_secret("SLACK_BOT_TOKEN", "xoxb-EXAMPLE not-a-real-token")
            .expect_err("must reject");
        assert!(err.contains("truncated or joined"), "{err}");
    }

    /// An unknown name must pass hygiene only. This must never become a gate
    /// that refuses a credential shape it has not been taught.
    #[test]
    fn an_unknown_credential_name_is_not_second_guessed() {
        check_secret("GRAFANA_SERVICE_ACCOUNT_TOKEN", "glsa_abc123").expect("unknown name passes");
        check_secret("SOME_FUTURE_TOKEN", "whatever-shape-this-is").expect("unknown name passes");
        // ...but hygiene still applies, because those errors are never intended.
        assert!(check_secret("GRAFANA_SERVICE_ACCOUNT_TOKEN", " glsa_abc ").is_err());
    }

    #[test]
    fn a_model_credential_accepts_both_providers() {
        check_model_credential("sk-ant-api03-abc").expect("anthropic");
        check_model_credential("sk-or-v1-abc").expect("openrouter");
        check_model_credential("sk-ant-oat01-abc").expect("oauth token is sk-ant- too");
    }

    #[test]
    fn a_slack_token_pasted_as_a_model_credential_is_named() {
        let err = check_model_credential("xoxb-EXAMPLE-not-a-real-token").expect_err("must reject");
        assert!(err.contains("Slack bot user token"), "{err}");
        assert!(err.contains("sk-ant-"), "{err}");
    }

    /// The mistake the runbook warns about twice: it binds to a channel that
    /// does not exist and nothing complains until the bot never answers.
    #[test]
    fn a_channel_name_is_refused_with_the_way_to_find_the_id() {
        let err = check_channel_id("#sre").expect_err("must reject");
        assert!(err.contains("channel NAME"), "{err}");
        assert!(
            err.contains("View channel details"),
            "must say where to look: {err}"
        );
    }

    #[test]
    fn a_real_channel_id_passes() {
        check_channel_id("C0EXAMPLE1").expect("valid id");
        check_channel_id("C0EXAMPLE2").expect("valid id");
    }

    #[test]
    fn something_that_is_neither_is_refused_with_the_shape() {
        for bad in ["sre", "general", "C0", "c0example1"] {
            let err = check_channel_id(bad).expect_err("must reject");
            assert!(err.contains('C'), "{bad:?} -> {err}");
        }
    }
}
