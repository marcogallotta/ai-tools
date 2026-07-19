#!/usr/bin/env bash
# PreToolUse(Bash) guard: force a confirmation prompt (or outright deny) for
# a broad set of destructive/risky ops — not just git and rm, but also
# docker volume removal, psql writes, rsync --delete, and ssh with a remote
# command — overriding any allow-list match (e.g. Bash(git *)).
# Emits permissionDecision:"ask" (or "deny") when matched; stays silent
# otherwise so normal permission evaluation proceeds.

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
# Skip the prompt when every target literally starts with /tmp/ — scratch space,
# not worth an approval round-trip. Any non-/tmp target (or no resolvable
# target) falls back to asking, since paths built from variables/expansions
# aren't visible to this hook as literal text.
if printf '%s' "$cmd" | grep -Eq '(^|[[:space:];&|(`])rm([[:space:]]|$)'; then
  rm_targets="$(printf '%s' "$cmd" | awk '
    BEGIN { found=0 }
    {
      for (i=1; i<=NF; i++) {
        if (!found) { if ($i == "rm") found=1; continue }
        if ($i ~ /^[;&|]/) { found=0; continue }
        if ($i ~ /^-/) continue
        print $i
      }
    }
  ')"
  rm_all_tmp=true
  if [[ -z "$rm_targets" ]]; then
    rm_all_tmp=false
  fi
  while IFS= read -r t; do
    [[ -z "$t" ]] && continue
    case "$t" in
      /tmp/*) ;;
      *) rm_all_tmp=false ;;
    esac
  done <<< "$rm_targets"
  if [[ "$rm_all_tmp" != true ]]; then
    ask "[destructive-op-guard] Destructive: 'rm' requires explicit approval."
  fi
fi

# docker compose down -v destroys volumes (including dev DB)
if printf '%s' "$cmd" | grep -Eq '\bdocker\b' && printf '%s' "$cmd" | grep -Eq '\bdown\b' && printf '%s' "$cmd" | grep -Eq '(^|[[:space:]])-[a-zA-Z]*v'; then
  ask "[destructive-op-guard] Destructive: 'docker compose down -v' will destroy volumes including the dev DB. Explicit approval required."
fi

# git-commit wrapper (atomic stage+commit) — but not --help/-h, which is read-only
if printf '%s' "$cmd" | grep -Eq '(^|[[:space:]/])git-commit[[:space:]]' \
  && ! printf '%s' "$cmd" | grep -Eq '(^|[[:space:]/])git-commit[[:space:]]+(--help|-h)([[:space:]]|$)'; then
  ask "[destructive-op-guard] git-commit (stage + commit) requires explicit approval."
fi

# destructive git subcommands (git-commit wrapper tokens stripped first — "commit" inside
# the hyphenated wrapper name would otherwise false-match \bcommit\b here too)
cmd_no_wrapper=$(printf '%s' "$cmd" | sed -E 's#(^|[[:space:]/])git-commit([[:space:]]|$)#\1GIT_COMMIT_WRAPPER\2#g')
if printf '%s' "$cmd_no_wrapper" | grep -Eq '\bgit\b'; then
  if printf '%s' "$cmd_no_wrapper" | grep -Eq '\b(commit|push|clean|checkout|restore)\b'; then
    ask "[destructive-op-guard] Destructive git operation (commit/push/checkout/restore/clean) requires explicit approval."
  fi
  if printf '%s' "$cmd_no_wrapper" | grep -Eq '\breset\b' && printf '%s' "$cmd_no_wrapper" | grep -Eq -- '--hard'; then
    ask "[destructive-op-guard] Destructive git operation (reset --hard) requires explicit approval."
  fi
  if printf '%s' "$cmd_no_wrapper" | grep -Eq '\bgit\b[^|&;]*\badd\b'; then
    deny "[destructive-op-guard] Don't run git add alone — it leaves staged changes that can collide with another session's commit. Use: ~/.claude/bin/git-commit <files> -m 'message'"
  fi
fi

# psql write operations
if printf '%s' "$cmd" | grep -Eq '\bpsql\b'; then
  if printf '%s' "$cmd" | grep -Eiq '\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|COPY)\b'; then
    ask "[destructive-op-guard] psql write operation (INSERT/UPDATE/DELETE/DROP/etc.) requires explicit approval."
  fi
  if printf '%s' "$cmd" | grep -Eq -- '-c\b'; then
    ask "[destructive-op-guard] psql -c (inline SQL command) requires explicit approval."
  fi
fi

# rsync --delete removes files on the target
if printf '%s' "$cmd" | grep -Eq '\brsync\b' && printf '%s' "$cmd" | grep -Eq -- '--delete\b'; then
  ask "[destructive-op-guard] rsync --delete can remove files on the target. Explicit approval required."
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
    ask "[destructive-op-guard] ssh with remote command (not just interactive login) requires explicit approval."
  fi
fi

exit 0
