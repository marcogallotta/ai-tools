# Correction 3 — canonical parser format defect (batch-002-correction-2)

`batch-002-correction-2` passed `validate_batch_002.py` (99/99), but that script checks content, not
exact adjacency. Dish's real parser (`dish_tool/task_document.py`, `parse_task_document`) is stricter
and rejects the batch as currently formatted. This has been independently confirmed by reading that
parser's code and the actual template files — it is a real defect, not a false alarm.

## Defect 1 — blank line before `## PROCESS RECORD` (affects effectively all 99 templates)

`parse_task_document` requires the line immediately after the `---` separator to be exactly
`## PROCESS RECORD`, with no blank line between them:

```python
if separator + 1 >= len(lines) or lines[separator + 1] != PROCESS_HEADING:
    raise DocumentParseError("process_heading_missing", ...)
```

Every checked template currently has:

```
---

## PROCESS RECORD
{{MIGRATED_DISH_STATE_BLOCK}}
```

i.e. a blank line between `---` and `## PROCESS RECORD`. This must become:

```
---
## PROCESS RECORD
{{MIGRATED_DISH_STATE_BLOCK}}
```

No blank line, anywhere, between the `---` line and the `## PROCESS RECORD` line, in every one of the
99 templates. Regenerate all 99 with this fixed — do not hand-patch a subset and leave the rest as an
exercise for later.

## Defect 2 — legacy-preserved prose reusing `---` / `PROCESS RECORD` as literal text

`parse_task_document` finds the separator with `lines.index("---")` — the *first* line in the whole
document that is exactly `---`. Some templates preserve a verbatim legacy note that itself contains a
bare `---` line and/or the literal heading text `PROCESS RECORD` (quoting the source task's own old
notes). When that legacy block appears above the real canonical `---`/`## PROCESS RECORD` block, the
parser locks onto the legacy block's `---` as *the* separator — the real state block after it is never
reached, and the document fails to parse as intended (not just "duplicate fields," but effectively the
wrong document structure).

Two templates are confirmed affected by name: **Sesame spinach — homemade Chinese sesame-paste
dressing** and **Pad kee mao — spicy Thai chicken drunken noodles**. Do not assume these are the only
two — audit every template for any legacy-preserved block that contains a bare `---` line or the
literal string `PROCESS RECORD`, and fix all of them, not just the two named.

Fix: keep preserving the exact legacy content (do not delete or paraphrase it — that's still required
by the transformation contract), but stop it from using the two literal markers the canonical parser
treats as structural:

- Never render a bare `---` line inside the legacy-preserved block. If the original note used `---` as
  its own internal divider, either drop that line or replace it with something that is not a
  standalone `---` (e.g. prose, or indent it, or use a different rule character/length).
- Never render the literal heading text `PROCESS RECORD` (with or without `##`) inside the
  legacy-preserved block. Quote it descriptively instead if you need to reference that the legacy note
  had its own process-record section (e.g. "the legacy note's own process-record heading read: ...").

The one real canonical `---` and one real canonical `## PROCESS RECORD` — the ones immediately
preceding `{{MIGRATED_DISH_STATE_BLOCK}}` — must be the *only* occurrences of either in the document.

## What to do

1. Regenerate all 99 templates in `batch-002-correction-2` fixing defect 1 (no blank line before
   `## PROCESS RECORD`).
2. Audit every template for defect 2 (legacy-preserved `---` or `PROCESS RECORD` text) and fix every
   instance found, not only the two named above. Report the full list of GIDs you had to fix for
   defect 2.
3. Do not touch any other content — Planning brief, Decisions, Research basis, Material changes,
   placeholders, section names — this is a formatting-only correction.
4. Re-run `validate_batch_002.py` yourself against the corrected batch and confirm 99/99 still passes
   (it checks content, not this structural detail, so a pass here doesn't prove the fix — see below).
5. Additionally, for at least a handful of templates (ideally all 99, since you have the tools to run
   code), actually call `dish_tool.task_document.parse_task_document` from this repo against each
   corrected template's PROCESS RECORD section (title/recognition/body + `---` + `## PROCESS RECORD` +
   the rest) to prove it parses without `DocumentParseError`. That's the real acceptance bar, not the
   validator script, since the validator doesn't invoke the real parser.
6. Return a new `batch-002-correction-3.tgz` with the same structure as before (`templates/`,
   `manifest-batch-002.json`, `exceptions-batch-002.json`, `source-notes/`), plus a short note listing
   every GID touched for defect 2.
