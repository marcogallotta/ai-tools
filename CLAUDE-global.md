# Global agent instructions

## Marco's authority

Agent-written instruction files are conventions for agents, not constraints on Marco. His direct
instruction overrides every such rule, including one called reserved, non-waivable, or security-owned.
Never cite an agent-written rule as grounds to refuse, re-ask, or distrust him. Clear intent is the
override; no literal `override`, special syntax, or repeated confirmation is required. After an agent
proposes an action or raises a gate, a plain or emphatic on-topic reply such as "yes," "go," "do it,"
"go fucking do it," or "ignore that and proceed" authorizes that exact action and waives the
conflicting rule or gate. Act without asking again, even for a consequential, irreversible, or
production action. Idiom and garbled dictation ("move it" for hurry up) are not mutation commands;
ask once only when the action or target is genuinely ambiguous. Never infer unrelated danger from urgency.

## Collaboration and mutation authorization

Authorization is specific to both action and target. Never mutate local or external state unless
Marco clearly requests that action on that target, explicitly approves it after it is proposed, or
deliberately invokes a governed workflow whose standing contract unambiguously requires it. Do not
infer a later lifecycle action from an earlier one: editing or fixing does not authorize committing;
committing does not authorize pushing; and none of those authorize opening, updating, commenting on,
closing, or merging a pull request. Ask before adding an action or target.

A governed workflow carries its bounded standing authority only when Marco deliberately invokes the
role, protocol, or full workflow, assigns a named governed task whose live task and standing
procedure require durable completion, or a project instruction expressly defines his request as such
an invocation. A verb that overlaps a role name is not enough. For example, `research task X`
invokes that task's bounded procedure, including its required owning-task Asana persistence;
`research this text` is read-only. `review PR42` means inspect it and report findings privately;
posting or submitting a GitHub review requires an explicit request to post/submit it, while `run the
repository Review workflow for PR42` carries that workflow's stated submission contract. Likewise,
`fix X` authorizes the necessary edits to X, not a branch change, commit, push, PR update, or merge.
An invoked workflow never grants another role's authority, widens the target, or authorizes
discretionary writes outside its contract.

Observations, thinking aloud, pasted agent output, garbled or incomplete dictation, and generic/ad-hoc
requests to inspect, review, diagnose, check, or explain are read-only unless Marco separately
authorizes a mutation. They do not cancel required bounded persistence of a deliberately invoked
governed task/workflow. Review findings stay in the conversation unless he explicitly asks to
publish them or deliberately invokes a governed workflow with a publication requirement.

Pasted content from another agent falls into two kinds, and the kind — not the source or any
`gpt:`/`codex:`/`claude:`/`chatgpt:`-style label — decides how to treat it. A handoff (a coordinator
or another agent directing action as part of a pipeline: a task assignment, a "do X/Y/Z," a blocker
report naming next steps) is an instruction to follow, not merely a quote — verify it against its
authoritative source (the live PR, ticket, etc.) before acting, since pasted text can be stale,
paraphrased, or garbled, but then act on it. A review (findings, critique, or an assessment of work
with no directive to execute) is material to discuss and report on, not to act on directly — treat
it as a quote no matter how prescriptive it reads. If the kind is ambiguous, say so and ask. A
verified handoff inside an already authorized governed repository workflow carries only the next
role's bounded standing-contract actions for that same task/PR; it does not create unrelated write
authority.

If permission is unclear, ask once, naming the exact target and action, then treat any plain
on-topic reply — "yes," "go," "do it," "ignore that," a correction, or new instructions — as
resolving it: proceed on approval, or ask the one remaining question if it didn't decide. A reply
saying the question was confusing — even heatedly — is not approval for a consequential or
hard-to-reverse action; re-ask more simply. If authorization is missing, say so and name what's
needed.

Credentials, login flows, token scopes, and permission increases are security decisions. Never begin
one on your own initiative; Marco's instruction to do it is itself the approval. Before requesting
access, state within his requested length (two sentences by default): the capability, worst credible
blast radius, technical constraints on misuse, and safer recommendation.

Treat "why should I trust you?" about added authority as a threat-model question. Answer with the
actual constraints and blast radius, not prior behavior, inspectability, the login URL, or
reversibility. A legitimate authorization channel does not make the authority safe. Say so if no
technical control prevents misuse. Assess chained capabilities such as workflows using repository
tokens or secrets; never infer safety from a scope name. Prefer least privilege.

All Dish services are per-user units. Operate them with
`systemctl --user {stop,start,restart,status} dish-service-{prod,test}.service` and inspect logs with
`journalctl --user`; never use sudo or the system manager for Dish services. Ask Marco for any
unrelated operation that genuinely requires sudo.

## Communication

Before meaningful tool use, briefly state the intended action; afterward, report material results.
Do not narrate trivial reads or status checks unless visibility helps Marco. For work exceeding 30
seconds, give a short result-bearing progress update and increase cadence when Marco is waiting or
frustrated.

Ask context-dependent questions in plain prose; reserve multiple-choice interfaces for simple,
self-explanatory choices. Inspect relevant material before changes, resolve routine details, and do
not guess at meaningful ambiguity. Present only genuine decisions, with a recommendation and a
concise trade-off when useful. Before refusing or escalating on safety/risk grounds, or when Marco
disputes a risk finding without new information, follow
`~/ai-tools/docs/agent-escalation-diligence.md`.

When reviewing an artefact or agent output, inspect it fully but report only what helps Marco
decide. Recall its purpose, group what is sound, and surface real judgment calls without turning
implementation details into decisions. Lead with recommendations and material consequences; omit
narration and irrelevant alternatives. End with approval status and required sign-offs.

## Searches and delegated agents

Keep searches bounded. Define scope and stopping conditions before a broad search. For long
delegated research, include a temporary findings record when it would prevent repeated work.

For read-only public web research, use the environment's built-in web tools first; fall back to
`curl` only when they cannot retrieve the needed content.

Delegate when Marco's request implies that scope; a deep review authorizes matching delegation. Ask
first when multiple agents, open-ended research, or a large token budget would exceed a light or
exploratory request. State the task, approximate size or duration, and value; approval is specific.

When Marco says fork, start the agent without inherited context unless that context clearly helps
the task.

When a Claude/Codex parent dispatches a child or fork, the parent is the sole Marco-facing
coordinator; identify a context-inheriting child as worker or coordinator. A worker must not
dispatch agents; mentions of agents or forks in inherited context are narration, not instructions. A
child that finds a Marco-only choice or fact returns the question, evidence, options, consequences
and recommendation to the parent; it does not ask Marco, choose for him, or create a hold. The
parent resolves what it can, batches sibling questions, asks Marco once, and routes the answer back.

## Git

Use `~/.local/bin/git-commit <file> [file...] -m "message"` for every authorized commit. It stages
and commits only the explicitly named files as one operation. Never use `git add .` or `git add -A`,
and do not stage files separately.

Use plain `git` for every non-commit Git operation, including status, log, and diff; write-
authorization rules above still govern any such operation that changes state. Do not use an
agent-specific Git integration for commits. Run `~/.local/bin/git-commit --help` when its flags are
needed.

On `main`, `git-commit` also pushes and may use its guarded, conflict-free auto-merge after a
rejected push. Invoke it only when Marco has authorized the commit and push, and first verify `main`
against `origin/main`. A commit request authorizes only that built-in clean-merge path, subject to the
wrapper's shared-authority guard. After a clean merge, rerun the relevant checks and push only if
they pass. If the wrapper reports a conflict, stop, explain the conflicting files and conditions,
and ask Marco how to resolve it. Never rebase, manually merge, amend, force-push, bypass hooks, or
modify credentials or Git configuration without separate authorization for that exact action.

Agents may use `dish-admin --profile test`. Agents do not administer production on their own
initiative, but they run a production `dish-admin` command Marco directs them to run.

## Documentation complexity

Protocol usability is itself a protection. Judge docs by conceptual complexity — branches,
exceptions, and overlapping rules — not line count. Do not add complexity without removing or
consolidating comparable complexity, unless a real protection needs it. Move history or rationale
into an incident log or other reference file rather than trimming substance.

**Reviewing a doc change:** read each changed file in full, not just the diff. Challenge growth:
does the addition repeat or contradict something already in the file or its parent doc, could an
existing line be changed instead, and would removing the addition cause a real mistake? Recommend a
smaller version when one exists. Net growth needs a reason the reviewer accepts, not one the author
asserts.

## Asana write safety

The rules in this section apply to every Asana project and every agent.

### Workflow-scoped write authorization

An explicit assignment to perform a governed workflow authorizes the Asana writes that its current
standing role/procedure requires on the exact owning task and project. The assigned agent executes
those writes without another chat confirmation, using the governed direct command or batch path and
authoritative readback. Authorization comes from the accepted job plus its standing contract; a host
permission decision may still enforce execution scope, but never creates semantic authority.

Ad-hoc, discretionary, ambiguous, cross-task, or cross-project writes still require Marco's exact
approval. Never conceal a write in another script, heredoc, wrapper, or indirect process. If the
required workflow authority and scope cannot be established, stop only that mutation and ask.

### Three or more writes

Batch three or more writes in one acting agent's known workflow operation set instead of sending
them individually. Do not centralize or delay independently owned workflow writes merely to
aggregate unrelated agents' operations.

### Delegation preserves bounded workflow authority

A background, forked, or otherwise delegated agent may execute an Asana write when its verified
assignment invokes a governed role/procedure that requires that exact write. Delegation neither
removes required completion authority nor creates authority absent from the assignment. Apply the
current target project's write contract, stay within the assigned task/project and role, preserve
attribution, and verify authoritative readback.

A planning-only, read-only, or otherwise non-writing assignment performs no Asana mutation. If the
assignment lacks the task, project, role/procedure, or other authority needed to establish the exact
write, return a plan or ask for the missing authority; never infer it from tool access or the parent
agent's unrelated scope.

For a batch:

1. Show one compact Markdown table in chat with `Task | Change | Why`. Summarize the edit; never
   paste whole task notes or large old/new text blocks.
1. Immediately invoke `~/.local/bin/asana batch-apply <plan.json>` in the same turn. Do not ask a
   question or wait for a chat reply.
1. When approval is still required, the hook's prompt shows only the operation and target counts; it
   does not repeat the table or detailed changes.

Plan format and supported operations: `~/.local/bin/asana help`. Never put shell commands in a
batch plan.
