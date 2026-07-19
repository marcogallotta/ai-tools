#!/usr/bin/env bash
# PreToolUse(Write) guard: force a confirmation prompt whenever Claude tries
# to write to the auto-memory directory, overriding the broad Write allow-list.

input=$(cat)
path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""')

if printf '%s' "$path" | grep -q '/.claude/projects/.*memory/'; then
  jq -nc --arg r "Memory write to '$path' requires explicit approval." \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"ask",permissionDecisionReason:$r}}'
  exit 0
fi

exit 0
