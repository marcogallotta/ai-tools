#!/usr/bin/env bash
# PreToolUse(Bash) guard: block shell redirect patterns that should use the
# Write/Edit tools instead. Catches >, >>, | tee, and sed -i.

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""')

deny() {
  jq -nc --arg r "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

first=$(printf '%s' "$cmd" | head -n1)

# Redirect to file: > or >> (not inside a quoted string — best-effort).
# /dev/null is exempt: discarding stdout/stderr (e.g. `grep ... 2>/dev/null`
# on an otherwise read-only command) writes nothing and isn't a Write-tool
# bypass.
if printf '%s' "$first" | grep -Eq '[^<]>[>]?[[:space:]]*/' \
  && ! printf '%s' "$first" | grep -Eq '[^<]>[>]?[[:space:]]*/dev/null\b'; then
  deny "BLOCKED: shell redirect to file. Use the Write tool instead."
fi
if printf '%s' "$first" | grep -Eq '[^<]>[>]?[[:space:]]*[^&[:space:]/]'; then
  deny "BLOCKED: shell redirect to file. Use the Write tool instead."
fi

# Pipe to tee (writes to file)
if printf '%s' "$first" | grep -Eq '\|[[:space:]]*tee[[:space:]]'; then
  deny "BLOCKED: pipe to tee writes a file. Use the Write tool instead."
fi

# Pipe into python -c (inline code — should be a script)
if printf '%s' "$cmd" | grep -Eq '\|[[:space:]]*(python3?|\.venv/bin/python)[[:space:]]+-c\b'; then
  deny "BLOCKED: pipe into python -c. Write a .py script and run it with .venv/bin/python instead — readable, reusable, no prompt needed."
fi

# sed -i (in-place file edit)
if printf '%s' "$first" | grep -Eq '\bsed\b[^|]*-[a-zA-Z]*i'; then
  deny "BLOCKED: sed -i edits files in place. Use the Edit tool instead."
fi

exit 0
