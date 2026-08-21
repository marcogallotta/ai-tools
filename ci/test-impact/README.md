# Affected-test graph

The affected-test graph is the repository certification selector. `targets.json` owns stable executable target identity and runtime metadata; `edges.json` owns explicit source, runtime, and harness consumer relationships. Test files already named by `dish/test_selection/ownership.csv` are bootstrapped mechanically into stable file targets, so migration does not require a flag day. Deterministic Python import edges add targets where the relationship is directly visible; generated edges are additive only in V1.

## Migration authority

The independently produced BASE obligation envelope is the safety floor. Repository/path classification is a routing prior only: a `.py` file or `scripts/**` path never proves that an inherited frontend, PostgreSQL, browser, or broader Python obligation is incidental.

Every path is classified explicitly:

- `EXACT_PROVEN_TARGET` — exact BASE-authoritative mapping and exhaustive mapped/retired disposition proof permit the proving target;
- `KNOWN_BOUNDARY_FALLBACK` — exact precision is missing but retained BASE obligations establish one execution boundary, so that boundary's conservative fallback runs;
- `BASE_OBLIGATION_UNION` — multiple retained BASE boundaries remain unresolved, so their union runs rather than being collapsed from file type;
- `TRUE_UNKNOWN_ALL_BOUNDARY` — trustworthy BASE identity/boundary cannot be established, so every boundary runs.

An unmigrated path remains visible in `legacy_adapter_paths`. Graph edges may augment that evidence immediately. An `authoritative` V1 edge must be an exact path that exists in both BASE and CANDIDATE. Patterns are augment-only, and a candidate-added or rename/move/split destination cannot become authoritative in its introducing candidate.

Authoritative cutover is closed: its disposition keys must exactly equal the union of independently produced BASE and CANDIDATE legacy obligation keys. A `mapped` disposition must use a target named by the authoritative mapping and remain in the same execution boundary. A `retired` disposition requires an allowed reason plus one or more machine-readable `replay_ids`. Each replay must include the exact path and prove the retired boundary/obligation unnecessary via `must_not_boundaries` or `must_not_obligations`. Missing, duplicate, extra, unproved, or wrong-boundary dispositions invalidate the cutover and fail closed.

## Selector gaps

`KNOWN_BOUNDARY_FALLBACK` and unresolved `BASE_OBLIGATION_UNION` plans emit stable `selector_gaps`. A gap records its stable gap ID, changed path, classification, BASE/CANDIDATE graph identities, retained/fallback boundaries, missing precision reason, responsible graph surface, and recurrence count. Exact-head PR certification adds PR/head/review/run evidence before persisting the plan artifact. Passing fallback evidence does not remove the gap; closure requires the missing exact mapping/retirement proof plus replay showing the gap no longer occurs.

`TRUE_UNKNOWN_ALL_BOUNDARY` remains a fail-closed all-boundary safety state rather than being mislabeled as a known-boundary precision gap.

## Self-change and fallback

`scripts/test_impact_graph.py` emits `dish-test-obligations-v1` from its own tree and inputs. `scripts/test_impact_arbiter.py` validates and unions BASE and CANDIDATE envelopes with schema/set operations only; it cannot traverse dependencies or remove an obligation. Removed or unrunnable preferred targets fall back within the same execution boundary. A missing compatible boundary fallback, missing BASE engine, or incompatible BASE arbiter on a self-change selects every boundary.

The catalog records target size, requirements, profiles, child launchers, and boundary fallback identity. `LOCAL_FAST` executes eligible small targets and reports larger evidence in `hosted_required_targets`; `PR_EXACT_HEAD` executes all affected targets for the reviewed source head; `POSTMERGE_FULL` remains the comprehensive selector-miss backstop.

## Replay and fingerprint

`ci/test-impact/replay.json` preserves historical/adversarial selection cases, including selector miss `31955770608`, the `scripts/integration_certification.py` cross-boundary orchestration case from f54e098a / PR #77/#79, and the exact nine-path PR #182 lifecycle replay. Run:

```sh
python3 scripts/test_impact_graph.py replay --json
```

Replay reports the conservative BASE baseline and proposed selection, including lost boundaries/targets. Any lost boundary without an exact replay-backed retirement proof is a replay failure. This is the proof gate that permits known lifecycle paths to narrow while keeping cross-boundary orchestrators broad until separately proven.

Every plan also emits `dish-impact-fingerprint-v1`. It is a deterministic advisory summary of paths, targets, guarantees, boundaries, resources, fallback, and legacy-adapter use. It is not textual-conflict, scheduling, ownership, Review, or merge authority.
