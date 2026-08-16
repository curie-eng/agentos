---
seam: Model provider / credentials
kind: SOFT
impls: 2 prefix-routed (Anthropic, OpenRouter) + base-URL-selected provider-native endpoints (Zhipu, Moonshot, DeepSeek, Ollama)
grade: not separately graded
epics:
  - "#24"
  - "#46"
order: 6
---

# INTERFACE: Model provider / credentials

> Part of the Curie swappable-seam catalog — see the [seam index](../../interfaces.md).

<!-- BEGIN GENERATED: header (curie dev docs-lint) -->
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 2 prefix-routed (Anthropic, OpenRouter) + base-URL-selected provider-native endpoints (Zhipu, Moonshot, DeepSeek, Ollama) &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The model provider is swapped through config, not code: a base-URL override plus a
credential whose prefix routes it onto the right SDK auth env, targeting any
Anthropic-compatible endpoint. There is no provider interface to implement — a
config author fills in a base URL, a credential, and a model id, and the runner maps
them onto the variables the bundled Claude CLI authenticates from. What stays core is
the Anthropic wire format (kept even for OpenRouter, so prompt caching survives).

## Current contract

Resolution lives in `runner/src/curie_runner/sdk_auth.py`. A provider is defined by
four things:

- **Wire protocol**, declared as `CURIE_MODEL_API_BACKEND`
  (`runner/src/curie_runner/sdk_auth.py::API_BACKEND_ENV`), one of the `ApiBackend`
  members (`runner/src/curie_runner/sdk_auth.py::ApiBackend`): `messages`,
  `chat_completions`, `responses`. Unset or empty means `messages`
  (`runner/src/curie_runner/sdk_auth.py::DEFAULT_API_BACKEND`), which is what every
  endpoint was previously assumed to speak. Only `messages` is dialable
  (`runner/src/curie_runner/sdk_auth.py::ApiBackend.speaks_anthropic_wire`) — the
  runner speaks the Anthropic wire format via claude-agent-sdk, so the OpenAI-shaped
  backends are declarable but rejected up front by `resolve_sdk_env`
  (`runner/src/curie_runner/sdk_auth.py::UnsupportedApiBackendError`); reaching one
  needs a translating proxy in front of the runner. See
  [ADR-0048](../../adr/0048-declared-model-wire-protocol-and-credential-keys.md).
- **Credential**, delivered as `CURIE_CREDENTIALS`
  (`runner/src/curie_runner/sdk_auth.py::CREDENTIALS_ENV`) by default, or from the env
  var names `CURIE_MODEL_ENV_KEY` declares
  (`runner/src/curie_runner/sdk_auth.py::MODEL_ENV_KEY_ENV`) — a bare name or a JSON
  array of them, walked in order, first set and non-empty key winning
  (`runner/src/curie_runner/sdk_auth.py::resolve_credential`); a present-but-empty key
  is skipped. `resolve_model_credential` (`runner/src/curie_runner/sdk_auth.py::resolve_model_credential`) routes it by
  prefix: `sk-ant-oat...` → `CLAUDE_CODE_OAUTH_TOKEN`; other `sk-ant-...`
  → `ANTHROPIC_API_KEY`; `sk-or-...` (OpenRouter) → base-URL override; a bare `sk-...`
  raises `UnsupportedCredentialError` (`runner/src/curie_runner/sdk_auth.py::UnsupportedCredentialError`). An SDK credential
  already in the env wins.
- **Base URL**, `ANTHROPIC_BASE_URL`, or its `CURIE_`-namespaced alias
  `CURIE_MODEL_BASE_URL` (the raw var wins when both are set).
  `resolve_base_url_override` (`runner/src/curie_runner/sdk_auth.py::resolve_base_url_override`) builds the override env when either is set, and
  `resolve_sdk_env` (`runner/src/curie_runner/sdk_auth.py::resolve_sdk_env`) is the entry point that decides override-vs-plain.
- **Model id**, `CURIE_MODEL`, read by `RunnerConfig.from_env`
  (`runner/src/curie_runner/config.py::RunnerConfig.from_env`).

The override deliberately blanks `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_AUTH_TOKEN`
to `""` (`runner/src/curie_runner/sdk_auth.py::resolve_base_url_override`) so an inherited token cannot take precedence or leak to a
third-party endpoint.

## Implementations today

**Anthropic** (direct API key or Claude Code OAuth token) — the plain path, no
base URL. **OpenRouter** — the one prefix-routed alternative: an `sk-or-`
credential auto-selects `OPENROUTER_BASE_URL = "https://openrouter.ai/api"` and
puts the real key in `ANTHROPIC_API_KEY` (the `x-api-key` header OpenRouter
reads).

**Provider-native `/anthropic` endpoints** — Zhipu (GLM), Moonshot (Kimi),
DeepSeek, and local Ollama are selected by **base URL**, not key prefix. Moonshot
and DeepSeek use OpenAI-style `sk-` keys and Zhipu uses a non-`sk-` key, so no key
prefix distinguishes them; the config author instead points the base URL at the
provider's `/anthropic` endpoint and supplies the provider key in
`CURIE_CREDENTIALS`. In base-URL-override mode a supplied provider credential is
forwarded into `ANTHROPIC_API_KEY` (`x-api-key`), overriding the `NO_OP_API_KEY`
placeholder; a Claude Code OAuth token (`sk-ant-oat`) is never forwarded and stays
blanked (hermetic). Canonical base URLs are in `PROVIDER_BASE_URLS`:

| Provider | `CURIE_MODEL_BASE_URL` | Key shape | Candidate models |
| --- | --- | --- | --- |
| Zhipu (GLM) | `https://api.z.ai/api/anthropic` | `id.secret` (non-`sk-`) | GLM 5.x |
| Moonshot (Kimi) | `https://api.moonshot.ai/anthropic` | `sk-...` | Kimi K2.x |
| DeepSeek | `https://api.deepseek.com/anthropic` | `sk-...` | DeepSeek V4 |

Each keeps the Anthropic wire format (the SDK appends `/v1/messages`), so the
provider's automatic prefix caching applies — a single-model agent needs no
gateway. A minimal config: `CURIE_MODEL_BASE_URL=https://api.deepseek.com/anthropic`,
`CURIE_CREDENTIALS=<deepseek key>`, `CURIE_MODEL=deepseek-...`.

## Known leakage

The gotcha the seam encodes rather than a bleed-through: base-URL override mode must
carry a **non-empty** placeholder key, `NO_OP_API_KEY = "not-needed"`
(`runner/src/curie_runner/sdk_auth.py::NO_OP_API_KEY`). The bundled Claude CLI treats an empty `ANTHROPIC_API_KEY` as
"not logged in" and refuses to dial the overridden endpoint (empirically verified
2026-07-07, `runner/src/curie_runner/sdk_auth.py::resolve_base_url_override`), so an empty string is not viable — a deliberately
non-`sk-` placeholder passes the auth gate without being mistakable for a credential.
The credential value is never logged.

The real bleed-through is above the seam: three separate copies of provider knowledge
live outside the runner, and each can drift from it.

- **A second, lagging provider registry in the CLI.** `--allow-egress-host` accepts
  only `anthropic` and `openrouter` (the `EGRESS_PROVIDERS` table behind
  `parse_egress_provider` in `cli/src/ops.rs`), while the runner ships `zhipu`,
  `moonshot`, `deepseek`, and `openrouter`
  (`runner/src/curie_runner/sdk_auth.py::PROVIDER_BASE_URLS`). The three
  provider-native endpoints in the table above therefore have no provider name to
  pass, so a cluster agent on Zhipu, Moonshot, or DeepSeek has to open its sandbox
  egress with a raw `--allow-web-egress` CIDR instead. The doc comment on that table
  still describes those three as runtime support the runner has yet to gain, which it
  has since shipped.
- **The CLI paste gate knows only the two prefix-routed shapes.**
  `check_model_credential` (`cli/src/credcheck.rs`) accepts a pasted value only when
  it starts with `sk-ant-` or `sk-or-`, so the interactive "Save Model Credential"
  prompt (`cli/src/interactive.rs`) refuses every provider-native key the table above
  documents: Zhipu's non-`sk-` `id.secret`, and Moonshot's and DeepSeek's bare `sk-`
  keys. The runner accepts all three under a base-URL override, so this is a
  paste-time check disagreeing with the resolver, not a real restriction; supplying
  the credential through the environment or a saved secret short-circuits the prompt
  before the check runs.
- **Two mirrors of the non-forwardable-credential rule.** The runner is the authority
  for the `sk-ant-oat` prefix
  (`runner/src/curie_runner/sdk_auth.py::OAUTH_TOKEN_PREFIX`), and it is already
  declared per harness rather than hardcoded
  (`runner/src/curie_runner/harness/contribution.py::AuthSpec`, field
  `oauth_token_prefix`). Above the seam the CLI (`cli/src/commands.rs`) and the
  worker's Docker sandbox
  (`apps/worker/src/curie_worker/sandbox/docker.py::_OAUTH_TOKEN_PREFIX`) each
  re-declare the literal so they can decide whether to forward a credential into a
  sandbox. The three are pinned together by the shared vector file
  `tests/vectors/model-credential-forwarding.json`, read by
  `apps/worker/tests/sandbox/test_vector_credential_forwarding.py` and by the vector
  loop in the `cli/src/commands.rs` test module, so a lane that changes the rule
  without changing the file fails its own test. That gate makes the duplication safe,
  not absent: a second harness declaring a different `oauth_token_prefix` would leave
  both above-seam mirrors hardcoded to Claude's.

## Cross-links

- **Epic(s):** #24 — bring your own model (OpenRouter + native Anthropic-format endpoints), the seam's forward work; #46 (closed) — the Ollama local-model demo mode, which also exercises this base-URL-override + credential path.
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — core config seam, not one of the six swappable jobs.
- **Harness declaration:** the credential shapes a harness accepts are declared, not assumed: `runner/src/curie_runner/harness/contribution.py::AuthSpec` carries `credential_env_keys` and `oauth_token_prefix`, and `build_spawn_env` is the hook the Claude harness wires to `resolve_sdk_env` (`runner/src/curie_runner/harness/claude.py::CLAUDE_CONTRIBUTION`). See [ADR-0060](../../adr/0060-the-harness-is-a-declared-package.md).
- **ADR(s):** [ADR-0009](../../adr/0009-per-agent-connector-auth.md) — per-agent secrets and connector credentials (the model credential is the one credential the platform resolves today, via prefix mapping in `sdk_auth.py`); [ADR-0048](../../adr/0048-declared-model-wire-protocol-and-credential-keys.md) — the endpoint's wire protocol and the credential's source env var are declared rather than assumed.
