#!/usr/bin/env bash
#
# Render-assertion test for the cluster sealing keypair (ADR-0094).
#
# The keypair is the one credential in this chart that cannot be regenerated.
# Losing it does not rotate anything -- it makes every value sealed to it, in
# every agent repository, permanently unreadable. Nothing outside the cluster
# can reconstruct it. So the properties below are not style preferences:
#
#   (a) The chart NEVER generates it. `curie.managedSecret` mints a random when
#       a value matches its default, which is right for a store password and
#       catastrophic here: the chart has no lookup-persist (#195), so a
#       chart-side random would mint a NEW key on every `helm upgrade`. The CLI
#       owns generation and preservation (ops::resolve_sealing_values).
#   (b) Only the WORKER receives it. The connector reconciler runs there and is
#       the only thing that decrypts. Every other workload that does not need a
#       decryption key must not be handed one.
#   (c) The previous key renders independently, so a rotation overlap survives
#       the upgrades that happen during it.
#
# Four assertions.
set -euo pipefail

# NOTE: read variables with a herestring, never `printf ... | cmd`. Under
# `pipefail` an early-exiting reader (grep -q, awk with `exit`) SIGPIPEs the
# printf, so the pipeline reports failure on a match that SUCCEEDED -- which is
# exactly how this script first reported a passing property as broken.

CHART="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0

fail() {
    echo "FAIL: $1" >&2
    FAILED=1
}

# -- (a) never generated -------------------------------------------------------
#
# A default install must leave both empty. If this ever renders a value, the
# chart has started minting keys and every upgrade silently destroys sealed
# credentials.
DEFAULT="$(helm template t "$CHART" -n t)"
CURRENT="$(printf '%s' "$DEFAULT" | awk '/^  sealingPrivateKey:/{print $2}')"
if [[ "$CURRENT" != '""' ]]; then
    fail "a default install rendered a sealing private key ($CURRENT); the chart must never generate one"
else
    echo "ok: a default install renders no sealing key"
fi

# -- (b) supplied key reaches the worker, and only the worker ------------------
SUPPLIED="$(helm template t "$CHART" -n t --set sealing.privateKey=QUJDREVG)"
REFS="$(grep -c 'name: CURIE_SEALING_PRIVATE_KEY' <<<"$SUPPLIED" || true)"
if [[ "$REFS" != "1" ]]; then
    fail "expected exactly one workload to receive the sealing key, found $REFS"
else
    echo "ok: exactly one workload receives the sealing key"
fi

# Confirm that one workload is the worker. Splitting on the document separator
# rather than grepping the whole render: a match anywhere would otherwise pass
# even if the key landed in the API or the dispatcher.
WORKER_DOC="$(awk '
    /^---/ { doc = ""; next }
    { doc = doc "\n" $0 }
    /CURIE_SEALING_PRIVATE_KEY/ { holder = doc }
    END { print holder }
' <<<"$SUPPLIED")"
if ! grep -q 'component: worker' <<<"$WORKER_DOC"; then
    fail "the sealing key went to a workload that is not the worker"
else
    echo "ok: the worker is the workload that receives it"
fi

# -- (c) the previous key renders independently --------------------------------
#
# Rotation is the weakest point of this design (ADR-0094). If the previous key
# could not be set alongside the current one, a rotation would break every
# repository the moment it started.
BOTH="$(helm template t "$CHART" -n t \
    --set-string sealing.privateKey=CURRENTKEY \
    --set-string sealing.previousPrivateKey=PREVIOUSKEY)"
# Extract the field rather than matching a rendered line literally: the value is
# base64 in real use, whose `=` padding makes a literal `--set` and a literal
# grep both quietly fragile.
PREV="$(awk -F'"' '/^  sealingPreviousPrivateKey:/{print $2; exit}' <<<"$BOTH")"
CURR="$(awk -F'"' '/^  sealingPrivateKey:/{print $2; exit}' <<<"$BOTH")"
if [[ "$PREV" != "PREVIOUSKEY" || "$CURR" != "CURRENTKEY" ]]; then
    fail "both keys must render together for a rotation to overlap; got current=[$CURR] previous=[$PREV]"
else
    echo "ok: both keys render together, so a rotation can overlap"
fi

if ((FAILED)); then
    echo "sealing-key assertions FAILED" >&2
    exit 1
fi
echo "sealing-key assertions passed"
