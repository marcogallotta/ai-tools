# Fast-track: testing, PR, or main

Fast-track is an execution-priority instruction for one bounded correction. It removes workflow
latency; it does not make the agent careless, widen scope, or turn Marco into the workflow
coordinator. Destination controls the route, regardless of whether Marco writes `fastrack`,
`fast-track`, or `fast track`.

## Procedure

### Route selection

- `fastrack to main`, `fast-track/direct/right to main`, or an equivalent explicit destination
  authorizes immediate direct publication to `main` for the exact bounded correction.
- `fastrack to PR` authorizes immediate branch/PR publication followed by fresh independent
  exact-head Review and Integration without waiting for ordinary CI.
- `fastrack to testing` authorizes the smallest reversible change on the active local testing
  surface, with no commit or push to `main`; after real-use testing, capture the coherent result on
  an isolated branch and continue through the PR route.
- Bare `fastrack` requires the agent to inspect urgency, consequence, reversibility, and desired
  feedback speed, recommend one destination with one concise consequence, and ask once. Marco's
  answer completes authorization.

An explicit destination is the complete route authorization. Do not ask again. The agent derives
task, path, branch, base, validation, Review, and handoff mechanics. If the named destination seems
materially inconsistent with the requested outcome, warn once with the concrete consequence and
ask `Are you sure?`; after confirmation or a direction to continue, execute without reopening it.

### Immediate-action latch

Latch the newest instruction as `{action, exclusions, completion condition}`. It atomically
replaces conflicting older objectives. A direct question or correction preempts pending tool work.
Before the requested first edit/publication, do not create an Asana task or authorization story,
prepare a worktree/test plan/PR/Review, regenerate unrelated artifacts, run tests, or inspect
adjacent consistency. Reuse already-ready machinery only when it is faster. Produce downstream
durable identity after the first requested artifact when Review or Integration needs it.

### Fast-track to main

1. Resolve current remote `main`, the exact path set, and the smallest coherent change.
2. Make only that change. For agentic/generated instructions, coherence includes canonical source
   plus every owned regenerated output; regeneration is artifact production, not testing.
3. Commit and push directly with non-force expected-head protection. Refuse concurrent `main`
   movement instead of overwriting it.
4. Read back remote `main` and report the landing immediately.
5. Observe CI afterward. Any tests, cleanup, formatting, adjacent work, or attributable repair goes
   to an isolated PR unless Marco separately fast-tracks that repair.

There is no PR, formal Review, pre-landing test, or CI wait on this route.

### Fast-track to PR

1. Create the smallest coherent branch commit and publish the PR without pre-publication test
   ceremony. For agentic/generated instructions, regenerate the complete owned set first.
2. Record the exact route grant and candidate identity on the durable PR/task surface without
   asking Marco for workflow fields.
3. Dispatch a fresh independent Reviewer for the exact head. The Review records
   `PRE-INTEGRATION TESTS TO RUN: NONE` unless Marco explicitly requested a pre-landing test.
4. A `MERGE` verdict proceeds directly through exact-head Integration without waiting for ordinary
   CI. A semantic `BLOCK` returns only the accepted blocker to Implementation.
5. Observe CI after landing and repair only candidate-attributable failures on an isolated PR.

### Fast-track to testing

1. Snapshot the exact primary-checkout pre-state for the bounded path set, then make the smallest
   reversible coherent change there. Do not commit or push `main`.
2. If agentic/generated instructions are involved, change canonical source and regenerate the full
   owned output set on that testing surface.
3. Let Marco test the behavior in real use. Do not substitute broad test ceremony.
4. When testing finishes, preserve the exact tested delta, restore the primary checkout to its
   snapshotted pre-state, apply the delta to an isolated owned branch, and continue through
   fast-track to PR. Never discard unrelated pre-existing changes.

### Live-target evidence

When correctness depends on an unknown host-visible integration, selected tool identity, prompt,
generated instruction, runtime fact, or target-agent behavior, repository prose and transport
labels are not evidence. Proactively run the smallest authorized live target-agent/environment
probe, or propose that single probe when only Marco can perform it. This rule is especially
important in fast-track and applies to ordinary work too. It never licenses a broad suite. If Marco
directs proceeding without the probe, proceed and record the uncertainty truthfully.

### Post-landing boundary

Main contains only the coherent fast-tracked correction. Post-landing testing, test fixes, cleanup,
formatting, generated material unrelated to coherence, and adjacent improvements stay off `main`.
Monitoring distinguishes candidate-caused, baseline, infrastructure, unrelated, and ambiguous
failures. Only candidate-caused failures belong to this correction.

## Mechanical minimum

Fast-track retains exact bounded paths, one change identity, canonical generation ownership,
non-force compare-and-set publication, concurrent-movement refusal, truthful evidence, and
authoritative readback. These are agent-owned mechanics, not pre-action forms or operator gates.
An explicit Marco `override` still supersedes this repository procedure for the exact action.
