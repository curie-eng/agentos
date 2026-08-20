#!/usr/bin/env bash
# EXPERIMENTAL developer-facing session loop for remote development, over an
# ALREADY-INSTALLED cluster release. Sibling of cli/scripts/remote-dev-spike.sh:
# the spike falsifies the claim once against the compose Docker substrate, this
# script is the repeatable loop against a real Kubernetes release.
#
# It never runs `cluster up` and never installs, upgrades, or uninstalls
# anything: the release, the namespace, and the sandbox pods are the platform's,
# and this script only deploys a bundle, sends turns, and reads the workspace.
#
#   curie dev remote-dev-session start --namespace <ns> --channel <id> \
#       --listen-host <ip> --repo-url <url> --task "<text>" \
#       [--plugin-dir <dir>] [--timeout-secs <n>]
#   curie dev remote-dev-session turn "<instruction>"
#   curie dev remote-dev-session status
#   curie dev remote-dev-session finish [--branch <name>] [--push]
#   curie dev remote-dev-session down
#
# Cluster access comes from the caller's KUBECONFIG and current context. This
# script never sets, guesses, or switches a context: picking one for the caller
# is how a scratch run lands in somebody's production cluster.
#
# Env knobs:
#   CURIE_BIN   path to a prebuilt curie binary (skip cargo build)
#
# Stdout is ONE JSON object (the machine-readable result). Every human progress
# line goes to stderr, so `curie dev remote-dev-session turn ... | jq` stays
# parseable.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Fixed path, not a mktemp: every verb after `start` is a separate process and
# has to find what `start` left behind.
STATE_DIR="/tmp/curie-remote-dev-session"
STATE_FILE="$STATE_DIR/state.json"
# The workspace path inside the sandbox. /home/runner is one of the two writable
# mounts on the read-only rootfs (the other is /tmp), so it is the only place a
# checkout can live.
SANDBOX_REPO="/home/runner/workspace/repo"
# Sandbox pods are named per thread, so a name filter matches nothing; the
# component label is the only handle that selects them.
SANDBOX_SELECTOR="app.kubernetes.io/component=runner-sandbox"
# The sandbox pod carries init containers, so every exec must name the runner
# container explicitly or kubectl picks the wrong one.
RUNNER_CONTAINER="runner"
# The claim can lag the turn by a couple of seconds, so the after-snapshot is
# retried rather than read once.
POD_WAIT_SECS=60

BIN=""

MODE=""
NAMESPACE=""
CHANNEL=""
LISTEN_HOST=""
REPO_URL=""
TASK=""
PLUGIN_DIR=""
TIMEOUT_SECS="240"
BRANCH=""
PUSH=0
INSTRUCTION=""
THREAD=""
POD=""
TURNS="0"

say() { printf '%s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
# Exit 2 means "you invoked this wrong", exit 1 means "the session ran and
# something failed". A caller that cannot tell those apart retries a typo.
usage_die() { printf 'error: %s\n' "$*" >&2; exit 2; }

usage() {
    cat >&2 <<'USAGE'
usage:
  remote-dev-session start --namespace <ns> --channel <id> --listen-host <ip> \
      --repo-url <url> --task "<text>" [--plugin-dir <dir>] [--timeout-secs <n>]
  remote-dev-session turn "<instruction>"
  remote-dev-session status
  remote-dev-session finish [--branch <name>] [--push]
  remote-dev-session down
USAGE
}

# A --repo-url may carry userinfo (https://user:token@host/...). The token must
# never reach stderr, state.json, or the emitted JSON; the unredacted URL is
# used only for the clone itself. Unanchored and global, because git's own
# error output embeds the URL mid-line (fatal: unable to access '<url>').
redact_url() {
    printf '%s' "$1" | sed -E "s#([a-z+]+://)[^/@[:space:]']+@#\1***@#g"
}

# ---------------------------------------------------------------- arg parsing

if [[ $# -eq 0 ]]; then
    usage
    usage_die "a verb is required."
fi
MODE="$1"
shift
case "$MODE" in
    start|turn|status|finish|down) ;;
    *)
        usage
        usage_die "unknown verb '$MODE'. Expected one of: start, turn, status, finish, down." ;;
esac

case "$MODE" in
    start)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --namespace)
                    [[ $# -ge 2 ]] || usage_die "--namespace needs a value"
                    NAMESPACE="$2"; shift 2 ;;
                --channel)
                    [[ $# -ge 2 ]] || usage_die "--channel needs a value"
                    CHANNEL="$2"; shift 2 ;;
                --listen-host)
                    [[ $# -ge 2 ]] || usage_die "--listen-host needs a value"
                    LISTEN_HOST="$2"; shift 2 ;;
                --repo-url)
                    [[ $# -ge 2 ]] || usage_die "--repo-url needs a value"
                    REPO_URL="$2"; shift 2 ;;
                --task)
                    [[ $# -ge 2 ]] || usage_die "--task needs a value"
                    TASK="$2"; shift 2 ;;
                --plugin-dir)
                    [[ $# -ge 2 ]] || usage_die "--plugin-dir needs a value"
                    PLUGIN_DIR="$2"; shift 2 ;;
                --timeout-secs)
                    [[ $# -ge 2 ]] || usage_die "--timeout-secs needs a value"
                    TIMEOUT_SECS="$2"; shift 2 ;;
                *) usage_die "unknown argument '$1' for verb 'start'" ;;
            esac
        done
        # No default namespace, on purpose: `curie cluster deploy` defaults to
        # `curie`, and inheriting that default here would point a session at
        # whatever release happens to own that namespace.
        [[ -n "$NAMESPACE" ]] || usage_die "start requires --namespace"
        # One agent per channel: a second agent on an occupied channel is
        # rejected by the platform, and the usual default (C0LOCALDEV) is
        # already taken by the weather example on most installs.
        [[ -n "$CHANNEL" ]] || usage_die "start requires --channel"
        # Required plumbing, never auto-detected: auto-detection can pick a
        # Tailscale address that the cluster's pods cannot route back to, and
        # the failure is a silent timeout minutes later.
        [[ -n "$LISTEN_HOST" ]] || usage_die "start requires --listen-host"
        [[ -n "$REPO_URL" ]] || usage_die "start requires --repo-url"
        [[ -n "$TASK" ]] || usage_die "start requires --task"
        [[ "$TIMEOUT_SECS" =~ ^[0-9]+$ ]] || usage_die "--timeout-secs must be a whole number of seconds"
        [[ -n "$PLUGIN_DIR" ]] || PLUGIN_DIR="$REPO_ROOT/examples/coder"
        ;;
    turn)
        [[ $# -ge 1 ]] || usage_die "turn requires the instruction text"
        INSTRUCTION="$1"
        shift
        [[ $# -eq 0 ]] || usage_die "turn takes exactly one instruction argument; quote it"
        [[ -n "$INSTRUCTION" ]] || usage_die "the instruction text must not be empty"
        ;;
    status|down)
        [[ $# -eq 0 ]] || usage_die "$MODE takes no arguments"
        ;;
    finish)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --branch)
                    [[ $# -ge 2 ]] || usage_die "--branch needs a value"
                    BRANCH="$2"; shift 2 ;;
                --push) PUSH=1; shift ;;
                *) usage_die "unknown argument '$1' for verb 'finish'" ;;
            esac
        done
        ;;
esac

# --------------------------------------------------------------- prerequisites

require_tools() {
    local missing=()
    local tool
    for tool in kubectl jq git "$@"; do
        command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
    done
    if (( ${#missing[@]} )); then
        die "missing required tool(s): ${missing[*]}. Install them and re-run."
    fi
}

resolve_bin() {
    if [[ -n "${CURIE_BIN:-}" && -x "${CURIE_BIN:-}" ]]; then
        # Absolutize: this script invokes the binary from other directories, so
        # a relative $CURIE_BIN stops resolving after the first cd.
        BIN="$(cd "$(dirname "$CURIE_BIN")" && pwd)/$(basename "$CURIE_BIN")"
        say "using prebuilt binary: $BIN"
    else
        say "=== cargo build --release (cli) ==="
        (cd "$REPO_ROOT/cli" && cargo build --release --quiet)
        # Ask cargo where the artifact landed rather than assuming cli/target:
        # a build.target-dir override (shared target dirs) moves it.
        local target_dir
        target_dir="$(cd "$REPO_ROOT/cli" && cargo metadata --format-version 1 --no-deps 2>/dev/null | jq -r '.target_directory // ""')"
        [[ -n "$target_dir" ]] || target_dir="$REPO_ROOT/cli/target"
        BIN="$target_dir/release/curie"
    fi
    [[ -x "$BIN" ]] || die "no usable curie binary at $BIN"
}

# ------------------------------------------------------------------- state I/O

load_state() {
    [[ -f "$STATE_FILE" ]] || die "no session state at $STATE_FILE. Run \`curie dev remote-dev-session start ...\` first."
    NAMESPACE="$(jq -r '.namespace // ""' "$STATE_FILE")"
    CHANNEL="$(jq -r '.channel // ""' "$STATE_FILE")"
    LISTEN_HOST="$(jq -r '.listen_host // ""' "$STATE_FILE")"
    THREAD="$(jq -r '.thread // ""' "$STATE_FILE")"
    POD="$(jq -r '.pod // ""' "$STATE_FILE")"
    REPO_URL="$(jq -r '.repo_url // ""' "$STATE_FILE")"
    PLUGIN_DIR="$(jq -r '.plugin_dir // ""' "$STATE_FILE")"
    TIMEOUT_SECS="$(jq -r '.timeout_secs // "240"' "$STATE_FILE")"
    TURNS="$(jq -r '.turns // 0' "$STATE_FILE")"
    [[ -n "$NAMESPACE" && -n "$CHANNEL" && -n "$THREAD" && -n "$POD" ]] \
        || die "$STATE_FILE is incomplete; run \`curie dev remote-dev-session down\` and start again."
}

# Read-modify-write through a temp file so an interrupted update cannot leave a
# truncated state.json behind. Arguments are passed straight to jq (options
# first, filter last), exactly as jq itself expects them.
update_state() {
    local tmp="$STATE_DIR/state.json.tmp"
    jq "$@" "$STATE_FILE" > "$tmp"
    mv "$tmp" "$STATE_FILE"
}

# ---------------------------------------------------------------- cluster glue

kexec() {
    kubectl exec -n "$NAMESPACE" "$POD" -c "$RUNNER_CONTAINER" -- "$@"
}

sandbox_pods() {
    kubectl get pods -n "$NAMESPACE" -l "$SANDBOX_SELECTOR" -o name 2>/dev/null | sort
}

# The single machine-readable turn. `cluster message` exits non-zero on a
# timeout, so the exit code is swallowed here and the terminal shape of the
# payload is judged instead (an unparseable payload is its own failure below).
send_turn() {
    local text="$1" thread="${2:-}"
    local args=(--json cluster message
        --namespace "$NAMESPACE"
        --channel "$CHANNEL"
        --listen-host "$LISTEN_HOST"
        --timeout-secs "$TIMEOUT_SECS")
    if [[ -n "$thread" ]]; then
        args+=(--thread "$thread")
    fi
    "$BIN" "${args[@]}" "$text" || true
}

# Accept the finalized shape and nothing else. An empty reply is NOT rejected:
# a model can finalize with no text and no workspace change, and reporting that
# honestly is the whole point of the before/after diff comparison below.
assert_turn_shape() {
    local label="$1" payload="$2" verdict
    verdict="$(printf '%s' "$payload" | jq -r '
        if type != "object" then "unparseable"
        elif .dry_run then "dry_run"
        elif .awaiting_approval then "awaiting_approval"
        elif .timed_out then "timed_out"
        elif .status == "enqueued" then "enqueued"
        elif .finalized == true then "ok"
        else "not_finalized" end
    ' 2>/dev/null || echo unparseable)"
    case "$verdict" in
        ok) ;;
        awaiting_approval)
            die "$label: the turn parked on an approval gate instead of finalizing; the session bundle must not require approval." ;;
        timed_out)
            die "$label: no reply finalized within ${TIMEOUT_SECS}s. Raise --timeout-secs, or check that --listen-host is an address the cluster's pods can reach." ;;
        dry_run)
            die "$label: got a dry-run descriptor; the session must run for real." ;;
        enqueued)
            die "$label: the turn was enqueued onto the real stream and no reply was waited for, so this session cannot read the result." ;;
        *)
            printf '%s\n' "$payload" >&2
            die "$label: the payload is neither finalized nor a known terminal shape." ;;
    esac
}

# The observable state of the workspace: tracked edits plus the porcelain, since
# an untracked new file is a real change that `git diff` alone never shows.
workspace_signature() {
    printf '%s\n--\n%s' "$(workspace_diff)" "$(workspace_porcelain)"
}

workspace_diff() {
    kexec git -C "$SANDBOX_REPO" diff
}

workspace_porcelain() {
    kexec git -C "$SANDBOX_REPO" status --porcelain
}

# =================================================================== verb: start

verb_start() {
    require_tools
    [[ -d "$PLUGIN_DIR" ]] || die "--plugin-dir '$PLUGIN_DIR' is not a directory."
    if [[ -e "$STATE_FILE" ]]; then
        die "$STATE_FILE already exists, so a previous session was never closed out. Run \`curie dev remote-dev-session down\` first."
    fi
    resolve_bin

    local safe_url
    safe_url="$(redact_url "$REPO_URL")"
    say "=== remote-dev session: start (namespace $NAMESPACE, channel $CHANNEL) ==="
    say "repo url for the sandbox: $safe_url"

    say
    say "=== curie --json cluster deploy --plugin-dir $PLUGIN_DIR ==="
    local deploy_json agent_id deployment_id
    deploy_json="$("$BIN" --json cluster deploy \
        --plugin-dir "$PLUGIN_DIR" \
        --namespace "$NAMESPACE" \
        --slack-channel "$CHANNEL")"
    agent_id="$(printf '%s' "$deploy_json" | jq -r '.agent.id // ""')"
    deployment_id="$(printf '%s' "$deploy_json" | jq -r '.deployment.id // ""')"
    [[ -n "$agent_id" && "$agent_id" != "null" ]] || die "deploy receipt carried no agent.id: $deploy_json"
    [[ -n "$deployment_id" && "$deployment_id" != "null" ]] || die "deploy receipt carried no deployment.id: $deploy_json"
    say "deployed agent $agent_id, deployment $deployment_id"

    # Snapshot BEFORE the turn: the claim is identified by difference, so a
    # sandbox that was already running (another session's) can never be mistaken
    # for this one's.
    local before after
    before="$(sandbox_pods)"

    say
    say "=== turn 1: session start (claims the managed Agent Sandbox) ==="
    local out reply thread
    out="$(send_turn "Remote development session start. The working repository checkout will appear at $SANDBOX_REPO. Your task for this session: $TASK")"
    printf '%s\n' "$out" >&2
    assert_turn_shape "turn-1" "$out"
    thread="$(printf '%s' "$out" | jq -r '.thread // ""')"
    reply="$(printf '%s' "$out" | jq -r '.reply // ""')"
    [[ -n "$thread" ]] || die "turn-1 finalized but carried no thread identity, so later turns cannot rejoin this session."

    say
    say "=== identify the claimed sandbox pod ==="
    local new_pods count waited=0
    while :; do
        after="$(sandbox_pods)"
        new_pods="$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | sed '/^$/d')"
        count="$(printf '%s' "$new_pods" | grep -c . || true)"
        if [[ "$count" == "1" ]]; then
            break
        fi
        if (( waited >= POD_WAIT_SECS )); then
            say "expected exactly one NEW sandbox pod after turn 1, saw $count after ${waited}s:"
            printf '%s\n' "$new_pods" >&2
            die "cannot identify the sandbox this session claimed."
        fi
        sleep 2
        waited=$(( waited + 2 ))
    done
    POD="${new_pods#pod/}"
    say "claimed sandbox pod: $POD"

    say
    say "=== clone the repository into the sandbox ==="
    # The unredacted URL appears exactly here, as the clone's argv, and nowhere
    # else: every other mention of it goes through redact_url.
    local clone_err
    if ! clone_err="$(kubectl exec -n "$NAMESPACE" "$POD" -c "$RUNNER_CONTAINER" -- git clone "$REPO_URL" "$SANDBOX_REPO" 2>&1)"; then
        say "the sandbox could not clone $safe_url. git said:"
        # git's own stderr echoes the remote URL (userinfo included), so it goes
        # through the same redaction as every other mention.
        redact_url "$clone_err" >&2
        printf '\n' >&2
        die "clone into the sandbox failed, so the session has no checkout to work on."
    fi
    say "cloned $safe_url into $SANDBOX_REPO"

    mkdir -p "$STATE_DIR"
    # The REDACTED url is what gets recorded: state.json is a world-readable
    # file under /tmp and must never hold a credential.
    jq -n \
        --arg namespace "$NAMESPACE" \
        --arg channel "$CHANNEL" \
        --arg listen_host "$LISTEN_HOST" \
        --arg thread "$thread" \
        --arg pod "$POD" \
        --arg repo_url "$safe_url" \
        --arg plugin_dir "$PLUGIN_DIR" \
        --arg timeout_secs "$TIMEOUT_SECS" \
        '{namespace: $namespace, channel: $channel, listen_host: $listen_host,
          thread: $thread, pod: $pod, repo_url: $repo_url,
          plugin_dir: $plugin_dir, timeout_secs: $timeout_secs,
          turns: 1, workdirs: []}' > "$STATE_FILE"

    jq -n \
        --arg namespace "$NAMESPACE" \
        --arg channel "$CHANNEL" \
        --arg listen_host "$LISTEN_HOST" \
        --arg state_file "$STATE_FILE" \
        --arg thread "$thread" \
        --arg pod "$POD" \
        --arg repo_url "$safe_url" \
        --arg reply "$reply" \
        '{session: {namespace: $namespace, channel: $channel,
                    listen_host: $listen_host, state_file: $state_file},
          thread: $thread, pod: $pod, repo_url: $repo_url, reply: $reply}'
}

# ==================================================================== verb: turn

verb_turn() {
    require_tools
    load_state
    resolve_bin
    say "=== remote-dev session: turn against pod $POD (thread $THREAD) ==="

    # Captured BEFORE the turn, because a finalized reply says nothing about
    # whether the workspace moved: a model can finalize with an empty reply and
    # no edit at all, and only this comparison can tell the difference.
    local pre post out reply changed
    pre="$(workspace_signature)"

    say
    say "=== send the instruction ==="
    out="$(send_turn "$INSTRUCTION" "$THREAD")"
    printf '%s\n' "$out" >&2
    assert_turn_shape "turn" "$out"
    reply="$(printf '%s' "$out" | jq -r '.reply // ""')"

    post="$(workspace_signature)"
    if [[ "$pre" == "$post" ]]; then
        changed=false
        say "workspace UNCHANGED by this turn (the reply text is not evidence of an edit)"
    else
        changed=true
        say "workspace changed by this turn"
    fi

    update_state '.turns = (.turns + 1)'
    local turns
    turns="$(jq -r '.turns' "$STATE_FILE")"

    local diff
    diff="$(workspace_diff)"

    jq -n \
        --arg thread "$THREAD" \
        --argjson turn "$turns" \
        --arg reply "$reply" \
        --argjson changed "$changed" \
        --arg diff "$diff" \
        '{thread: $thread, turn: $turn, reply: $reply,
          changed: $changed, diff: $diff}'
}

# ================================================================== verb: status

verb_status() {
    require_tools
    load_state
    say "=== remote-dev session: status ==="

    local phase changed_files diff
    phase="$(kubectl get pod -n "$NAMESPACE" "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    [[ -n "$phase" ]] || phase="Unknown"
    say "pod $POD is $phase"

    changed_files="$(workspace_porcelain)"
    diff="$(workspace_diff)"

    jq -n \
        --arg thread "$THREAD" \
        --arg pod "$POD" \
        --arg pod_phase "$phase" \
        --argjson turns "$TURNS" \
        --arg changed_files "$changed_files" \
        --arg diff "$diff" \
        '{thread: $thread, pod: $pod, pod_phase: $pod_phase, turns: $turns,
          changed_files: $changed_files, diff: $diff}'
}

# ================================================================== verb: finish

verb_finish() {
    if (( PUSH )); then
        require_tools gh
    else
        require_tools
    fi
    load_state
    say "=== remote-dev session: finish ==="

    local diff untracked
    diff="$(workspace_diff)"
    [[ -n "$diff" ]] || die "the sandbox's \`git diff\` is empty, so there is nothing to carry out of this session."
    untracked="$(workspace_porcelain | grep '^??' || true)"
    if [[ -n "$untracked" ]]; then
        # Stated, not silently dropped: `git diff` covers tracked edits only, so
        # a new file the agent created is NOT in the patch this verb applies.
        say "warning: the checkout has untracked files that a \`git diff\` patch cannot carry:"
        printf '%s\n' "$untracked" >&2
    fi

    local parent workdir patch_file
    parent="$(mktemp -d /tmp/curie-remote-dev-session-finish.XXXXXX)"
    workdir="$parent/repo"
    patch_file="$parent/session.patch"
    printf '%s\n' "$diff" > "$patch_file"
    update_state --arg dir "$parent" '.workdirs += [$dir]'
    say "patch written to $patch_file"

    # The recorded URL is the REDACTED one, so a repo that needed inline
    # credentials cannot be re-cloned here by design; configure a git credential
    # helper for that host instead of putting the token back into state.json.
    say "=== clone $REPO_URL on the host ==="
    local clone_err
    if ! clone_err="$(git clone "$REPO_URL" "$workdir" 2>&1)"; then
        redact_url "$clone_err" >&2
        printf '\n' >&2
        die "cloning $REPO_URL on the host failed, so the session changes cannot be turned into a branch."
    fi

    if [[ -z "$BRANCH" ]]; then
        BRANCH="task/remote-dev-session-$(printf '%s' "$THREAD" | tr -c 'A-Za-z0-9._-' '-')"
    fi
    say "=== branch $BRANCH ==="
    git -C "$workdir" checkout -q -b "$BRANCH" \
        || die "could not create branch '$BRANCH' in $workdir."
    git -C "$workdir" apply "$patch_file" \
        || die "\`git apply\` rejected $patch_file. The host clone's HEAD has moved away from what the sandbox cloned; re-run against the matching base."
    git -C "$workdir" add -A
    git -C "$workdir" commit -q \
        -m "Remote dev session changes" \
        -m "Produced over $TURNS remote development session turn(s)." \
        || die "the commit failed in $workdir. Set a git identity (user.name and user.email) and re-run."
    say "committed on $BRANCH"

    local pushed=false pr_url=""
    if (( PUSH )); then
        say "=== git push -u origin HEAD ==="
        git -C "$workdir" push -u origin HEAD >&2 \
            || die "the push failed from $workdir."
        say "=== gh pr create --fill ==="
        local gh_out
        gh_out="$(cd "$workdir" && gh pr create --fill)" \
            || die "\`gh pr create --fill\` failed from $workdir."
        # gh prints progress lines before the URL, so the URL is the last line.
        pr_url="$(printf '%s\n' "$gh_out" | tail -n1)"
        pushed=true
        say "pull request: $pr_url"
    else
        say
        say "not pushed. From $workdir, run:"
        say "  git push -u origin HEAD"
        say "  gh pr create --fill"
    fi

    jq -n \
        --arg branch "$BRANCH" \
        --arg workdir "$workdir" \
        --arg patch_file "$patch_file" \
        --argjson pushed "$pushed" \
        --arg pr_url "$pr_url" \
        '{branch: $branch, workdir: $workdir, patch_file: $patch_file,
          pushed: $pushed}
         + (if $pr_url == "" then {} else {pr_url: $pr_url} end)'
}

# ==================================================================== verb: down

verb_down() {
    require_tools
    say "=== remote-dev session: down ==="
    # Deliberately NOT a teardown of the platform: the release, the namespace,
    # and the sandbox pod all belong to the platform (the worker's sandbox
    # substrate claims and reaps a pod per thread), and a session command that
    # uninstalled them would take down work it does not own. `down` removes only
    # what this script itself created on the host.
    if [[ -f "$STATE_FILE" ]]; then
        local dirs
        dirs="$(jq -r '.workdirs[]? // empty' "$STATE_FILE")"
        local dir
        while IFS= read -r dir; do
            [[ -n "$dir" ]] || continue
            say "removing host workdir $dir"
            rm -rf "$dir"
        done <<< "$dirs"
    else
        say "no session state at $STATE_FILE; nothing recorded to clean up."
    fi
    rm -rf "$STATE_DIR"
    say "the sandbox pod and the cluster release are left alone; they are the platform's."

    jq -n '{cleaned: true}'
}

case "$MODE" in
    start) verb_start ;;
    turn) verb_turn ;;
    status) verb_status ;;
    finish) verb_finish ;;
    down) verb_down ;;
esac
