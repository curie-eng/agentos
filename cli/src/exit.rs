//! Semantic exit codes and error classification for the agent-facing CLI
//! contract (ADR-0021 decision 1).
//!
//! An agent driving `curie` needs to branch on *why* a command failed without
//! parsing prose. The scheme is five stable exit classes:
//!
//! - `0` Success: the command did what was asked.
//! - `1` Failure: a genuine runtime failure (the request was well-formed but the
//!   operation did not succeed).
//! - `2` Usage: a deterministic input error (a missing `--yes`, a malformed
//!   flag) -- retrying the same argv will fail identically, so fix the input.
//! - `3` Transient: a retryable condition (the endpoint was unreachable or timed
//!   out) -- the same argv may succeed once the dependency is up.
//! - `4` Unsupported: the verb exists at this tier but the concept it inspects
//!   does not exist here by construction (issue #459). No input and no retry
//!   changes that; the fix is another tier, which the hint names.
//!
//! A command tags an input error by returning [`usage`] (or building a
//! [`CliError`] directly) and a tier-absent concept by returning [`unsupported`];
//! an unreachable dependency is detected structurally by walking the error chain
//! for a `reqwest` connect/timeout error. Everything else is
//! [`ExitClass::Failure`]. [`classify`] returns the class plus an optional
//! one-line fix hint, and [`error_json`] renders the generic `--json` error
//! payload. Command-specific wrapped payloads are exposed by
//! [`wrapped_json_payload`] for the centralized emitter to select.

/// The five semantic exit classes. The `#[repr(i32)]` values are the process
/// exit codes and are a stable contract agents branch on.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(i32)]
pub enum ExitClass {
    Success = 0,
    Failure = 1,
    Usage = 2,
    Transient = 3,
    Unsupported = 4,
}

impl ExitClass {
    /// The process exit code for this class.
    pub fn code(self) -> i32 {
        self as i32
    }
}

/// Exit the process with `class` after dropping `guards` first.
///
/// `std::process::exit` does not unwind the stack, so any `Drop` impl still in
/// scope at the call site -- a `kubectl port-forward` child, a Slack stub
/// listener -- would otherwise never run. Taking `guards` by value makes the
/// drop-then-exit ordering structural (#751, #766, #1908).
pub fn exit_after_drop<T>(class: ExitClass, guards: T) -> ! {
    drop(guards);
    std::process::exit(class.code());
}

/// A tagged CLI error: a message, an optional actionable fix hint, and the exit
/// class it maps to. Carried through `anyhow`'s chain so [`classify`] can recover
/// the class even when the error was wrapped in later context.
#[derive(Debug)]
pub struct CliError {
    pub message: String,
    pub fix: Option<String>,
    pub class: ExitClass,
}

/// Operator facing context for normal terminal rendering. The original error
/// remains its source so classification and debug output retain the full chain.
#[derive(Debug)]
struct OperatorContext {
    message: String,
    remedy: Option<String>,
    source: anyhow::Error,
}

impl std::fmt::Display for OperatorContext {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for OperatorContext {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(self.source.as_ref())
    }
}

/// An error whose JSON rendering is a command specific reconciliation
/// payload instead of the ordinary `{error, fix}` object.
#[derive(Debug)]
struct JsonPayloadError {
    message: String,
    payload: serde_json::Value,
    class: ExitClass,
    fix: Option<String>,
}

impl std::fmt::Display for JsonPayloadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for JsonPayloadError {}

impl CliError {
    /// A deterministic input error (exit 2).
    pub fn usage(msg: impl Into<String>) -> Self {
        CliError {
            message: msg.into(),
            fix: None,
            class: ExitClass::Usage,
        }
    }

    /// A genuine runtime failure (exit 1).
    pub fn failure(msg: impl Into<String>) -> Self {
        CliError {
            message: msg.into(),
            fix: None,
            class: ExitClass::Failure,
        }
    }

    /// A retryable condition (exit 3).
    pub fn transient(msg: impl Into<String>) -> Self {
        CliError {
            message: msg.into(),
            fix: None,
            class: ExitClass::Transient,
        }
    }

    /// A concept that does not exist at this tier by construction (exit 4).
    pub fn unsupported(msg: impl Into<String>) -> Self {
        CliError {
            message: msg.into(),
            fix: None,
            class: ExitClass::Unsupported,
        }
    }

    /// Attach an actionable fix hint (surfaced in the `--json` payload).
    pub fn with_fix(mut self, fix: impl Into<String>) -> Self {
        self.fix = Some(fix.into());
        self
    }
}

impl std::fmt::Display for CliError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // Render the message only; the fix travels through `classify`, not the
        // Display surface, so a wrapping `err.to_string()` stays clean.
        f.write_str(&self.message)
    }
}

impl std::error::Error for CliError {}

/// Add operator facing context while preserving `source` for classification
/// and debug output.
pub fn operator_context(
    source: anyhow::Error,
    message: impl Into<String>,
    remedy: Option<String>,
) -> anyhow::Error {
    anyhow::Error::from(OperatorContext {
        message: message.into(),
        remedy,
        source,
    })
}

/// Select the outermost operator context for normal terminal rendering. Errors
/// without typed context expose only their outer display text.
pub fn present_error(err: &anyhow::Error) -> (String, Option<String>) {
    err.chain()
        .find_map(|cause| cause.downcast_ref::<OperatorContext>())
        .map_or_else(
            || (err.to_string(), None),
            |context| (context.message.clone(), context.remedy.clone()),
        )
}

/// Build a usage error (exit 2) as an `anyhow::Error` ready to `return Err(..)`.
pub fn usage(msg: impl Into<String>) -> anyhow::Error {
    anyhow::Error::from(CliError::usage(msg))
}

/// Build an unsupported error (exit 4) as an `anyhow::Error` ready to
/// `return Err(..)`: the verb was understood, but `concept` does not exist at
/// this tier `reason`, and `alternative` names the tier that does have it.
///
/// This is the honest answer an agent gets instead of a fabricated empty result
/// (issue #459: every verb is answered at every tier; a verb that lies is worse
/// than one absent).
///
/// The message carries the concept's absence, the reason, AND the alternative,
/// while the alternative ALSO rides in the fix. The redundancy is deliberate: the
/// two consumers read different fields. A machine consumer branches on the
/// ADR-0021 `{error, fix}` payload, where `fix` is the alternative alone and stays
/// exactly the shape it was. A bare [`CliError`] contributes only its `Display`
/// message to the human presenter, so an alternative that lived only in `fix`
/// would tell them why the verb cannot answer and never where it can. Composing
/// it into the message preserves both parts for that human path.
pub fn unsupported(
    concept: impl std::fmt::Display,
    reason: impl std::fmt::Display,
    alternative: impl std::fmt::Display,
) -> anyhow::Error {
    anyhow::Error::from(
        CliError::unsupported(format!(
            "{concept} is not available at this tier: {reason}; {alternative}"
        ))
        .with_fix(alternative.to_string()),
    )
}

/// Build a transient error (exit 3) as an `anyhow::Error` ready to `return Err(..)`.
pub fn transient(msg: impl Into<String>) -> anyhow::Error {
    anyhow::Error::from(CliError::transient(msg))
}

/// Preserve an error's semantic exit classification while replacing only its
/// centralized JSON rendering with `payload`.
pub fn with_json_payload(err: anyhow::Error, payload: serde_json::Value) -> anyhow::Error {
    let (class, fix) = classify(&err);
    anyhow::Error::from(JsonPayloadError {
        message: format!("{err:#}"),
        payload,
        class,
        fix,
    })
}

/// Classify an error into its exit class plus an optional fix hint. Walks the
/// `anyhow` chain so a tagged [`CliError`] is found even under context layers; a
/// `reqwest` connect/timeout failure anywhere in the chain maps to
/// [`ExitClass::Transient`] with a retry hint; everything else is
/// [`ExitClass::Failure`] with no fix.
pub fn classify(err: &anyhow::Error) -> (ExitClass, Option<String>) {
    for cause in err.chain() {
        if let Some(payload) = cause.downcast_ref::<JsonPayloadError>() {
            return (payload.class, payload.fix.clone());
        }
        if let Some(cli) = cause.downcast_ref::<CliError>() {
            // A transient error is retryable by definition, so it always carries
            // a retry hint even when the caller did not attach a specific one.
            let fix = cli
                .fix
                .clone()
                .or_else(|| (cli.class == ExitClass::Transient).then(|| RETRY_HINT.to_string()));
            return (cli.class, fix);
        }
    }
    if is_transient_reqwest(err) {
        return (ExitClass::Transient, Some(RETRY_HINT.to_string()));
    }
    (ExitClass::Failure, None)
}

/// True when the error chain contains a `reqwest` connect/timeout failure --
/// i.e. a dependency (runner, platform API) was unreachable rather than
/// returning an error status. The single definition of "unreachable" shared by
/// [`classify`]'s Transient rule and command-level remediation hints, so the
/// two never diverge on what counts as retryable.
pub fn is_transient_reqwest(err: &anyhow::Error) -> bool {
    err.chain().any(|cause| {
        cause
            .downcast_ref::<reqwest::Error>()
            .is_some_and(|e| e.is_connect() || e.is_timeout())
    })
}

/// The default one-line retry hint for a transient (retryable) failure.
const RETRY_HINT: &str = "the endpoint was unreachable; retry once it is up";

/// The generic `--json` error payload: `{"error": <message>, "fix": <hint or null>}`.
/// `error` is the top-level rendered error; `fix` comes from [`classify`].
pub fn error_json(err: &anyhow::Error) -> serde_json::Value {
    let (_class, fix) = classify(err);
    serde_json::json!({
        "error": format!("{err:#}"),
        "fix": fix,
    })
}

/// Return a command-specific JSON payload carried by a wrapped error, if any.
pub fn wrapped_json_payload(err: &anyhow::Error) -> Option<serde_json::Value> {
    err.chain()
        .find_map(|cause| cause.downcast_ref::<JsonPayloadError>())
        .map(|payload| payload.payload.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unsupported_class_code_is_four() {
        assert_eq!(ExitClass::Unsupported.code(), 4);
    }

    #[test]
    fn classify_unsupported_returns_class_and_alternative_fix() {
        let err = unsupported(
            "versions",
            "the skill tier has no deployed release to inspect",
            "use curie cluster versions <agent>",
        );
        let (class, fix) = classify(&err);
        assert_eq!(class, ExitClass::Unsupported);
        let fix = fix.expect("an unsupported error carries the cross-tier fix");
        assert!(
            fix.contains("cluster versions"),
            "fix must point at the alternative: {fix}"
        );
    }

    #[test]
    fn unsupported_message_carries_reason_and_alternative() {
        // A bare CliError gives the human presenter its Display message without
        // its machine fix field. The message must name both why this tier cannot
        // answer and the tier that can, or the redirect never reaches them.
        let err = unsupported(
            "versions",
            "the skill tier has no deployed release to inspect",
            "use curie cluster versions <agent>",
        );
        let shown = format!("{err:#}");
        assert!(
            shown.contains("no deployed release to inspect"),
            "the human message must carry the reason: {shown}"
        );
        assert!(
            shown.contains("curie cluster versions"),
            "the human message must carry the cross-tier alternative, not only the fix field: {shown}"
        );
    }

    #[test]
    fn error_json_of_unsupported_has_only_error_and_fix_keys() {
        let err = unsupported(
            "versions",
            "the skill tier has no deployed release to inspect",
            "use curie cluster versions <agent>",
        );
        let json = error_json(&err);
        let obj = json.as_object().expect("error_json is an object");
        assert_eq!(obj.len(), 2, "exactly error and fix: {obj:?}");
        assert!(obj.contains_key("error"));
        assert!(obj.contains_key("fix"));
        assert!(
            json["error"].as_str().unwrap().contains("versions"),
            "error names the concept: {}",
            json["error"]
        );
        assert!(
            json["fix"].as_str().unwrap().contains("cluster versions"),
            "fix names the alternative: {}",
            json["fix"]
        );
    }
}
