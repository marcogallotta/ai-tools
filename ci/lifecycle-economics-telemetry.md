# Lifecycle economics and timing telemetry V1A

## Authority boundary

This telemetry is observational only. It is not a scheduler, queue, policy engine, merge gate, lifecycle state machine, or completion authority. A telemetry write, parse, aggregation, or report failure is telemetry degradation only and MUST NOT change Implementation eligibility, Review, Integration, merge, or completion truth.

`scripts/lifecycle_economics_telemetry.py emit` deliberately exits zero even when the append degrades. Its JSON result reports `telemetry_written=false` and the error for separate operational visibility. Lifecycle code must never branch on that result.

## Provider-neutral source-event envelope

Each JSONL source event uses `schema_version: lifecycle-economics-source-event/v1` and carries a source-owned `source_event_id`. The collector de-duplicates only the exact `(source, source_event_id)` pair.

Required fields are:

- `source`: provider-neutral source name such as `github`, `asana`, `worker`, `review`, or `integration`;
- `source_event_id`: durable identifier owned by that source;
- `observed_at`: authoritative source timestamp;
- `series`: `pr_flow` or `repository_health`;
- `repository`: `owner/name`.

Lifecycle identity fields (`task_id`, `pr_number`, `lineage_id`, `generation_id`, `replaces_generation_id`, `head_sha`, `attempt_id`) are exact facts when available. Missing or unlanded identity is emitted as `UNKNOWN`; the collector never synthesizes it. Events with unknown lineage/generation identity are deliberately kept as separate observations rather than collapsed into a fabricated generation.

Optional exact evidence includes:

- `execution`: `host`, `provider`, `model`, `config`, `snapshot`;
- `timing`: source-owned `stage` plus exact `duration_ms`;
- `review`: exact `round_id`, `review_id`, exact reviewed head, and one of `dispatched`, `blocked`, `fix_started`, `rereview_requested`, `passed`;
- `operator`: `required`, exact category, optional exact `action_id`, exact duration, `override`, and exact `gate_id`;
- `usage`: exact tool calls/tokens and exact provider charge with source currency/unit;
- `outcome`: source-owned `kind`, with `authoritative=true` and `terminal=true` required before it can become the generation terminal outcome;
- `size`: exact source metadata such as files/additions/deletions/lines changed.

No prompt, chat, source-code content, or other payload text belongs in this envelope.

## Operator-touch categories

Operator-required events use exactly one category:

- `design_risk_product_decision` — productive Marco design/risk/product judgment;
- `manual_relay_or_queue_routing` — routine manual relay/routing;
- `status_reconciliation_or_repair` — stale GitHub/Asana reconciliation/status repair;
- `override_waiver_permission_prompt` — override, waiver, or permission friction;
- `integration_merge_controller_babysitting` — manual Integration/merge/controller babysitting;
- `workflow_incident_firefighting` — incident work caused by Development Workflow process failure.

`operator.required=false` is never counted as human intervention merely because a source actor happened to be human. Exact duration is preferred when durable; otherwise the event contributes only an exact count and its duration remains unknown. Repeated same-gate override recurrence is reported only when an exact `gate_id` is present.

## Economics semantics

Retries stay distinct by exact `attempt_id`; an unknown attempt is not folded into a known one. Review rounds and BLOCK/fix/re-review progression are counted only from explicit source-owned review identifiers/phases.

Token/tool totals include only exact attributed values and carry unknown-event counts alongside them. Money is grouped by exact source `(currency, unit)` with no exchange-rate conversion or model-price heuristic. Size metadata is preserved as exact observed values rather than multiplied across repeated source observations.

Scheduled/full-regression health belongs to `repository_health`. It is reported separately and is excluded from PR-flow timing distributions.

## Derived reports

`collect` emits one observational generation record for each exact `(repository, series, lineage_id, generation_id)` and separate UNKNOWN-identity observations where grouping cannot be proven. It also emits a diagnostic-only report with counts and nearest-rank p50/p90 timing plus explicit low-sample visibility.

The report carries `authority=diagnostic_only`, `eligibility=UNKNOWN`, and `routing_recommendation=UNKNOWN`. No productivity score, automatic routing, lifecycle transition, or gate is derived from telemetry.

## CLI

```sh
python3 scripts/lifecycle_economics_telemetry.py emit \
  --event-json /path/to/event.json \
  --output /path/to/source-events.jsonl

python3 scripts/lifecycle_economics_telemetry.py collect \
  --input /path/to/source-events.jsonl \
  --records /path/to/generation-records.jsonl \
  --report /path/to/diagnostic-report.json
```

## Representative source events

A productive design decision with exact operator time:

```json
{"schema_version":"lifecycle-economics-source-event/v1","source":"asana","source_event_id":"story:design-42","observed_at":"2026-08-18T10:05:00Z","series":"pr_flow","repository":"marcogallotta/ai-tools","task_id":"1217487779268948","pr_number":168,"lineage_id":"impl:1217487779268948","generation_id":"gen-2","operator":{"required":true,"category":"design_risk_product_decision","action_id":"decision-42","duration_ms":240000}}
```

An exact Review BLOCK event:

```json
{"schema_version":"lifecycle-economics-source-event/v1","source":"github","source_event_id":"review:991","observed_at":"2026-08-18T10:35:00Z","series":"pr_flow","repository":"marcogallotta/ai-tools","task_id":"1217487779268948","pr_number":168,"lineage_id":"impl:1217487779268948","generation_id":"gen-2","head_sha":"0123456789abcdef0123456789abcdef01234567","review":{"round_id":"round-1","review_id":"991","phase":"blocked","exact_head_sha":"0123456789abcdef0123456789abcdef01234567"},"timing":{"stage":"review_round","duration_ms":310000}}
```

An exact Integration attempt with a source-unit charge and authoritative terminal outcome:

```json
{"schema_version":"lifecycle-economics-source-event/v1","source":"integration","source_event_id":"attempt:integration-3:terminal","observed_at":"2026-08-18T11:12:00Z","series":"pr_flow","repository":"marcogallotta/ai-tools","task_id":"1217487779268948","pr_number":168,"lineage_id":"impl:1217487779268948","generation_id":"gen-2","attempt_id":"integration-3","timing":{"stage":"integration_attempt","duration_ms":92000},"usage":{"total_tokens":"UNKNOWN","cost":{"amount":"0.042","currency":"USD","unit":"provider_charge"}},"outcome":{"kind":"merged","authoritative":true,"terminal":true}}
```
