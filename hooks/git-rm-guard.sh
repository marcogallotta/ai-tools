#!/usr/bin/env bash
# PreToolUse(Bash) guard: force a confirmation prompt for destructive
# git operations and rm, overriding any allow-list match (e.g. Bash(git *)).
# Emits permissionDecision:"ask" when matched; stays silent otherwise so
# normal permission evaluation proceeds.

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

# rm anywhere in the command (word-bounded so npm/charm/etc. don't match)
if printf '%s' "$cmd" | grep -Eq '(^|[[:space:];&|(`])rm([[:space:]]|$)'; then
  ask "[git-rm-guard] Destructive: 'rm' requires explicit approval."
fi

# docker compose down -v destroys volumes (including dev DB)
if printf '%s' "$cmd" | grep -Eq '\bdocker\b' && printf '%s' "$cmd" | grep -Eq '\bdown\b' && printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])-[a-zA-Z]*v'; then
  ask "[git-rm-guard] Destructive: 'docker compose down -v' will destroy volumes including the dev DB. Explicit approval required."
fi

# git-commit wrapper (atomic stage+commit)
if printf '%s' "$cmd" | grep -Eq '(^|[[:space:]/])git-commit[[:space:]]'; then
  ask "[git-rm-guard] git-commit (stage + commit) requires explicit approval."
fi

# destructive git subcommands
if printf '%s' "$cmd" | grep -Eq '\bgit\b'; then
  if printf '%s' "$cmd" | grep -Eq '\b(commit|push|clean|checkout|restore)\b'; then
    ask "[git-rm-guard] Destructive git operation (commit/push/checkout/restore/clean) requires explicit approval."
  fi
  if printf '%s' "$cmd" | grep -Eq '\breset\b' && printf '%s' "$cmd" | grep -Eq -- '--hard'; then
    ask "[git-rm-guard] Destructive git operation (reset --hard) requires explicit approval."
  fi
  if printf '%s' "$cmd" | grep -Eq '\bgit\b[^|&;]*\badd\b'; then
    deny "[git-rm-guard] Don't run git add alone — it leaves staged changes that can collide with another session's commit. Use: ~/.claude/bin/git-commit <files> -m 'message'"
  fi
fi

# psql write operations
if printf '%s' "$cmd" | grep -Eq '\bpsql\b'; then
  if printf '%s' "$cmd" | grep -Eiq '\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|COPY)\b'; then
    ask "[git-rm-guard] psql write operation (INSERT/UPDATE/DELETE/DROP/etc.) requires explicit approval."
  fi
  if printf '%s' "$cmd" | grep -Eq -- '-c\b'; then
    ask "[git-rm-guard] psql -c (inline SQL command) requires explicit approval."
  fi
fi

# rsync --delete removes files on the target
if printf '%s' "$cmd" | grep -Eq '\brsync\b' && printf '%s' "$cmd" | grep -Eq -- '--delete\b'; then
  ask "[git-rm-guard] rsync --delete can remove files on the target. Explicit approval required."
fi

# ssh with a remote command (not just interactive login)
# plantpi.local is trusted — skip the prompt for that host
if printf '%s' "$cmd" | grep -Eq '\bssh\b' && ! printf '%s' "$cmd" | grep -Eq 'plantpi\.local'; then
  nonflag=$(printf '%s' "$cmd" | awk '
    BEGIN { found=0; skip=0; n=0 }
    {
      for (i=1; i<=NF; i++) {
        if (!found) { if ($i ~ /\/ssh$/ || $i == "ssh") found=1; continue }
        if (skip) { skip=0; continue }
        if ($i ~ /^-[bicloDEeFIiJLmopQRSWw]$/) { skip=1; continue }
        if ($i ~ /^-/) continue
        n++
      }
    }
    END { print n }
  ')
  if [ "${nonflag:-0}" -gt 1 ]; then
    ask "[git-rm-guard] ssh with remote command (not just interactive login) requires explicit approval."
  fi
fi

exit 0
