# VHS tape for the README Quickstart, steps 1-2 (skill -> local).
# Regenerate with: vhs docs/demo/demo-loop.tape
#
# Requires on PATH: curie, docker (running), tree
# Requires in env: one of CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY / CURIE_CREDENTIALS
#   (real model, matching the README Quickstart's default flow -- no --fake-model here)
#
# NOTE: step 3 (cluster) and step 4 (CI/CD git push) are intentionally NOT in this
# tape. `curie cluster up` installs the full Helm chart (Postgres, Valkey, Langfuse,
# ClickHouse, MinIO, OTel Collector, API, worker) and realistically takes several
# minutes end to end -- too long for a single continuous GIF. Record that as its own
# asset once the timing/format is decided. Step 4 needs a repo already connected via
# git-flow (docs/operations.md) -- skipped per instruction, no such repo set up yet.
#
# Install VHS: brew install charmbracelet/tap/vhs

Output docs/demo/demo-loop.gif

Require curie
Require docker

Set Shell "bash"
Set FontSize 17
Set Width 1300
Set Height 750
Set Theme "Dracula"
Set TypingSpeed 55ms
Set Padding 20

# Start from a clean scratch dir so the recording is reproducible.
Hide
Type "rm -rf /tmp/curie-demo && mkdir -p /tmp/curie-demo && cd /tmp/curie-demo"
Enter
Sleep 500ms
Show

# --- Step 1: Build and test (skill) ---
# -q/--quiet keeps the payload (banner, reply) but drops the per-second spinner
# lines, which is what was bloating the file size. Measured for real: skill up
# ~2-3s, message near-instant reply.

Type "curie init my-agent && cd my-agent"
Enter
Sleep 1.5s

Type "tree -a"
Enter
Sleep 2s

Type "curie skill up -q"
Enter
Sleep 3s

Type `curie skill message "hello, are you there?"`
Enter
Sleep 25s

Type "curie skill down -q"
Enter
Sleep 1.5s

# --- Step 2: Full platform, still your laptop (local) ---
# Measured for real (cold containers, twice): up ~18-19s, deploy ~0.3s,
# message ~5-10s, continue ~2-3s, down ~13s. `up`'s wait is real Docker
# startup time, not a scripting gap -- -q just means it prints nothing
# during that stretch instead of a repeating spinner.

Type "curie local up -q"
Enter
Sleep 20s

Type `curie local deploy -q --plugin-dir . --slack-channel C0123ABCD --api-url http://localhost:28000`
Enter
Sleep 2s

Type `curie local message "hello, are you there?"`
Enter
Sleep 20s

Type `curie local message --continue "what's 2 + 2?"`
Enter
Sleep 15s

Type "curie local down -q"
Enter
Sleep 14s
