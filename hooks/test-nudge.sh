#!/usr/bin/env bash
# PreToolUse(Bash) nudge: suggest writing a test instead of a python -c diagnostic.
# Non-blocking — injects a hint to Claude only, never prompts the user.

input=$(cat)
cmd=$(jq -r '.tool_input.command // ""' <<< "$input")

hints=()

if printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])(python3?|\.venv/bin/python)[[:space:]]+-c\b'; then
  hints+=("python -c diagnostic detected: if this is investigating a bug, consider writing it as a test in the relevant test file instead — reviewable, reusable, and make test-backend is already allowlisted.")
fi

first=$(printf '%s' "$cmd" | head -n1)

if printf '%s' "$first" | grep -Eq '\bcurl\b.*localhost' || printf '%s' "$cmd" | grep -Eq '\bcurl\b.*Authorization'; then
  hints+=("curl to local API detected: use ~/.claude/bin/api instead — e.g. 'api GET /photos?limit=10'. No token wrangling, no subshells, already allowlisted.")
fi

if printf '%s' "$first" | grep -Eq '(^|[[:space:]/])playwright[[:space:]]+install\b'; then
  hints+=("playwright install detected: check if browsers are already installed first with '.venv/bin/playwright --version' and 'ls ~/.cache/ms-playwright/' before running install.")
fi

if printf '%s' "$first" | grep -Eq '^[[:space:]]*(for|while|until)[[:space:]]'; then
  hints+=("Bash loop detected: consider a Python script or test function instead — easier to debug, already allowlisted, and stays in the repo.")
fi

if [ ${#hints[@]} -gt 0 ]; then
  msg=$(printf '%s\n' "${hints[@]}" | paste -sd ' | ' -)
  jq -n --arg m "$msg" '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "additionalContext": $m
    }
  }'
fi
exit 0
