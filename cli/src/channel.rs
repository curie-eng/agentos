//! Terminal adapter for the rendering-neutral `curie-reply` channel contract.

use serde::Deserialize;

pub const REPLY_FENCE: &str = "```curie-reply";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TerminalAction {
    pub label: String,
    pub value: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TerminalMessage {
    pub lines: Vec<String>,
    pub actions: Vec<TerminalAction>,
    pub action_prompt: Option<String>,
    pub allow_free_text: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Action {
    label: String,
    value: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MessageField {
    label: String,
    value: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MessageLink {
    label: String,
    url: String,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
enum Interaction {
    #[serde(rename = "choice")]
    Choice {
        id: String,
        prompt: Option<String>,
        options: Vec<Action>,
        #[serde(default = "default_true")]
        allow_free_text: bool,
    },
    #[serde(rename = "confirm")]
    Confirm {
        id: String,
        prompt: String,
        confirm: Action,
        cancel: Action,
        #[serde(default)]
        allow_free_text: bool,
    },
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OutboundMessage {
    version: String,
    text: String,
    status: Option<String>,
    header: Option<String>,
    #[serde(default)]
    fields: Vec<MessageField>,
    #[serde(default)]
    links: Vec<MessageLink>,
    footer: Option<String>,
    interaction: Option<Interaction>,
}

#[derive(Debug, Deserialize)]
struct LegacyMessage {
    #[serde(default)]
    text: String,
    status: Option<String>,
    header: Option<String>,
    #[serde(default)]
    fields: Vec<(String, String)>,
    #[serde(default)]
    buttons: Vec<(String, String)>,
    #[serde(default)]
    links: Vec<(String, String)>,
    footer: Option<String>,
}

fn display_lines(
    text: String,
    status: Option<String>,
    header: Option<String>,
    fields: impl IntoIterator<Item = (String, String)>,
    links: impl IntoIterator<Item = (String, String)>,
    footer: Option<String>,
) -> Vec<String> {
    let mut lines = Vec::new();
    if let Some(status) = status.filter(|value| !value.is_empty()) {
        lines.push(status);
    }
    if let Some(header) = header.filter(|value| !value.is_empty()) {
        lines.push(header);
    }
    for (label, value) in fields {
        lines.push(format!("{label}: {value}"));
    }
    lines.extend(text.lines().map(str::to_string));
    for (label, url) in links {
        lines.push(format!("{label}: {url}"));
    }
    if let Some(footer) = footer.filter(|value| !value.is_empty()) {
        lines.push(footer);
    }
    lines
}

// Field bounds the channel-protocol schema declares
// (`packages/channel-protocol/schema/channel-protocol.schema.json`). The Rust
// adapter enforces them so a message the Python model would reject does not
// render as an interactive widget here -- it degrades to plain text (the same
// drop-to-fallback this parser already does for malformed input). Gated against
// the committed schema corpus (#490) so these cannot silently drift.
const ACTION_LABEL_MAX: usize = 75;
const ACTION_VALUE_MAX: usize = 255;
const INTERACTION_ID_MAX: usize = 255;
const CHOICE_OPTIONS_MAX: usize = 10;

/// An action's label/value are both 1..=max chars (schema min_length 1).
fn action_bounds_ok(action: &Action) -> bool {
    (1..=ACTION_LABEL_MAX).contains(&action.label.chars().count())
        && (1..=ACTION_VALUE_MAX).contains(&action.value.chars().count())
}

/// An interaction id is 1..=255 chars.
fn interaction_id_ok(id: &str) -> bool {
    (1..=INTERACTION_ID_MAX).contains(&id.chars().count())
}

/// Parse a complete fenced semantic message. Legacy button pairs remain readable
/// during migration, but only the versioned shape is the public contract.
pub fn parse_terminal_message(text: &str) -> Option<TerminalMessage> {
    let start = text.find(REPLY_FENCE)? + REPLY_FENCE.len();
    let rest = text
        .get(start..)?
        .strip_prefix('\n')
        .unwrap_or(&text[start..]);
    let end = rest.rfind("```")?;
    let payload = rest[..end].trim();
    let value: serde_json::Value = serde_json::from_str(payload).ok()?;
    if value.get("version").is_some() {
        let message: OutboundMessage = serde_json::from_value(value).ok()?;
        if message.version != "1.0" {
            return None;
        }
        let (actions, prompt, allow_free_text) = match message.interaction {
            Some(Interaction::Choice {
                id,
                prompt,
                options,
                allow_free_text,
            }) if interaction_id_ok(&id)
                && (1..=CHOICE_OPTIONS_MAX).contains(&options.len())
                && options.iter().all(action_bounds_ok) =>
            {
                (
                    options
                        .into_iter()
                        .map(|action| TerminalAction {
                            label: action.label,
                            value: action.value,
                        })
                        .collect(),
                    prompt,
                    allow_free_text,
                )
            }
            Some(Interaction::Confirm {
                id,
                prompt,
                confirm,
                cancel,
                allow_free_text,
            }) if interaction_id_ok(&id)
                && action_bounds_ok(&confirm)
                && action_bounds_ok(&cancel) =>
            {
                (
                    vec![
                        TerminalAction {
                            label: confirm.label,
                            value: confirm.value,
                        },
                        TerminalAction {
                            label: cancel.label,
                            value: cancel.value,
                        },
                    ],
                    Some(prompt),
                    allow_free_text,
                )
            }
            _ => (Vec::new(), None, true),
        };
        return Some(TerminalMessage {
            lines: display_lines(
                message.text,
                message.status,
                message.header,
                message
                    .fields
                    .into_iter()
                    .map(|item| (item.label, item.value)),
                message.links.into_iter().map(|item| (item.label, item.url)),
                message.footer,
            ),
            actions,
            action_prompt: prompt,
            allow_free_text,
        });
    }

    let legacy: LegacyMessage = serde_json::from_value(value).ok()?;
    Some(TerminalMessage {
        lines: display_lines(
            legacy.text,
            legacy.status,
            legacy.header,
            legacy.fields,
            legacy.links,
            legacy.footer,
        ),
        actions: legacy
            .buttons
            .into_iter()
            .map(|(label, value)| TerminalAction { label, value })
            .collect(),
        action_prompt: None,
        allow_free_text: true,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The committed cross-language corpus (#490, #1079): the SAME file the Python
    /// channel-protocol model test asserts against, so a field-bound change on one
    /// side without the other fails a corpus test here or there. It pins field
    /// bounds (#490) AND field semantics (#1079): every valid case carries an
    /// explicit `allow_free_text` that both sides compare against, so a side that
    /// hardcodes the flag instead of carrying the value through fails here.
    const CORPUS: &str =
        include_str!("../../packages/channel-protocol/schema/channel-protocol.corpus.json");

    fn fenced(message: &serde_json::Value) -> String {
        format!("```curie-reply\n{message}\n```")
    }

    #[test]
    fn matches_the_shared_schema_corpus() {
        let corpus: serde_json::Value = serde_json::from_str(CORPUS).unwrap();
        // A valid message renders an interactive widget (non-empty actions).
        for message in corpus["valid"].as_array().unwrap() {
            let parsed = parse_terminal_message(&fenced(message)).expect("valid message parses");
            assert!(
                !parsed.actions.is_empty(),
                "corpus valid message rendered no widget: {message}"
            );
            // Compared, not just rendered: a side that hardcoded the flag would
            // otherwise pass every corpus case regardless of what it carries. The
            // key is REQUIRED, not presence-guarded, so deleting it from a corpus
            // case reds this test instead of silently disarming the comparison.
            // Omitted-key defaults (choice `true`, confirm `false`, per
            // ChoiceIntent/ConfirmIntent in
            // packages/channel-protocol/src/channel_protocol/models.py) are
            // deliberately out of scope for this corpus.
            if let Some(interaction) = message.get("interaction") {
                let expected = interaction
                    .get("allow_free_text")
                    .and_then(serde_json::Value::as_bool)
                    .unwrap_or_else(|| {
                        panic!("corpus valid message carries no allow_free_text: {message}")
                    });
                assert_eq!(
                    parsed.allow_free_text, expected,
                    "corpus valid message rendered the wrong allow_free_text: {message}"
                );
            }
        }
        // An out-of-bounds message must NOT render a widget: the bound guard fails
        // and it degrades to plain text (no actions) -- the same rejection the
        // Python model makes with a ValidationError.
        for entry in corpus["invalid"].as_array().unwrap() {
            let message = &entry["message"];
            let widget = parse_terminal_message(&fenced(message))
                .map(|m| !m.actions.is_empty())
                .unwrap_or(false);
            assert!(
                !widget,
                "corpus out-of-bounds message rendered a widget ({}): {message}",
                entry["reason"]
            );
        }
    }

    #[test]
    fn choice_renders_labels_but_preserves_values() {
        let message = parse_terminal_message(
            "```curie-reply\n{\"version\":\"1.0\",\"text\":\"Pick one\",\"interaction\":{\"kind\":\"choice\",\"id\":\"repo\",\"prompt\":\"Repository\",\"options\":[{\"label\":\"Curie\",\"value\":\"curie-eng/curie\"}]}}\n```",
        )
        .unwrap();
        assert_eq!(message.lines, ["Pick one"]);
        assert_eq!(message.actions[0].label, "Curie");
        assert_eq!(message.actions[0].value, "curie-eng/curie");
        assert!(message.allow_free_text);
    }

    #[test]
    fn confirm_disables_free_text_by_default() {
        let message = parse_terminal_message(
            "```curie-reply\n{\"version\":\"1.0\",\"text\":\"Deploy?\",\"interaction\":{\"kind\":\"confirm\",\"id\":\"deploy\",\"prompt\":\"Deploy?\",\"confirm\":{\"label\":\"Deploy\",\"value\":\"deploy\"},\"cancel\":{\"label\":\"Cancel\",\"value\":\"cancel\"}}}\n```",
        )
        .unwrap();
        assert!(!message.allow_free_text);
        assert_eq!(message.actions.len(), 2);
    }

    #[test]
    fn rejects_native_widgets_and_unknown_versions() {
        assert!(parse_terminal_message(
            "```curie-reply\n{\"version\":\"1.0\",\"text\":\"x\",\"blocks\":[]}\n```"
        )
        .is_none());
        assert!(parse_terminal_message(
            "```curie-reply\n{\"version\":\"2.0\",\"text\":\"x\"}\n```"
        )
        .is_none());
    }
}
