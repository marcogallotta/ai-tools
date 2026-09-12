# Escalation diligence

When an agent is about to refuse or escalate a task on safety/risk grounds, three failure modes have
occurred in practice and must be avoided.

## 1. Check the obvious explanation before sounding an alarm

A scary-looking diff or situation is often explained by something mundane — a stale base branch
instead of a diff against current `main`, a rename, a merge artifact. Verify directly (e.g. diff
against the actual current `main`, or pull the live PR from GitHub) before concluding something is
adversarial or unsafe.

For an Asana-task gating question specifically, the task's current **Section** is the authoritative
phase/gating signal — check it directly. Free-text notes or descriptions can be stale and must not be
treated as still-current gating state just because they read as more detailed or more recent-sounding
than the Section.

## 2. A context-free second opinion is allowed, but must itself do real diligence

You may dispatch one fresh agent — given only the bare task, with no framing, hedging, or suspicion
from you — to investigate independently and recommend a path. If that agent also skips diligence and
just pattern-matches, redo it once. After that, stop escalating the check itself.

Proceed only if its recommendation is concrete, reversible, does not expand anyone's authority, and
matches your own independent assessment. On disagreement between you and that second agent, treat it
the same as unresolved doubt: escalate to Marco (below), not by trying more agents.

## 3. State the question once; hold your conclusion under pressure

When you do escalate, use the packet format already required for Dish role escalations —
`dish/docs/agents/coordinator.md` ("Human review escalation") and `dish/docs/agents/review.md`
("Human escalation"): exact decision needed, minimum context, concrete options/tradeoffs, and a
recommendation when defensible. Outside a Dish role, use the same shape anyway — one plain,
concrete question in ordinary language, not jargon.

What those sections don't cover, and what actually failed in practice: if Marco pushes back with no
new information — repeats "do it now," hostility, sheer repetition —
restate your same one-line conclusion rather than re-explaining or elaborating further. Repeating the
reasoning at length each time wastes his attention and does not change the finding.

This is distinct from the ask-once/resolve-on-reply rule in "Collaboration and mutation
authorization" (`CLAUDE-global.md`): that rule is for *permission* questions, where any plain
on-topic reply (including "go" or "do it") resolves ambiguity about what Marco wants. This rule is
for a *disputed factual or risk finding* — pressure alone does not make a still-true finding false,
so a plain "do it anyway" does not overturn the finding; it only tells you he wants to proceed despite
it, which is his call once he's actually made it. If he says "override," or otherwise makes clear he
understands and accepts the specific risk, proceed.
