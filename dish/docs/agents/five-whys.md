# Five Whys / root-cause analysis procedure

Use this shared procedure when a Dish agent is asked for a Five Whys or root-cause Five Whys analysis. It is an evidence discipline, not a role-authority change.

## Procedure

1. Start with a precise observed failure and the authoritative evidence that establishes it. Do not start from a preferred solution.
2. For each Why, state the immediate cause of the preceding established fact and verify that causal link with available evidence. Label every material statement as **VERIFIED FACT**, **TESTED/REJECTED ALTERNATIVE**, or **HYPOTHESIS/UNKNOWN**. A hypothesis never enters the factual causal chain until verified.
3. Treat “five” as a heuristic, not a required count. Stop when the evidence reaches an actionable root mechanism; continue beyond five when needed. Branch into multiple causal chains when evidence supports independent causes instead of forcing one linear story.
4. Distinguish the occurrence/root cause from contributing factors, the detection or escape failure, and downstream consequences or amplifiers when those are materially different mechanisms.
5. Do not stop at individual blame such as `agent error`, `human error`, `forgot`, `did not follow instructions`, `lack of training`, or `agent mistake` when a system, process, interface, or control made the failure possible or failed to prevent/detect it. Ask why that failure mode was permitted and why it escaped.
6. Keep cause and countermeasure separate. A desired fix is not evidence for a Why. Test a candidate root cause against contrary evidence, rejected alternatives, and the question: if this mechanism were prevented or removed, would the observed recurrence chain plausibly break?
7. Map each accepted root cause to a corrective/preventive countermeasure and a concrete verification or recurrence-prevention test. Record unresolved uncertainty rather than manufacturing closure.
8. For repository/process incidents, reconcile the relevant live GitHub, Asana, and runtime evidence before declaring cause. A healthy current state does not erase a documented historical or process defect.

## Required output

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
