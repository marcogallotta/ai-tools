# Frontend activation record

Status: **not approved for production activation**.

This temporary record owns only the evidence and human decision needed to
activate the already implemented frontend. It does not specify product behavior,
architecture, API shapes, implementation layout, or test procedure. Complete it
for one exact candidate; after activation, retain the decision in Git history
and remove this file.

Production administration and activation require Marco's explicit authorization.

## Candidate identity

- Commit:
- Frontend build identity:
- PostgreSQL migration head and schema fingerprint:
- Frontend contract version:
- Deployment target and configuration identity:
- Reviewer and review date:

Any candidate change invalidates evidence whose recorded identity no longer
matches.

## Unresolved decisions

Resolve these from governing authority and runtime evidence rather than names or
frontend presentation assumptions:

- exact invalid/contested lease presentation predicates;
- exact failed/disputed Verification predicates and cycle scope;
- task-scoped recovery mapping for unresolved command uncertainty;
- projection reducer, delay threshold, readiness input, and precedence;
- canonical destination source and disclosure/advisory equivalence;
- deterministic title normalization/collation behavior;
- route/cursor token secret lifecycle, rotation, expiry, and compatibility
  behavior.

Accepted outcomes belong in the relevant architecture owner or checked-in
contract/code. This record retains only the fact that the candidate was reviewed
against them.

## Required evidence

- [ ] Dedicated private HTTPS hostname and HSTS termination are verified; the
      public Action route and listener remain isolated.
- [ ] Production Argon2 policy, frontend secrets, owner-only restore fence, and
      physically separate writable security and SELECT-only observation
      databases are accepted.
- [ ] Native PostgreSQL covers authentication concurrency, session replacement,
      restart, revocation, read coherence, transaction isolation, and required
      query plans.
- [ ] Destructive restore/PITR cannot revive session authority, and failure
      modes close access safely.
- [ ] Board/detail predicates, projection presentation, destination, disclosure,
      rendering, ordering, pagination, and cursor behavior match the accepted
      contracts.
- [ ] Query count, execution time, statement timeout, response size, and
      configured capacity remain bounded on representative production-shaped
      data.
- [ ] The complete frontend unit/static/build and Playwright acceptance suite
      passes against this exact candidate through the real private HTTPS
      surface.
- [ ] Login, expiry, replacement, logout, restart, restore, multi-tab, page
      restoration, graceful drain, security headers, no-store behavior, and
      sensitive-data exclusion are verified in the deployed shape.
- [ ] An independent reviewer has reconciled the evidence to the exact candidate
      and recorded a decision below.

Evidence locations:

- Native PostgreSQL:
- Restore/PITR:
- Browser acceptance:
- Deployment/security review:
- Remaining decision evidence:

## Decision

Choose exactly one:

- [ ] **APPROVE:** this exact candidate may be activated separately by Marco.
- [ ] **REJECT:** activation remains closed; findings below require resolution.

Findings:

| ID  | Severity | Finding | Required action | Resolution |
| --- | -------- | ------- | --------------- | ---------- |
|     |          |         |                 |            |

Reviewer statement:

>
