# Batch 002 correction — stop blanket-flagging, attempt inference first

Your batch-002 output flagged the same boilerplate question on nearly every one of the 99 templates,
regardless of whether the answer was actually obvious from the source. Example: every single template
has the identical line `Role: [MIGRATION QUESTION — Marco must confirm main vs non-main and, if
non-main, its kind.]` — including on dishes like Nihari, chicken jalfrezi, and beef shank braise
where "main" is not in doubt, and on dishes like Sesame spinach dressing, Tabbouleh, and "White beans
— small sage vs rosemary comparison" where "non-main" (a condiment/test/comparison) is equally
obvious from the name, portion framing, and quantities already in the source note.

Re-run the transformation over all 99 templates with this rule: attempt each field from the source
content first; only emit a `[MIGRATION QUESTION]` when the content is genuinely silent or ambiguous,
not by default. Per field:

- **Role** (`dish-planning-protocol.md`, main | non-main): infer from the source note — single-portion
  or "test"/"comparison"/"repeat" framing, a component/condiment/side description, or a technique
  isolation (e.g. dressing, one ingredient's doneness) is non-main; a full multi-component dish meant
  to be eaten as the meal is main. Only ask when the note is genuinely ambiguous between the two.
- **Destination section**: use each task's own `captured_section_name` from
  `cooking-raw-capture/capture-manifest.json` — it's already the exact legacy section (cuisine) for
  that task. Do not ask this as an open question; format it as `<captured_section_name> —
  {{TARGET_SECTION_GID}}` per the transformation contract. Only flag if a task's captured section name
  is missing or clearly doesn't correspond to a coherent cuisine grouping.
- **Priors**: check the source note and `Research basis`/legacy agent lines you already extracted for
  a reference to an earlier related cook before declaring "not settled" — only ask if there's truly no
  such reference.
- **Locks / Locks classification**: you already preserve the `Human — Marco:` decision lines verbatim
  under Decisions. Classify each one as a lock or not from its own wording (a stated boundary/rejected
  alternative reads as a lock; a one-off preference note may not). Only ask when a specific decision's
  status is genuinely unclear, not as a blanket per-task question.
- **Research emphasis**: only populate/ask when the source note contains an actual open question or
  unresolved comparison — you're already extracting these in several templates (e.g. "Legacy open
  questions: Preferred vegetable doneness"); don't also add a generic question on top of that.
- **Exemptions**: default to `None` unless the source note actually raises a nutrition or other
  exemption-relevant point. Don't flag this by default on every task.

Return an updated `templates/`, `manifest-batch-002.json`, and `exceptions-batch-002.json` with only
the fields that survive genuine ambiguity after this pass. Everything else in the original batch
(source fidelity, placeholder handling, Korean status, Moinudin normalization) was correct — keep it.
