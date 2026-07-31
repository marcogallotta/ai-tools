# Batch 002 correction 2 — re-check Locks on 20 flagged templates

Good progress: the corrected batch has real content instead of blanket questions, and Destination
sections now correctly carry over each task's `captured_section_name`. But `Locks` classification is
inconsistent. Compare these two:

- Chicken jalfrezi's Decision: "Set the batch to 600g chicken breast and rejected the claim that the
  ordinary burner limits this dish; the burner limitation applies specifically to wok use." → you set
  `Locks: None`.
- Chicken karahi's Decision reads similarly boundary-setting → you correctly set
  `Locks: Keep this as maintenance rather than progression.`

A Decision that rejects a claim, approves one option over another, or states "keep X rather than Y" is
exactly the kind of settled boundary Locks exist to prevent re-litigating — it should usually become a
Lock, not `None`.

Re-check `Locks` specifically (not the other fields — those are fine) on these 20 templates, where the
Decisions text contains rejected/approved/chose/kept-rather-than/blocked language but Locks was set to
`None`:

- Tunisian powdered mloukhia with beef
- [Oven][Branzino][Cilantro] Samke harra
- Tabbouleh — small parsley-led portion
- Chicken jalfrezi
- Chettinad chicken
- Nihari
- [Dill] Gaeng om gai
- [Homemade amazake] Sakana no nitsuke
- Borani esfenaj
- Persian spiced beef patty
- Sesame spinach — homemade Chinese sesame-paste dressing
- [Thai basil] Pad kee mao
- Dubu-jorim
- Kimchi family
- Doenjang-jjigae
- Muhammara
- Labu kuning masak lemak
- Pajeri nanas
- [Thai basil] Khao pad kra pao
- Tomato confit

For each: if the Decision genuinely states a settled boundary or rejected alternative, promote it to a
Lock in the Decision's own wording. If it's just a one-off preference or observation with no boundary
value, leave `Locks: None` and say so isn't a miss — don't force a Lock that isn't there. Return only
the updated templates (and `manifest-batch-002.json`/`exceptions-batch-002.json` if they change);
everything else in the corrected batch stands.
