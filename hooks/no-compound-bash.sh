#!/usr/bin/env bash
# Hard-block compound/chained bash on the FIRST line of a command:
#   - && or || chaining
#   - leading VAR=value env prefix
# These defeat the permission allowlist and cause prompt spam. Forces one
# command per Bash call. Heredoc bodies (after the first newline) are ignored,
# so `python - <<'PY' ... PY` and quoted ';' are unaffected.
cmd=$(jq -r '.tool_input.command // empty')
first=$(printf '%s' "$cmd" | head -n1)
# Strip content inside single/double quotes before checking for && and ||
stripped=$(printf '%s' "$first" | sed "s/\"[^\"]*\"//g; s/'[^']*'//g")
if printf '%s' "$stripped" | grep -qE '&&|\|\||^[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]'; then
  printf '%s' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"BLOCKED: compound/chained bash. Run ONE command per Bash call — no && or ||, no leading VAR= prefix. Split it into separate Bash calls."}}'
fi
exit 0
