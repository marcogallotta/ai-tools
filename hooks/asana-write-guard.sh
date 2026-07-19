#!/usr/bin/env bash
# PreToolUse(Bash) guard: force a confirmation prompt for Asana WRITE
# operations via the asana CLI, overriding the Bash(.../asana *) allow-list match.
# Emits permissionDecision:"ask" when matched; stays silent otherwise so normal
# permission evaluation proceeds. Read subcommands (notes/get/tasks/sections/
# projects/subtasks/search, and `raw GET`) fall through untouched.

input=$(cat)
cmd=$(printf '%s' "$input" | jq -r '.tool_input.command // ""')

ask() {
  jq -nc --arg r "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"ask",permissionDecisionReason:$r}}'
  exit 0
}

deny() {
  jq -nc --arg r "$1" \
    '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:$r}}'
  exit 0
}

batch_plan_from_command() {
  python3 -c '
import os, shlex, sys
try:
    words = shlex.split(sys.argv[1])
except ValueError:
    raise SystemExit(1)
if words and words[0] == "bash":
    words = words[1:]
for i, word in enumerate(words):
    if os.path.basename(os.path.expanduser(word)) == "asana" and words[i + 1:i + 2] == ["batch-apply"]:
        if len(words) == i + 3:
            print(os.path.expanduser(words[i + 2]))
            raise SystemExit(0)
raise SystemExit(1)
' "$1"
}

has_asana_write() {
  local text="$1"

  # Match asana CLI whether invoked directly (.../bin/asana) or via bash (.../bin/asana)
  # Strip a leading "bash " prefix so "bash /path/to/asana ..." is treated the same.
  text=$(printf '%s' "$text" | sed 's|^[[:space:]]*bash[[:space:]]\+||')

  # Only consider asana CLI invocations (binary may be a full path: .../bin/asana)
  if printf '%s' "$text" | grep -Eq '(^|[[:space:]/])asana[[:space:]]'; then
    # write subcommands immediately after the asana binary
    if printf '%s' "$text" | grep -Eq '(^|[[:space:]/])asana[[:space:]]+(set-notes|append|replace|rename|create-task|create-subtask|move|batch-apply)\b'; then
      return 0
    fi
    # raw writes: PUT/POST/DELETE (raw GET is a read)
    if printf '%s' "$text" | grep -Eiq '(^|[[:space:]/])asana[[:space:]]+raw[[:space:]]+(put|post|delete)\b'; then
      return 0
    fi
  fi

  # Also catch argv-list forms in scripts, e.g.
  # subprocess.run(["/path/to/asana", "rename", ...]).
  if printf '%s' "$text" | grep -Eq "asana[\"'][[:space:]]*,[[:space:]]*[\"'](set-notes|append|replace|rename|create-task|create-subtask|move|batch-apply)[\"']"; then
    return 0
  fi
  if printf '%s' "$text" | grep -Eiq "asana[\"'][[:space:]]*,[[:space:]]*[\"']raw[\"'][[:space:]]*,[[:space:]]*[\"'](put|post|delete)[\"']"; then
    return 0
  fi

  return 1
}

script_from_command() {
  local first file runner
  first=$(printf '%s' "$cmd" | head -n1)

  # Common script runners, source forms, or direct /path/to/script execution.
  # This intentionally handles simple unquoted paths, which is the form agents
  # normally generate for these bypass attempts.
  runner=$(printf '%s' "$first" | awk '{print $1}')
  case "$(basename "$runner")" in
    bash|sh|zsh|python|python3|node|ruby|perl|php|source)
      file=$(printf '%s' "$first" | awk '{for (i = 2; i <= NF; i++) if ($i !~ /^-/) {print $i; exit}}')
      [ -n "$file" ] || return 1
      ;;
    env)
      file=$(printf '%s' "$first" | awk '
        {
          for (i = 2; i <= NF; i++) {
            if ($i ~ /^-/ || $i ~ /^[A-Za-z_][A-Za-z0-9_]*=/) continue
            runner = $i
            base = runner
            sub(/^.*\//, "", base)
            if (base ~ /^(bash|sh|zsh|python|python3|node|ruby|perl|php)$/) {
              for (j = i + 1; j <= NF; j++) {
                if ($j !~ /^-/) {
                  print $j
                  exit
                }
              }
            }
            exit
          }
        }')
      [ -n "$file" ] || return 1
      ;;
    .)
      file=$(printf '%s' "$first" | awk '{print $2}')
      [ -n "$file" ] || return 1
      ;;
    *)
      if printf '%s' "$first" | grep -Eq '^[[:space:]]*(/|\.{1,2}/|~)[^[:space:]]+'; then
        file=$(printf '%s' "$first" | awk '{print $1}')
      else
        return 1
      fi
      ;;
  esac

  file=${file/#\~/$HOME}
  if [ "$(basename "$file")" = "asana" ]; then
    return 1
  fi
  if [ -f "$file" ] && [ -r "$file" ]; then
    printf '%s' "$file"
    return 0
  fi

  return 1
}

# A direct batch-apply gets one short prompt. Claude has already shown the readable
# summary table immediately before invoking the command.
if printf '%s' "$cmd" | grep -Eq '(^|[[:space:]/])asana[[:space:]]+batch-apply\b'; then
  plan=$(batch_plan_from_command "$cmd") || deny "[asana-write-guard] Invoke batch-apply directly with exactly one plan path so its preview can be checked."
  count=$(jq -r '.operations | if type == "array" then length else 0 end' "$plan" 2>/dev/null)
  if [ -z "$count" ] || [ "$count" -lt 3 ]; then
    deny "[asana-write-guard] Batch requires at least 3 operations; use direct Asana commands for one or two writes."
  fi
  preview=$(/home/marco/.claude/bin/asana batch-preview "$plan" 2>&1)
  status=$?
  if [ "$status" -ne 0 ]; then
    error=$(printf '%s\n' "$preview" | tail -n 1)
    reason=$(printf '[asana-write-guard] Batch validation failed; nothing was written. %s' "$error")
    deny "$reason"
  fi
  target_count=$(jq -r '[.operations[] | (.task // .parent // .project // empty | tostring)] | unique | length' "$plan")
  reason=$(printf '[asana-write-guard] Approve Asana batch: %s operations across %s targets?' "$count" "$target_count")
  ask "$reason"
fi

if has_asana_write "$cmd"; then
  ask "[asana-write-guard] Approve this Asana write?"
fi

if script=$(script_from_command); then
  if has_asana_write "$(cat "$script")"; then
    deny "[asana-write-guard] Script contains hidden Asana writes. Invoke direct CLI operations for one or two writes, or one direct batch-apply for three or more."
  fi
fi

# Plant monitoring API wrapper — writes require approval
if printf '%s' "$cmd" | grep -Eq '(^|[[:space:]/])api[[:space:]]+(POST|PATCH|PUT|DELETE)\b'; then
  ask "[api-guard] API write (POST/PATCH/PUT/DELETE) requires explicit approval."
fi

exit 0
