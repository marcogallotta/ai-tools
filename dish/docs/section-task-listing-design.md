# Section task listing design

## Status and purpose

This is a near-term candidate for soon after rollout, not implementation authorization.

Agents can currently discover Cooking section GIDs with `dish sections`, but they cannot ask Dish
which tasks are waiting in a section. They must already know a task GID before using `dish read` or
starting governed work. The first version should add a narrow, read-only task-discovery command for
any section in the Cooking project:

```text
dish list SECTION_GID --agent AGENT
dish list SECTION_GID --agent AGENT --status incomplete|complete|both
```

`incomplete` is the default. This makes Research Queue and Verification Queue useful as pending-work
lists without hard-coding queue-specific commands, while retaining the same bounded interface for
destination, Sourcing, and Reference sections.

The command discovers task identities only. It does not infer workflow state, compute legal actions,
or replace the required exact `dish read TASK_GID` before governed work.

## Surface and authority

The initial command belongs on the private CLI and private service surface. It should not initially
be added to the Funnel-exposed GPT Action: unrestricted section enumeration can reveal every title
in a section and can create an unbounded Action response. If connected GPT use later needs this
capability, design a separately bounded response or use the bounded lookup described in
[`gpt-natural-interaction-design.md`](gpt-natural-interaction-design.md).

The section argument is an immutable GID, not a display name. The command must:

1. enumerate all Cooking project sections through the existing paginated section path;
2. build the normal `SectionRegistry`;
3. reject a GID that is not in that registry; and
4. return both the confirmed section GID and its current display name.

This keeps Cooking-project placement authority GID-based and avoids turning Dish into a generic
Asana section browser. `dish sections --agent AGENT` remains the discovery path for valid GIDs.

## Task enumeration and pagination

Add one backend operation for Asana's tasks-for-section endpoint. It should request only the fields
needed by this discovery result:

```text
gid
name
completed
modified_at
permalink_url
```

Use pages of 100 and consume every `next_page.offset` internally. The caller receives one complete
result rather than an Asana cursor. Pagination must follow the same fail-closed rules as existing
section enumeration:

- reject malformed page data or pagination metadata;
- reject an empty or repeated continuation offset;
- preserve the endpoint's section order when concatenating pages;
- fail the whole command if any page fails; and
- never return a partial task list as if enumeration completed.

For `--status incomplete`, pass `completed_since=now` so Asana filters incomplete tasks before
pagination. For `complete` and `both`, omit that parameter, fetch all pages, and apply the exact
completion filter locally. Filtering must never stop pagination early.

Asana pagination is not a transactional snapshot. A task moved or completed during enumeration can
affect the observed pages. This is acceptable for discovery: the selected task is authoritatively
reread by `dish read` or `dish start`. The command must not claim that its list is a frozen workflow
assignment.

## Result contract

A successful canonical envelope should have no workflow `allowed_actions` and contain:

```text
data.project_gid
data.section.gid
data.section.name
data.status
data.tasks[]
data.count
```

Each task entry contains:

```text
gid
title
completed
modified_at
permalink_url
```

`count` is the number of returned tasks after status filtering. An empty section is a successful
result with `tasks: []` and `count: 0`.

The command should use the existing backend failure mapping. A missing or inaccessible section is a
rejected read, not an uncertain mutation, and is safe to call again according to the ordinary
read-only error contract. Like `sections` and `read`, it needs no client request UUID or request-replay record.

## Owning changes

Implementation should extend the existing owners rather than introduce another lookup path:

- `dish_tool.backend.AsanaBackend` and `CommandBackend` own fully paginated section-task retrieval;
- `dish_tool.commands` validates the actor and Cooking section, filters status, and builds the
  canonical result;
- `dish_service.cli` owns `list` parsing and help;
- the private service client and route carry the read-only command;
- `dish_service.command_spec.ACTION_COMMANDS` deliberately excludes it in the first version.

No workflow policy, task document, database schema, lease, request replay, or Honest protocol change
is required.

## Verification

Tests should cover:

- parser help, required agent, GID validation, and all status values;
- arbitrary valid Cooking sections, including both queues and excluded sections;
- default incomplete filtering and complete/both filtering;
- multiple pages, preserved ordering, and an empty final page;
- malformed data, malformed/empty/repeated offsets, and failure on a later page;
- rejection of a section outside the Cooking registry;
- canonical empty and populated result envelopes;
- private HTTP/service transport access;
- absence from the Action route allowlist and generated Action OpenAPI document; and
- real generated Asana SDK method invocation through the low-level fake transport.

The complete Dish suite remains the handoff gate.
