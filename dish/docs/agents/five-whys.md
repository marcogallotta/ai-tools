# Five Whys / root-cause analysis procedure

Use this shared procedure when a Dish agent is asked for a Five Whys or root-cause Five Whys analysis. It is an evidence discipline, not a role-authority change.

## Procedure

1. Start with a precise observed failure and the authoritative evidence that establishes it. Do not start from a preferred solution.
2. For each Why, state the immediate cause of the preceding established fact and verify that causal link with available evidence. Label every material statement as **VERIFIED FACT**, **TESTED/REJECTED ALTERNATIVE**, or **HYPOTHESIS/UNKNOWN**. A hypothesis never enters the factual causal chain until verified.
3. Treat “five” as a heuristic, not a required count. Stop when the evidence reaches an actionable root mechanism; continue beyond five when needed. Branch into multiple causal chains when evidence supports independent causes instead of forcing one linear story.
4. Distinguish the occurrence/root cause from contributing factors, the detection or escape failure, and downstream consequences or amplifiers when those are materially different mechanisms.
5. Do not stop at individual blame such as `agent error`, `human error`, `forgot`, `did not follow instructions`, `lack of training`, or `agent mistake` when a system, process, interface, or control made the failure possible or failed to prevent/detect it. Ask why that failure mode was permitted and why it escaped.
6. Keep cause and countermeasure separate. A desired fix is not evidence for a Why. Test a candidate root cause against contrary evidence, rejected alternatives, and the question: if this mechanism were prevented or removed, would the observed recurrence chain plausibly break?
7. Map each accepted root cause to a concrete, actionable fix and a verification or recurrence-prevention test. A fix must say what changes; another diagnosis or a generic policy reminder is not a fix.
8. For repository/process incidents, inspect every applicable authority class before declaring cause: current code/repository state; the owning Asana task and material chronology; involved GitHub PR, issue, commit, and history; and runtime/deployment evidence when the proposed mechanism crosses runtime. Mark a class **N/A** only when it is genuinely inapplicable. Material evidence that cannot be obtained remains **HYPOTHESIS/UNKNOWN**.
9. A root cause must identify an evidence-backed mechanism that the system can change or own. Reject incident labels, symptoms, blame, and generic prescriptions as roots.
10. If bounded research does not establish the mechanism with high confidence, report **INCONCLUSIVE**. Do not promote the likely explanation into a root cause or turn it into implementation work.

## Required output

### Durable RCA record

- **Problem statement / observed failure**
- **Evidence establishing the failure**
- **Why 1:** cause + evidence classification
- **Why 2:** cause + evidence classification
- **Why N:** cause + evidence classification
- **Root cause(s)**
- **Contributing factors**
- **Detection / escape failure**
- **Rejected alternatives / contrary evidence**
- **Countermeasure(s) mapped to root cause**
- **Verification / recurrence-prevention test**
- **Remaining uncertainty**
- **Owner / next action**

Keep that forensic detail on the owning RCA/task surface. Do not send it to Marco by default.

## Marco-facing output

Use plain language and no heavy jargon. Give each Five Whys result one short name tag and one bullet:

> **<name>:** <root cause>. **Confidence:** <high | inconclusive>. **Fix:** <concrete action>.

Include only the root cause, whether research established it with high confidence, and the actionable fix. When several Five Whys were requested, keep them as separate tagged bullets.

- When confidence is high, ask only: **Do you want me to add this to Asana?**
- When confidence is not high, label the result **INCONCLUSIVE**, state what research has not established instead of naming a likely root cause, give the safest actionable next step, and ask: **Do you want me to dig deeper or add this to Asana?**
- Do not write the RCA or corrective owner to Asana without Marco's affirmative answer.
- If Marco chooses deeper research, continue the bounded evidence investigation without manufacturing closure.
- If Marco chooses Asana for an inconclusive result, record investigation context only; do not create implementation work.
- If Marco chooses Asana for a high-confidence result, reconcile live owners first. Reuse, update, or reopen a still-authoritative matching owner; follow its successor instead when superseded; otherwise create only the minimum missing bounded corrective work. Link it to the RCA and read it back before claiming durable follow-through.
- If an authorized Asana write or readback fails after valid fallbacks, say that follow-through is incomplete and name the exact remaining write boundary.

## Reject these anti-patterns

- five unrelated reasons;
- five restatements of the same symptom;
- starting from a chosen solution and reverse-engineering causes;
- speculation presented as fact or an unsupported causal jump;
- stopping at individual blame;
- treating exactly five questions as mandatory;
- naming `lack of training` or `agent mistake` as root cause without asking why the system permitted it;
- mixing corrective action into the causal chain;
- calling chronology or correlation a cause without evidence.
- sending the forensic chain to Marco instead of the concise tagged result;
- giving a diagnosis without an actionable fix;
- using heavy jargon when plain language conveys the result;
- writing to Asana before Marco chooses it;
- creating implementation work from an inconclusive result.
