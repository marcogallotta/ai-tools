# Repository-native standing gate exceptions

This procedure governs Marco-approved standing exceptions to Dish development gates. Current Git is
the only standing-policy freshness domain: there is no ChatGPT Project-settings overlay, reserved
block, overlay generation, or fresh-chat activation step. The registry is policy data, not a second
approval service, scheduler, queue, database, identity system, merge gate, or lifecycle controller.

## Procedure

1. **Fix a wrong gate at source.** If the desired behavior should be normal, remove or correct the
   canonical gate through the ordinary reviewed Development Workflow lifecycle. Do not preserve a
   nuisance gate and hide it behind a durable exception.
2. **Require Marco's exact decision for a real exception.** A gate that remains useful by default
   may receive a standing exception only after Marco approves the exact gate, consequence, and
   scope. Development Workflow records that decision's exact Asana task/story/timestamp and owns
   the routine repository change. Wildcards and future-gate inheritance are invalid.
3. **Bind exact current semantics.** Each registry exception in
   `dish/docs/chatgpt-projects/fast-track-gates.json` binds one gate ID, current version, semantic
   digest, `ACTIVE`/`INACTIVE` state, exact Marco provenance, optional expiry/condition, and an
   append-only activation history. A material gate change requires a new gate version and new Marco
   approval; rewriting an old version does not expand its exception.
4. **Create debt before activation.** For every new exception or reactivation, Development Workflow
   immediately creates a follow-up in canonical project `1217419962189616`, sets structured
   Priority to `P-CRITICAL`, and sets `due_on` to the activation's same calendar date. Its name/notes
   identify the exact gate/version/digest, activation ID, Marco decision provenance, and objective
   to remove or narrow the exception or replace it with a correct source/gate fix. Freshly apply the
   Development Workflow Asana project contract for the writes.
5. **Read back before making it active.** Authoritatively read the created task's project, structured
   Priority, due date, identity, and identifying objective. Persist that exact task plus readback
   evidence in a new registry activation record, then pass ordinary Implementation, independent
   Review, and Integration. Missing, late, wrong-project, wrong-priority, wrong-date, or incomplete
   readback evidence makes the activation invalid; never use the exception first and repair the
   debt record later.
6. **Do not duplicate on use.** Repeated uses of the current active activation create no new
   follow-up. A later reactivation appends a new activation ID and new same-day follow-up/readback;
   an active entry must bind its newest activation.
7. **Use only current reviewed Git.** Before use, require the exact active, unexpired exception and
   current gate version/digest. When a condition exists, record current evidence that it is true.
   Absent, inactive, expired, stale, mismatched, or condition-unproved entries fall back to ordinary
   gate behavior.
8. **Record every use truthfully.** On the existing durable lifecycle surface record
   `GATE WAIVED BY MARCO OVERRIDE`, registry version, exact gate/version/digest, activation ID,
   Marco decision and follow-up task, exact task/candidate/action, condition evidence when relevant,
   and the raw failed evidence. A waiver never turns failed/red evidence into PASS.
9. **Honor current-chat revocation immediately.** Marco's clear current-chat revocation or
   correction stops use in that chat. Durable inactivation/removal then follows the normal reviewed
   repository lifecycle; other freshly grounded agents consume current Git until that change lands.

## Retained boundaries

No standing exception implicitly waives exact task/branch/PR/head identity, independent semantic
Review, Integration separation, production/destructive-operation safeguards, genuine
platform/system impossibilities, or another separately governed gate. Marco must explicitly name
any additional gate, and the same exact-scope activation rules apply.

## Current gate inventory

`repository-context-bundle-witness@1` can waive only the exact-current repository-bundle
retrieval/materialization/verification prerequisite when bundle transport is unavailable. GitHub
and Asana authority, exact candidate identity, and rejection of an invalid, stale, mismatched,
corrupt, or wrong-SHA bundle remain required. The registry currently contains no active standing
exception, so ordinary gate behavior applies unless a reviewed activation is later added.
