# Lifecycle economics and timing telemetry V1A

## Authority boundary

This telemetry is observational only. It is not a scheduler, queue, policy engine, merge gate, lifecycle state machine, completion authority, or host-selection authority. A telemetry read, adapter, write, aggregation, or report failure is telemetry degradation only and MUST NOT change Implementation eligibility, Review, Integration, merge, priority, or completion truth.

`scripts/lifecycle_economics_telemetry.py emit` and `capture-pr` deliberately exit zero when telemetry degrades. Their JSON result exposes the degradation separately. Lifecycle code must never branch on that result.

## Closed provider-neutral source-event envelope

Each JSONL source event uses `schema_version: lifecycle-economics-source-event/v1` and a source-owned or source-derived exact `source_event_id`. The collector de-duplicates only the exact `(source, source_event_id)` pair.

The schema is closed. Unknown top-level keys and unknown nested keys are rejected; arbitrary metadata, prompt/chat/message text, diffs, raw payloads, descriptions, source code, and transcripts therefore cannot be persisted under alternate field names. The only permitted evidence surfaces are identifiers, timestamps, source-owned stage/state names, exact usage totals, exact source cost units, operator-action identities, review identities, outcomes, and size counts.

Required fields are `source`, `source_event_id`, `observed_at`, `series`, and `repository`. `series` is `pr_flow` or `repository_health`.

Lifecycle identity fields (`task_id`, `pr_number`, `lineage_id`, `generation_id`, `replaces_generation_id`, `head_sha`, `attempt_id`) are exact facts when available. Missing or unlanded identity is emitted as literal `UNKNOWN`; the collector never synthesizes a lineage/generation ID. Events with unknown lineage/generation are deliberately kept as separate observations rather than collapsed into a fabricated generation.

Optional exact evidence is allowlisted as:

- `execution`: `execution_id`, `host`, `provider`, `model`, `config`, `snapshot`;
- `timing`: source-owned `stage`, exact `duration_ms`, optional exact `estimate_ms`;
- `review`: exact `round_id`, `review_id`, reviewed head, and source progression phase;
- `operator`: `required`, exact category, exact `action_id` when available, exact duration when available, `override`, and exact `gate_id`;
- `usage`: exact tool calls/tokens and exact provider charge with source currency/unit;
- `outcome`: source-owned `kind`, exact evidence ID, and authoritative/terminal flags;
- `size`: exact files/additions/deletions/lines-changed counts.

An absent execution or usage observation normalizes to literal `UNKNOWN`. This is distinct from known zero: `total_tokens: 0` or cost amount `0` remains a real known zero only when the source supplied it exactly.

For timing, `duration_ms` is the source-owned actual interval and `estimate_ms` is an exact source estimate when available. Generation records retain both exact values and signed `estimate_error_ms = duration_ms - estimate_ms`; if either side is unavailable, estimate error remains unknown rather than being imputed.

## Authoritative source adapters

`events_from_authoritative_pr_sources()` adapts the repository's existing `PRLifecycle` snapshot plus raw GitHub PR/review/comment facts. It does not duplicate lifecycle legality or stage membership. It carries the source-owned state/verdict/marker values, exact GitHub IDs/timestamps/head/size/outcome, and then discards source bodies.

The adapter recognizes only current durable source facts it can bind mechanically:

- a GitHub/lifecycle snapshot from the exact PR/head/state;
- formal exact-head Review submissions, using the durable review ID, submitted timestamp, reviewed commit and formal verdict;
- durable `dish-human-notice:v1` markers for the bounded currently-known operator-action categories;
- an explicit `GATE WAIVED BY MARCO OVERRIDE:` line on a formal Review as one exact override action tied to that review ID. Free-form text after the marker is **not** a gate identity. Same-gate recurrence is available only when the durable line uses the canonical machine-readable form `GATE WAIVED BY MARCO OVERRIDE: gate=<canonical-id>`; otherwise `gate_id` remains `UNKNOWN`.

It does **not** infer provider/model/run economics from GitHub actors, chat text, or timing adjacency. Unavailable execution/lineage/generation IDs remain `UNKNOWN`.

`safe_capture_pr()` reads the current PR, formal reviews, and PR comments from the existing GitHub backend and appends adapted events fail-open. The one-shot CLI uses the repository's current `GitHubREST`/`AsanaREST`/`LifecycleEngine` seams; it is not a watcher or scheduler:

```sh
python3 scripts/lifecycle_economics_telemetry.py capture-pr \
  --pr-number 168 \
  --output ~/.local/state/dish/telemetry/lifecycle-economics.jsonl
```

The acting role or one-shot manual lifecycle tooling may call the same adapter seam where observational capture is desired; the adapter result grants no lifecycle authority.

## Operator-touch categories and exact action counting

Operator-required events use exactly one category:

- `design_risk_product_decision` — productive Marco design/risk/product judgment;
- `manual_relay_or_queue_routing` — routine manual relay/routing;
- `status_reconciliation_or_repair` — stale GitHub/Asana reconciliation/status repair;
- `override_waiver_permission_prompt` — override, waiver, or permission friction;
- `integration_merge_controller_babysitting` — manual Integration/merge/controller babysitting;
- `workflow_incident_firefighting` — incident work caused by Development Workflow process failure.

`operator.required=false` is never counted merely because a source actor is human. `action_id` is source-local unless its source contract says otherwise, so exact de-duplication is scoped by `(source, execution identity, action_id)`. Repeated observations of the same scoped durable action count once per generation, while identical local ID strings from different sources/executions remain distinct. With no exact action ID, each unique source event remains a distinct exact observation and duration stays `UNKNOWN` unless the source supplies it.

## Execution-bound economics

Each generation record keeps a lineage-level diagnostic total **and** `execution_economics` buckets keyed by the complete exact execution identity (`execution_id`, host, provider, model, config, snapshot). Usage/cost from one bucket is never allocated to another bucket. Events without exact execution identity stay in an `UNKNOWN` execution bucket.

Retries remain distinct by exact source-owned attempt identity. `attempt_id` is de-duplicated only within its `(source, execution identity)` namespace; identical local strings from different providers/sources therefore remain separate attempts. If any attempt observation in a generation/execution bucket lacks exact attempt identity, that generation is excluded from the exact attempt-count distribution and contributes to unknown-generation/event coverage instead of appearing as a misleading zero-attempt sample. Review rounds and BLOCK/fix/re-review progression are counted only from explicit source review identifiers/phases. Exact monetary evidence is grouped by the source `(currency, unit)` and kept as exact decimal strings. There is no exchange-rate conversion, token-price inference, or current-list-price heuristic.

A generation terminal outcome is reported separately from a bucket's own source-terminal event. Diagnostic `generation_outcomes` under an execution identity means only that the exact identity appeared in that generation; it is not a causal quality claim about that provider/model.

Scheduled/full-regression health belongs to `repository_health`; it is excluded from PR-flow duration/economics distributions.

## Derived reports

`collect` emits one observational generation record for each exact `(repository, series, lineage_id, generation_id)` and separate unknown-identity observations where grouping cannot be proven.

The diagnostic report includes:

- source-stage actual timing count/p50/p90, exact estimate count/p50/p90, and signed estimate-error (`actual_ms - estimate_ms`) count/p50/p90, each with unknown-event coverage;
- terminal generation outcome counts;
- attempts, Review rounds, operator interventions, exact token/tool totals, and exact source-unit cost distributions; attempt distributions include only generations/buckets whose attempt identity coverage is complete, with unknown generations/events reported separately;
- operator category counts and duration distributions with unknown duration coverage;
- `by_execution` comparisons preserving the exact execution identity, exact usage/cost unit, generation outcome context, unknown coverage, and low-sample flags.

The report carries `authority=diagnostic_only`, `eligibility=UNKNOWN`, `routing_recommendation=UNKNOWN`, and `productivity_score=UNKNOWN`. No derived metric can automatically change routing, priority, Review depth, lifecycle transitions, or merge admission.

## CLI

```sh
python3 scripts/lifecycle_economics_telemetry.py emit \
  --event-json /path/to/event.json \
  --output /path/to/source-events.jsonl

python3 scripts/lifecycle_economics_telemetry.py capture-pr \
  --pr-number 168 \
  --output /path/to/source-events.jsonl

python3 scripts/lifecycle_economics_telemetry.py collect \
  --input /path/to/source-events.jsonl \
  --records /path/to/generation-records.jsonl \
  --report /path/to/diagnostic-report.json
```

## Representative source events

A provider-run event with exact execution-bound economics:

```json
{"schema_version":"lifecycle-economics-source-event/v1","source":"worker","source_event_id":"run:impl-42","observed_at":"2026-08-18T10:05:00Z","series":"pr_flow","repository":"marcogallotta/ai-tools","task_id":"1217487779268948","pr_number":168,"lineage_id":"impl:1217487779268948","generation_id":"gen-2","head_sha":"0123456789abcdef0123456789abcdef01234567","attempt_id":"impl-42","event_type":"implementation","execution":{"execution_id":"impl-42","host":"chatgpt","provider":"openai","model":"model-x","config":"implementation","snapshot":"snapshot-1"},"usage":{"tool_calls":12,"input_tokens":9000,"output_tokens":2100,"total_tokens":11100,"cost":{"amount":"0.042","currency":"USD","unit":"provider_charge"}}}
```

An exact durable operator decision with unknown execution economics:

```json
{"schema_version":"lifecycle-economics-source-event/v1","source":"asana","source_event_id":"story:design-42","observed_at":"2026-08-18T10:05:00Z","series":"pr_flow","repository":"marcogallotta/ai-tools","task_id":"1217487779268948","pr_number":168,"lineage_id":"impl:1217487779268948","generation_id":"gen-2","event_type":"operator_decision","execution":"UNKNOWN","usage":"UNKNOWN","operator":{"required":true,"category":"design_risk_product_decision","action_id":"decision-42","duration_ms":240000}}
```

A source-backed formal Review event stores the durable review facts but not the Review body:

```json
{"schema_version":"lifecycle-economics-source-event/v1","source":"github-formal-review","source_event_id":"github-review:4960992929","observed_at":"2026-08-18T12:30:45Z","series":"pr_flow","repository":"marcogallotta/ai-tools","task_id":"1217487779268948","pr_number":168,"lineage_id":"UNKNOWN","generation_id":"UNKNOWN","head_sha":"0123456789abcdef0123456789abcdef01234567","event_type":"formal_review","execution":"UNKNOWN","usage":"UNKNOWN","review":{"round_id":"4960992929","review_id":"4960992929","phase":"blocked","exact_head_sha":"0123456789abcdef0123456789abcdef01234567"}}
```
