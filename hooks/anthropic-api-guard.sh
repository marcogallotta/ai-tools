#!/usr/bin/env bash
# PreToolUse(Bash) guard: force a confirmation prompt for ANY command that may
# submit to the Anthropic (corporate) API and spend the limited prepaid budget.
# Emits permissionDecision:"ask" when matched; stays silent otherwise so normal
# permission evaluation proceeds.
#
# There is a HARD, limited prepaid budget. No call to the Anthropic API runs
# without explicit, per-run human approval — a rule, not a promise.
#
# Detection = (a) scripts known to call the API + (b) direct API signals (SDK
# import, key, endpoint, messages.create/batches, model ids). MAINTAIN the script
# list when new API-calling scripts are added. A `--dry` run does not spend, but is
# still gated for safety — it is cheap to approve.

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""')

ask() {
  jq -nc --arg r "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"ask",permissionDecisionReason:$r}}'
  exit 0
}

# (a) known API-calling scripts        (b) direct Anthropic API signals
if printf '%s' "$cmd" | grep -Eiq \
  '(tag_eval|submit_tagging_batch|ingest_tagging_batch|agreement_gate)([._[:space:]]|$)|anthropic|ANTHROPIC_API_KEY|api\.anthropic\.com|messages\.(create|batches)|claude-(sonnet|opus|haiku)'; then
  ask "Anthropic API spend — limited prepaid budget; explicit per-run approval required (\$ leaves the budget). A --dry/no-spend run is safe to approve."
fi

exit 0
