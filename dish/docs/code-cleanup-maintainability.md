# Dish code-cleanup maintainability

**Status:** planned. Begins in earnest once code-cleanup consolidation (`docs/code-cleanup-consolidation.md`) has established coherent ownership in an area and the system is stable enough that long-tail cleanup no longer risks obscuring architectural decisions. Individual packages may still start earlier where entry conditions (§2) are already met.

**Relationship to cutover:** this program is part of the wider code-cleanup effort. It should
substantially complete before PostgreSQL cutover readiness is finalized, but it must not become an
indefinite blocker to useful product/dark-launch work.

This document replaces ad hoc "Stage C" references — that name collides with other uses elsewhere in `dish/docs`. Internal workstream codes below (CM1–CM6) may be used for cross-references within this document and `docs/code-cleanup-consolidation.md`; anywhere else, use the descriptive workstream name.

## 1. Purpose

This program finishes the long tail after code-cleanup consolidation removes the major structural overlaps.

Its goals are:

- make critical code locally understandable;
- retire temporary compatibility and migration-era surfaces at their real lifecycle boundary;
- simplify operational tooling without weakening safety;
- leave one maintained documentation home for each subject;
- make development and agent work predictable;
- establish lightweight controls that prevent the repository from regrowing duplicate architecture.

This is scheduled maintenance, not a permanent cleanup state.

## 2. Entry conditions

Start a package when the relevant area has already reached a coherent code-cleanup-consolidation ownership model.

Do not use this program's decomposition to pre-empt unresolved consolidation-stage architecture.

Before decomposing or deleting an area, confirm:

- its authoritative owner is settled;
- active recovery/dark-launch/frontend/cutover work is not still moving the same seam;
- removal does not erase required replay, audit, transition, recovery, or cutover evidence;
- compatibility branches have an explicit lifecycle or can safely receive one.

## 3. CM1 — Complete module and class decomposition

### Goal

Reduce god modules and mixed-responsibility classes after their semantic ownership is already settled.

### Work

- split routing, validation, authority, transaction, effect, and rendering responsibilities where they are currently entangled;
- reduce high-branch functions in critical paths;
- remove service/port objects that merely proxy unrelated responsibilities;
- tighten type boundaries and names;
- keep related explicit code together when splitting would make reasoning harder;
- favor cohesive vertical modules over generic framework layers.

### Rules

- do not introduce abstraction solely to reduce line count;
- do not hide state transitions behind generic dispatch machinery;
- do not move domain policy into transport or operational scripts;
- every split should reduce the amount of unrelated code required to understand a change.

## 4. CM2 — Remove obsolete compatibility and migration-era code

### Goal

Delete transitional code only when its actual producer/consumer lifecycle has ended.

### Work

- remove temporary adapters whose real producers have moved;
- remove old schema-compatibility paths no longer required by supported state;
- retire migration tests that only protect unreachable historical upgrade paths once the migration/baseline policy permits it;
- remove obsolete shadow/import/rehearsal helpers when their operational lifecycle ends;
- remove duplicated legacy mutation paths at the approved authority-retirement point;
- eliminate compatibility branches with no remaining caller;
- assign an owner and expiry/review condition to every compatibility branch that remains.

### Transition-record rule

Transition records are not deleted merely because code around them becomes obsolete. They remain
until Marco explicitly decides their disposition (`docs/postgresql-cutover.md`, "Evidence retention").

A scheduled review may surface that decision (see `/home/marco/ai-tools/CLAUDE.md`, "Scheduled reviews"); a date must never cause automatic deletion.

## 5. CM3 — Simplify operational tooling

### Goal

Keep operational commands safe and repeatable while removing migration-era orchestration and evidence chains that no longer protect live invariants.

### Work

- keep scripts as thin entry points;
- move reusable behavior into properly owned application/ops modules;
- delete superseded one-shot scripts after their event/lifecycle closes;
- collapse report/evidence chains that certify other certification records without enforcing a live invariant;
- preserve explicit preflight, fencing, backup, restore, smoke, and recovery checks where they remain operationally valuable;
- retain first-failure artifacts long enough for diagnosis rather than producing permanent bureaucracy.

### Cutover timing

Do not simplify tooling merely because it looks heavy while a real cutover/rehearsal still depends on it.

Conversely, do not freeze the entire current control plane until after cutover. CM3 and explicit
pre-cutover cleanup may retire machinery when its lifecycle and consumers are proven ended.

## 6. CM4 — Consolidate documentation

### Goal

One maintained source per subject, with status/history separated from design/operations.

### Work

- assign one authoritative maintained document to each architectural/operational subject;
- delete or explicitly supersede stale plans and duplicate status narratives;
- keep exploratory research, agent reports, and handoff packets outside maintained product documentation;
- update architecture documents when ownership changes;
- prevent archive SHA, delivery metadata, temporary patch notes, and synthetic Git identities from becoming product architecture;
- keep current operational runbooks separate from historical migration rationale where practical;
- use links instead of copying the same policy into many documents.

### PostgreSQL documentation

Current PostgreSQL invariants live in the architecture knowledge base, cutover policy lives in
`docs/postgresql-cutover.md`, and exact operations live in the two PostgreSQL runbooks. Do not add
another planning document; record implementation work in the task tracker and preserve history in Git.

## 7. CM5 — Developer and agent ergonomics

### Goal

Make ordinary work fast to start, easy to validate, and difficult to misinterpret.

### Work

- one obvious local/bootstrap path;
- one documented archive-review/bootstrap path;
- explicit offline dependency strategy where required;
- planner-selected commands that are directly runnable;
- fast focused semantic lanes for bounded patches;
- clear escalation to smoke/native/process evidence;
- failure output that distinguishes assertion failure, dependency/setup failure, wrapper timeout, and hang/resource leak;
- predictable changed-path ownership selection;
- fewer mandatory metadata edits for ordinary refactors;
- concise examples of approved command, transition, replay, lease, effect, and test patterns;
- agent handoff templates that require provenance, scope, evidence, unresolved friction, and explicit exclusions.

### Primary productivity metric

Measure **time from receiving current source to first valid test result**.

Do not optimize this metric by weakening evidence. Improve environment discovery, bootstrap, isolation, planner ergonomics, and test architecture instead.

## 8. CM6 — Ongoing quality controls

Track trends rather than enforcing arbitrary size quotas.

Useful controls include:

- number of authoritative implementations per rule;
- dependency violations/cycles;
- files changed per feature/defect;
- critical function/class cognitive complexity;
- test first-attempt reliability;
- time-to-first-valid-test;
- full-suite/smoke runtime and reproducibility;
- test-to-production duplication;
- dead/test-only production surfaces;
- dark-launch regressions;
- unresolved compatibility expiry/review dates;
- number of maintained documents per subject.

Line count, table count, and file count remain diagnostic only.

## 9. Documentation/review lifecycle

For deferred lifecycle decisions that Marco does not want to forget:

- schedule a **human review**, not automatic deletion;
- make the reminder scope-specific so unrelated repo work is not globally blocked;
- when a real event date exists, replace placeholder dates with an event-relative review date;
- a future agent may remind Marco that a review is due, but must never infer that the due date itself authorizes destructive action.

For PostgreSQL transition-record cleanup specifically, the current planning direction is: retain through cutover and stabilization; use a pre-cutover checkpoint if needed to ensure the decision has not gone stale; after actual cutover, set the agreed post-cutover review date; Marco explicitly chooses retain/archive/remove at that review.

## 10. Execution model

Run this program as explicit packages rather than a broad "cleanup everything" mandate.

Typical package types: one god-module decomposition; one compatibility-family retirement; one operational-tooling simplification; one documentation-domain consolidation; one developer/test ergonomics improvement; one quality-control addition that addresses a demonstrated recurring defect.

Parallel packages are acceptable when they do not share core files or unresolved semantic ownership.

## 11. Exit criteria

This program is complete when:

- no unexplained god object or high-branch critical function remains as normal architecture;
- no duplicate production source of truth is accepted as normal;
- compatibility/migration-era branches have been removed or have explicit owners and review conditions;
- operational tooling is thin, coherent, and proportionate to live invariants;
- each maintained subject has one clear documentation authority;
- setup/test/planner paths are predictable for humans and agents;
- test and metadata maintenance is proportional to behavioral risk;
- future agents have clear extension patterns;
- lightweight quality controls catch reintroduction of duplicated complexity.

## 12. Relationship to PostgreSQL cutover

This program does not execute cutover.

Its output should make pre-cutover revalidation smaller and safer by leaving: a coherent
authority/workflow/replay/effect architecture; a justified persistence model; a known
deployment/runtime boundary; understandable operational tools; current documentation; repeatable
evidence paths.

Cutover preparation must still revalidate the actual surviving architecture and operational evidence
rather than assuming this program's plans describe deployed reality.
