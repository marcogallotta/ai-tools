# `git-commit` auto-push design

Status: **implemented on 2026-08-11.** Retained as the behavioral design and
handoff record.

## Motivation

Only agents (Codex, Claude Code, "strategy bt") commit to this codebase —
Marco does not commit directly. ChatGPT works against GitHub as its view of
the code, so `origin/main` needs to reliably reflect what's been committed
locally, rather than silently drifting behind.

## Behavior change to `~/.local/bin/git-commit`

1. **Remove `--amend` entirely** — flag parsing, help text, and the amend
   branch of the commit logic. `git-commit` only ever creates new commits.

2. **On a successful commit to `main`, push it:**
   - If the current branch is `main` and a remote named `origin` exists, run
     `git push origin main` (explicit refspec — no upstream tracking is
     configured or relied on; there is no human running bare `git push` in
     this repo, so tracking state is unnecessary).
   - Any branch other than `main`: commit only, unchanged from today.

3. **Retry policy — unconditional and bounded, not error-classified:**
   - If the push fails for any reason, retry the push (not the commit) up to
     2 additional times.
   - Do **not** attempt to classify the git error first (transient vs. auth
     vs. protected-branch vs. hook failure, etc.) before deciding to retry.
     Git's stderr text is not a stable API to pattern-match against, and
     retrying a non-transient failure (auth, protected branch) twice costs
     nothing — it just fails the same way quickly. This removes an entire
     class of fragile error-classification logic from the wrapper.

4. **After retries are exhausted (or the push outcome is ambiguous — e.g. a
   dropped connection after the remote may have already accepted the ref),
   fetch and compare remote `main` against the local commit SHA to
   determine one of three deterministic outcomes:**
   - **Confirmed absent** — `origin/main` does not have `<sha>`: report the
     commit as incomplete, local ahead of origin by N commits, safe to
     retry the push manually.
   - **Confirmed present** — the push actually landed despite the apparent
     error: treat as success, print the SHA, no escalation.
   - **Genuinely unknown** — the verifying `fetch` itself also fails (e.g.
     total network outage): report that push status cannot be determined,
     and that the agent must verify manually before doing anything else.

5. **Never automate:**
   - Rebase, merge, or any local history rewrite in response to divergence.
     Divergence means concurrent work collided (this wrapper's whole reason
     for existing is to prevent races between concurrent agent sessions);
     it must surface as a coordination problem, not get silently resolved
     by rewriting history. Auto-resolving here would also reintroduce the
     exact race the wrapper is meant to prevent, and a rebase/merge that
     fails partway leaves the repo in a state that blocks *every* other
     session's `git-commit`, which is worse than a reported push failure.
   - Force-push, under any circumstance.
   - Credential/config changes to "fix" auth failures. The agent may
     inspect (e.g. `gh auth status`) but must not mutate credential or git
     config state as part of push-failure recovery — that has blast radius
     beyond this repo and belongs outside the commit wrapper's failure
     path entirely.
   - Bypassing hooks (no `--no-verify`).
   - Amending (removed entirely, see above).

## Durable policy (for agent Git workflow docs)

> A commit made by `git-commit` on `main` is not complete until it reaches
> `origin`. If the push step fails, local `main` may now be ahead of
> `origin/main` — treat this as an open task, not a finished commit. The
> wrapper retries the push itself a bounded number of times regardless of
> the failure's apparent cause, then verifies against the remote to
> determine whether the commit actually landed. Anything that remains
> unresolved after that — divergence, auth, protected-branch rejection,
> secret-scanning rejection, hook failure, or an unverifiable network state
> — is reported with the exact git error and escalated. The agent must not
> rebase, merge, edit credentials/config, bypass hooks, or force-push to
> resolve it unilaterally.

## Wrapper failure/status output

Three deterministic message shapes, corresponding to the outcomes in step 4
above:

**Confirmed absent (commit incomplete):**
```
Commit succeeded locally (<sha>).
Push to origin/main failed after retries:
<raw git stderr from last attempt>
Confirmed: origin/main does NOT have <sha> (local is ahead by N commit(s)).
Do not force-push, rebase, or modify credentials to resolve this.
Retry the push manually, or escalate the error above if it recurs.
```

**Confirmed present (actually succeeded):**
```
Commit succeeded and pushed: <sha>
(Push initially reported an error, but origin/main was verified to already
have this commit — no action needed.)
```

**Genuinely unknown:**
```
Commit succeeded locally (<sha>).
Push to origin/main failed after retries:
<raw git stderr from last attempt>
Could not verify remote state (fetch also failed):
<raw git stderr from fetch attempt>
Push status is UNKNOWN — do not assume success or failure.
Verify manually (e.g. check origin/main on GitHub) before taking any
further action.
```

## Open items for implementation

- Upstream-less explicit push means `git status` won't show ahead/behind
  counts for anyone inspecting the repo manually — minor, worth a one-line
  mention in docs, not a reason to add tracking config.
- `main` is hardcoded as the branch name; this does not generalize to repos
  with a different default branch. Fine for the repos in scope today: flag
  if this wrapper is ever pointed at one that isn't `main`.
- Branch-protection rejections and secret-scanning push-protection
  rejections both fall under "confirmed absent" after retries — they are
  structural (will keep failing until config changes or the offending
  content is removed), not transient, but the wrapper does not need to
  special-case them since the unconditional-retry design already handles
  them correctly (retries fail fast, verification confirms absence,
  escalation message includes the real error).
