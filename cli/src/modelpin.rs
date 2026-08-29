//! Is the configured model a pinned snapshot, or a name that moves?
//!
//! Providers publish two kinds of model id. A **dated snapshot** ends in a
//! calendar date (`claude-haiku-4-5-20251001`, `gpt-4o-2024-08-06`) and always
//! serves the same weights. A **floating alias** omits the date
//! (`claude-sonnet-5`, `gpt-4o`) and is repointed at a newer snapshot whenever
//! the provider ships one. Both are valid to send; only one is reproducible.
//!
//! An alias is therefore an unversioned input to a versioned artifact. The
//! bundle is immutable and promotes across the ladder unchanged, and the eval
//! suite that approved it graded one set of weights -- but an alias can serve
//! different weights the next morning, with no deploy, no diff, and nothing in
//! any log to say so. That is the one behaviour change no gate in this repo can
//! see, which is why it is worth naming at the only moment a human is looking:
//! `curie doctor`.
//!
//! **Shape, not a catalog.** The rule here is purely the id's own form, so it
//! needs no list of known models, no provider credential, and no network call.
//! That is a deliberate limit: it can say "this name moves", and it cannot say
//! "a newer snapshot exists", because knowing that needs a list of what the
//! provider currently publishes. A curated list is the follow-up; asserting a
//! model id nobody fetched would be inventing data, and a stale hand-written
//! catalog is worse than no catalog.
//!
//! **The rule is three-way, not two** (#1950). Pinned, floating, and
//! *unrecognised*. The third arm exists because the second used to be a
//! catch-all: any id whose shape [`dated_suffix`] could not parse was
//! *asserted* to float. That mislabelled a whole class of ids that are fully
//! pinned snapshots -- `glm-4-0520`, `kimi-k2-0711-preview`,
//! `gemini-1.5-pro-002`, `mistral-large-2411`,
//! `anthropic.claude-3-5-sonnet-20240620-v1:0`, `amazon.nova-pro-v1:0` -- in a
//! spelling this rule has never seen. Calling those floating invents a fact, and the fix the check
//! emitted for a floating name (`export CURIE_MODEL=<id>-YYYYMMDD`) produces an
//! id every one of those providers rejects. Refusing to assert is the honest
//! answer: `Unrecognized` carries no claim about whether the id moves and no
//! advice about pinning it.
//!
//! Classification is a pure function of the id, so every case below is a unit
//! test rather than a fixture.

/// The trailing calendar date in a model id, normalised to `YYYYMMDD`.
///
/// Two spellings are in use and both must be recognised, which is the kind of
/// thing worth pinning in a test rather than assuming:
///
/// - compact, `...-YYYYMMDD` (`claude-haiku-4-5-20251001`);
/// - hyphenated, `...-YYYY-MM-DD` (`gpt-4o-2024-08-06`).
///
/// Structural rather than a regex over known families, so a family this rule
/// has never seen classifies correctly the day it ships. The month and day are
/// range-checked so an ordinary numeric version suffix cannot read as a date,
/// and at least one non-date segment is required so a bare date is not a model
/// id.
fn dated_suffix(id: &str) -> Option<String> {
    fn digits(s: &str, len: usize) -> bool {
        s.len() == len && s.bytes().all(|b| b.is_ascii_digit())
    }
    fn calendar(year: &str, month: &str, day: &str) -> Option<String> {
        let m: u32 = month.parse().ok()?;
        let d: u32 = day.parse().ok()?;
        ((1..=12).contains(&m) && (1..=31).contains(&d)).then(|| format!("{year}{month}{day}"))
    }

    let parts: Vec<&str> = id.split('-').collect();
    let n = parts.len();

    // Compact: one trailing eight-digit segment, preceded by a name.
    if n >= 2 {
        let last = parts[n - 1];
        if digits(last, 8) {
            if let Some(date) = calendar(&last[0..4], &last[4..6], &last[6..8]) {
                return Some(date);
            }
        }
    }

    // Hyphenated: three trailing segments of 4, 2 and 2 digits, preceded by a
    // name. `n >= 4` is what keeps a bare `2024-08-06` from classifying.
    if n >= 4 {
        let (year, month, day) = (parts[n - 3], parts[n - 2], parts[n - 1]);
        if digits(year, 4) && digits(month, 2) && digits(day, 2) {
            if let Some(date) = calendar(year, month, day) {
                return Some(date);
            }
        }
    }

    None
}

/// What the configured model id is, as far as its own shape can say.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PinStatus {
    /// Nothing configured. The platform default applies, which is a choice the
    /// operator has not made rather than one they got wrong.
    Unset,
    /// A dated snapshot: reproducible, and what an eval result can be trusted
    /// against.
    Pinned {
        /// The full model id.
        id: String,
        /// Its `YYYYMMDD` suffix, for the detail line.
        date: String,
    },
    /// A floating alias: valid, but the weights behind it can change without
    /// any change here.
    Floating {
        /// The full model id.
        id: String,
    },
    /// A shape this rule does not recognise. It carries a number, or an
    /// explicit `v<digits>` version marker, that the rule cannot read as a
    /// calendar date, so it is very likely a provider revision this rule has
    /// never seen. Deliberately asserts nothing about whether the id moves.
    Unrecognized {
        /// The full model id.
        id: String,
    },
}

/// Whether the id carries a component this rule cannot interpret.
///
/// Split on every separator the providers in use here spell an id with -- `-`,
/// `.`, `:`, `_`, `/` (the last covers the OpenRouter `vendor/model` form) --
/// and look for a token in **either** of two shapes. Both mean the same thing:
/// "a version lives in this id, and this rule cannot read which one".
///
/// - **A run of three or more ASCII digits and nothing else** (`002`, `0520`,
///   `0711`, `2411`, `20240620`). Three is the threshold because a family or
///   version digit is one or two (`claude-sonnet-5`, `qwen3`, `gpt-4o`,
///   `llama-3.1`), while every observed provider revision suffix is three or
///   more. A number that long, sitting where [`dated_suffix`] could not read it
///   as a calendar date, is a revision spelling this rule has not seen.
/// - **An explicit version marker: `v` followed by one or more ASCII digits and
///   nothing else** (`v1`, `v2`, `v10`). This is the Bedrock scheme.
///   `amazon.nova-pro-v1:0` is a concrete versioned id that AWS publishes
///   alongside separately versioned siblings such as `v1:1`, and it is not
///   silently repointed -- lifecycle migration is the caller's move, not the
///   provider's. Its digit run is a single character, so the first shape misses
///   it entirely and the id would be *asserted* to float; the `v` marker is the
///   signal that says otherwise (#1950).
///
/// A token carrying any other character matches neither shape (`405b` is a
/// parameter count, and `v20251001x` is a build tag rather than a marker,
/// because the digits do not run to the end of the token). Both tests are
/// deliberately exact rather than "contains digits".
///
/// The question this answers is "could this shape be carrying a revision or a
/// date I cannot read?", not "is this id one of the five in #1950". It has two
/// known limits, and neither is fail-safe -- each is a case the rule gets
/// wrong, in a bounded and deliberate way:
///
/// - a hypothetical two-digit revision (`model-52`) still reads as floating, so
///   the check still *asserts* something it cannot know for that shape. That is
///   the pre-existing behaviour and a real limit, not a safe fallback;
///   separating it from a family digit needs a signal shape alone does not have.
/// - a genuinely floating alias carrying an unrelated three-digit token reads as
///   unrecognised, so the check declines to advise where it could have. That one
///   costs advice rather than asserting a falsehood, which is why the threshold
///   sits where it does.
fn carries_unreadable_revision(id: &str) -> bool {
    // Purely numeric, three characters or more: a provider revision or a date
    // this rule could not parse.
    fn long_digit_run(token: &str) -> bool {
        token.len() >= 3 && token.bytes().all(|b| b.is_ascii_digit())
    }
    // `v` followed by at least one ASCII digit and nothing else: an explicit
    // version marker. The digits must run to the end of the token, which is what
    // keeps the build tag `v20251001x` out.
    fn version_marker(token: &str) -> bool {
        match token.strip_prefix('v') {
            Some(digits) => !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()),
            None => false,
        }
    }

    id.split(['-', '.', ':', '_', '/'])
        .any(|token| long_digit_run(token) || version_marker(token))
}

/// Classify the configured model id by its shape.
///
/// `None` and an all-whitespace value are both `Unset`: an exported-but-empty
/// variable is the #229 footgun, and reading it as a configured model would
/// report a pin that does not exist.
///
/// A readable calendar date is a pin. Otherwise the id is only *asserted* to
/// float when it carries nothing that could be an unreadable revision -- see
/// [`carries_unreadable_revision`].
pub fn classify(model: Option<&str>) -> PinStatus {
    let Some(id) = model.map(str::trim).filter(|id| !id.is_empty()) else {
        return PinStatus::Unset;
    };
    match dated_suffix(id) {
        Some(date) => PinStatus::Pinned {
            id: id.to_string(),
            date,
        },
        None if carries_unreadable_revision(id) => PinStatus::Unrecognized { id: id.to_string() },
        None => PinStatus::Floating { id: id.to_string() },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_dated_suffix_is_a_pin() {
        assert_eq!(
            classify(Some("claude-haiku-4-5-20251001")),
            PinStatus::Pinned {
                id: "claude-haiku-4-5-20251001".into(),
                date: "20251001".into(),
            }
        );
        // The other provider hyphenates the date, so the trailing segment is
        // "06" rather than the whole date. Both spellings are load bearing and
        // this assertion is what caught the first rule for only handling one.
        assert_eq!(
            classify(Some("gpt-4o-2024-08-06")),
            PinStatus::Pinned {
                id: "gpt-4o-2024-08-06".into(),
                date: "20240806".into(),
            }
        );
    }

    /// The case that motivated this check: the name stayed and the weights
    /// behind it changed. This list is half the contract of the three-way rule
    /// -- every id here carries no token that could be read as a revision or a
    /// date, so the rule is entitled to assert that it moves. The list is
    /// deliberately only ever extended: shrinking it would silently move an id
    /// into `Unrecognized`, where the check declines to advise at all.
    #[test]
    fn an_undated_name_floats() {
        for id in [
            "gpt-4o",
            "claude-sonnet-5",
            "claude-opus-5",
            "qwen3:4b",
            // `405b` is a parameter count, not a revision -- it is not purely
            // numeric, so the 3+-digit rule must not claim it as one (#1950
            // edge case 11b).
            "llama-3.1-405b",
            "gpt-4o-mini",
            "deepseek-chat",
            "model-latest",
            // A build tag, not a version marker. The `v<digits>` rule added for
            // the Bedrock ids must NOT swallow this: its digits do not run to
            // the end of the token (`v20251001x`), so the marker test fails and
            // the id keeps the floating verdict it has always had. If this ever
            // flips to `Unrecognized`, the marker predicate has been loosened
            // into "starts with v and has digits somewhere", which would take a
            // whole class of aliases with it.
            "model-v20251001x",
        ] {
            assert_eq!(
                classify(Some(id)),
                PinStatus::Floating { id: id.into() },
                "{id} should read as floating"
            );
        }
    }

    #[test]
    fn unset_and_empty_are_both_unset() {
        assert_eq!(classify(None), PinStatus::Unset);
        assert_eq!(classify(Some("")), PinStatus::Unset);
        assert_eq!(classify(Some("   ")), PinStatus::Unset);
    }

    #[test]
    fn surrounding_whitespace_does_not_change_the_verdict() {
        assert_eq!(
            classify(Some("  claude-haiku-4-5-20251001  ")),
            PinStatus::Pinned {
                id: "claude-haiku-4-5-20251001".into(),
                date: "20251001".into(),
            }
        );
    }

    /// The eight-digit range check is what keeps an ordinary build or version
    /// suffix from being reported as a pinned date. That property is the whole
    /// reason this test exists and it is asserted directly below, id by id.
    ///
    /// What changed with the three-way rule (#1950): these ids all carry a
    /// 3+-digit numeric token, so instead of being *asserted* to float they now
    /// route to `Unrecognized` -- the rule cannot read the number as a calendar
    /// date, and it will not guess that it is therefore an alias. Every id from
    /// the original list is retained deliberately: this list is the record of
    /// what the range check rejects.
    #[test]
    fn an_eight_digit_suffix_that_is_not_a_date_is_not_a_pin() {
        for id in [
            "some-model-99999999",   // month 99
            "some-model-20251301",   // month 13
            "some-model-20250100",   // day 00
            "some-model-20250132",   // day 32
            "some-model-2025-13-01", // hyphenated, month 13
            "some-model-2025-01-32", // hyphenated, day 32
            "2024-08-06",            // a bare date is not a model id
        ] {
            assert!(
                !matches!(classify(Some(id)), PinStatus::Pinned { .. }),
                "{id} must never be reported as a pin -- a number that is not a \
                 calendar date is not a snapshot"
            );
            assert_eq!(
                classify(Some(id)),
                PinStatus::Unrecognized { id: id.into() },
                "{id} should not read as a date"
            );
        }
    }

    /// The loop is split because the three-way rule parts these four ids: a
    /// purely numeric 3+-digit token is a revision this rule cannot read, while
    /// a token carrying any non-digit (or shorter than three) is not. All four
    /// original ids are retained -- they are the boundary cases of the
    /// discriminator, and dropping one would leave that boundary untested.
    #[test]
    fn a_short_or_non_numeric_suffix_is_not_a_pin() {
        // `2025` is four pure digits, so the rule declines to call it an alias.
        assert_eq!(
            classify(Some("model-2025")),
            PinStatus::Unrecognized {
                id: "model-2025".into()
            }
        );
        for id in [
            "model-latest", // no numeric token at all
            // Numeric-looking, but neither shape: not purely numeric, and not a
            // `v<digits>` marker either, since the trailing `x` means the digits
            // do not run to the end of the token.
            "model-v20251001x",
            "model-", // trailing empty token, shorter than three
        ] {
            assert_eq!(
                classify(Some(id)),
                PinStatus::Floating { id: id.into() },
                "{id} should read as floating"
            );
        }
        for id in ["model-2025", "model-latest", "model-v20251001x", "model-"] {
            assert!(
                !matches!(classify(Some(id)), PinStatus::Pinned { .. }),
                "{id} must never be reported as a pin"
            );
        }
    }

    /// An id with no separator at all must not panic on the split. That is the
    /// property this test exists for and it survives the three-way rule
    /// verbatim; only the verdict moved, because eight pure digits is exactly
    /// the shape the new arm refuses to interpret.
    #[test]
    fn an_id_with_no_separator_does_not_panic() {
        assert_eq!(
            classify(Some("20251001")),
            PinStatus::Unrecognized {
                id: "20251001".into()
            }
        );
    }

    /// The defect #1950 names: every one of these is a fully pinned snapshot
    /// from a provider whose spelling this rule has never seen, and every one
    /// was reported `ok | ... is a floating name` with a fix
    /// (`export CURIE_MODEL=<id>-YYYYMMDD`) that produces an INVALID id for it.
    /// Refusing to assert is the honest answer; a wrong fix string is worse
    /// than none.
    ///
    /// The two `amazon.nova-*` ids are AWS's versioned-id scheme and the reason
    /// the discriminator grew its second shape. Bedrock spells a concrete model
    /// version as a `v<major>:<minor>` suffix (`amazon.nova-pro-v1:0`, with
    /// separately published siblings such as `v1:1`), and AWS documents that
    /// moving between them is the caller's migration, never an automatic
    /// repoint. Under the 3+-digit rule alone these tokenise to
    /// `amazon`/`nova`/`pro`/`v1`/`0` with no numeric run long enough to catch,
    /// so they were *asserted* to float and handed an
    /// `export CURIE_MODEL=<id>-YYYYMMDD` fix Bedrock rejects -- a pinned id
    /// reported as floating, which is exactly the false assertion this arm
    /// exists to stop.
    #[test]
    fn a_provider_revision_suffix_is_not_asserted_to_float() {
        for id in [
            "anthropic.claude-3-5-sonnet-20240620-v1:0", // Bedrock, fully pinned
            "amazon.nova-pro-v1:0",                      // Bedrock versioned id
            "amazon.nova-lite-v1:0",                     // ditto, sibling family
            "glm-4-0520",                                // zhipu
            "kimi-k2-0711-preview",                      // moonshot
            "gemini-1.5-pro-002",
            "mistral-large-2411",
        ] {
            assert_eq!(
                classify(Some(id)),
                PinStatus::Unrecognized { id: id.into() },
                "{id} carries a revision this rule cannot read, so it must not \
                 be asserted to float"
            );
        }
    }
}
