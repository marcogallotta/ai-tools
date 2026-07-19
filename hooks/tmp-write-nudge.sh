#!/usr/bin/env bash
# PostToolUse(Write) nudge: when writing a script to /tmp, hint to consider
# whether it should be permanent.

input=$(cat)
path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""')

if printf '%s' "$path" | grep -Eq '^/tmp/.*\.(py|sh)$'; then
  jq -n '{
    "hookSpecificOutput": {
      "hookEventName": "PostToolUse",
      "additionalContext": "You just wrote a script to /tmp. Is this genuinely disposable, or has this kind of script come up before? Check if other Claude sessions have solved the same problem — a recurring /tmp script is usually worth a permanent home in scripts/ or ~/.claude/bin/."
    }
  }'
fi
exit 0
