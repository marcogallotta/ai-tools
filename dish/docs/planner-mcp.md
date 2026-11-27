# Read the saved planner through Dish MCP

Goal: another ChatGPT agent can inspect Marco's current saved planner without exports or access to the local computer. No scheduling or recipe mutations are added.

## Design (independent review: PASS)

- Add `dish_planner_read` to the existing authenticated Dish MCP shell. It takes no arguments. Existing GitHub OAuth still admits only the configured immutable owner.
- Read the fixed `main` row from `cook_planner.plans` using a dedicated SELECT-only PostgreSQL login. No client-selected connection, plan, query, or filesystem path. This is single-owner deployment, not a multi-tenant API.
- Return the complete saved workspace unchanged, authoritative database revision, database update time, and UTC read time. The document's internal revision is opaque metadata. Do not call the workspace approved, and do not imply unsaved browser edits are included.
- Explain cook dates versus serving dates, Holding, home/away boards, preparation and baking. Track 0/1 are movable columns, not assigned lunch/dinner. No derived second calendar.
- Keep planner data separate from recipe workflow authority. No Dish run, Research or readiness changes. No roadmap/seed/cache fallback.
- Use bounded connect and query timeouts and async database I/O. Check object/version/core arrays only; do not clone the planner's validator. Return explicit sanitized configuration, unavailable, uninitialized and unsupported-data errors.
- Register through the production application factory. Deployment adds only the readonly credential to MCP, stages reviewed source, updates the existing MCP unit and probes the public authenticated endpoint. Do not alter the planner, its data, or Dish's backend release pointer.

## Test plan

1. Real HTTP MCP initialize, tools/list and tools/call through the production `create_app` registration with disposable PostgreSQL and test-only authentication. Assert no-argument schema and readonly annotations.
2. Compare returned state exactly against a real database fixture with home/away, Holding, bakes, preparation and two movable tracks. Assert outer revision/update/read times and interpretation guidance.
3. Update the disposable row and read again through MCP: new revision/document must appear. Compare plan/history before and after reads: no mutation.
4. Missing/invalid HTTP token must fail. Run existing wrong-GitHub-owner authentication tests. No database/query/plan arguments may change the selected source.
5. Missing configuration, missing plan, unsupported/malformed document and unavailable database return clear failures without secrets or fallback. Verify SELECT-only role denies INSERT/UPDATE/DELETE.
6. Run focused existing MCP tests and the repository's applicable selected checks. Have an independent agent challenge implementation and tests, then fix and rerun any failures.
7. After deployment, initialize/list/call the public authenticated MCP endpoint and compare its exact revision/document with the live planner API read. Verify unrelated MCP tools remain discoverable and no production writes occur. Record release identity and evidence.

Acceptance: independent design PASS, implementation review PASS, automated HTTP MCP tests passing, deployed readonly conformance passing. Then ask Marco to reload the Dish MCP app schema and give a copy/paste prompt for the ChatGPT agent. Do not claim ChatGPT sees the new tool before reload.
