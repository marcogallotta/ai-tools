# Affected-test graph

The affected-test graph is the repository certification selector. `targets.json` owns stable executable target identity and runtime metadata; `edges.json` owns explicit source, runtime, and harness consumer relationships. Test files already named by `dish/test_selection/ownership.csv` are bootstrapped mechanically into stable file targets, so migration does not require a flag day. Deterministic Python import edges add targets where the relationship is directly visible; generated edges are additive only in V1.

## Migration authority

An unmigrated path uses the legacy adapter and remains visible in `legacy_adapter_paths`. Graph edges may augment that evidence immediately. An `authoritative` V1 edge must be an exact path that exists in both BASE and CANDIDATE. Patterns are augment-only, and a candidate-added or rename/move/split destination cannot become authoritative in its introducing candidate.

Authoritative cutover is closed: its disposition keys must exactly equal the union of independently produced BASE and CANDIDATE legacy obligation keys. A `mapped` disposition must resolve to a target with the same execution boundary. A `retired` disposition requires an allowed reason and durable provenance. Missing, duplicate, extra, or wrong-boundary dispositions invalidate the cutover and fail closed.

## Self-change and fallback

`scripts/test_impact_graph.py` emits `dish-test-obligations-v1` from its own tree and inputs. `scripts/test_impact_arbiter.py` validates and unions BASE and CANDIDATE envelopes with schema/set operations only; it cannot traverse dependencies or remove an obligation. Removed or unrunnable preferred targets fall back within the same execution boundary. A missing compatible boundary fallback, missing BASE engine, or incompatible BASE arbiter on a self-change selects every boundary.

The catalog records target size, requirements, profiles, child launchers, and boundary fallback identity. `LOCAL_FAST` executes eligible small targets and reports larger evidence in `hosted_required_targets`; `PR_EXACT_HEAD` executes all affected targets for the reviewed source head; `POSTMERGE_FULL` remains the comprehensive selector-miss backstop.

## Replay and fingerprint

`ci/test-impact/replay.json` preserves historical selection cases, including selector miss `31955770608`. Run:

```sh
python3 scripts/test_impact_graph.py replay --json
```

Every plan also emits `dish-impact-fingerprint-v1`. It is a deterministic advisory summary of paths, targets, guarantees, boundaries, resources, fallback, and legacy-adapter use. It is not textual-conflict, scheduling, ownership, Review, or merge authority.
