export const lifecycleFixtures = Object.freeze({
  loading: {
    kind: "loading",
    message: "Loading the fixture board…",
  },
  initialError: {
    kind: "initial-error",
    code: "initial_load_failed",
    message: "The board could not be loaded. The persistent shell is still available.",
  },
  lastSafe: {
    kind: "last-safe",
    code: "service_unavailable",
    message: "Refresh failed. Showing the last successful fixture board.",
  },
});

const generatedAt = "2026-08-12T06:00:00Z";
const diagnostics = (attention_codes) => ({ attention_codes });
const attention = (code, label, message) => ({ code, label, message });
const noOperation = { state: "no_active_operation" };
const activeOperation = (operation, phase) => ({ state: "active_operation", operation, phase });

export const adminFixture = Object.freeze({
  generated_at: generatedAt,
  summary: {
    needs_you: 2,
    human_review: 1,
    recovery: 1,
    workflow_queue: 2,
    research: 1,
    verification: 1,
    system_activity: 1,
    affected_dishes: 5,
  },
  dishes: [
    {
      task_id: "11111111-1111-4111-8111-111111111111",
      title: "Charred cabbage with anchovy butter",
      section_label: "Verification Queue",
      workflow_status: activeOperation("Verification", "Human review"),
      bucket: "needs_you",
      attention: [attention(
        "verification_attention",
        "Waiting for your decision",
        "Accept the softer-than-target center for tonight's service, or require another verification pass after recooking?",
      )],
      last_activity_at: "2026-08-12T05:52:00Z",
      diagnostics: diagnostics(["verification_attention"]),
    },
    {
      task_id: "22222222-2222-4222-8222-222222222222",
      title: "Preserved lemon chicken",
      section_label: "Operations",
      workflow_status: activeOperation("Change", "Recovery rehearsal"),
      bucket: "needs_you",
      attention: [attention(
        "recovery_required",
        "Recovery needs your attention",
        "The last command has an uncertain outcome. Confirm the durable result before ordinary workflow resumes.",
      )],
      last_activity_at: "2026-08-12T05:41:00Z",
      diagnostics: diagnostics(["recovery_required"]),
    },
    {
      task_id: "33333333-3333-4333-8333-333333333333",
      title: "Green tomato broth",
      section_label: "Research Queue",
      workflow_status: noOperation,
      bucket: "workflow_queue",
      attention: [attention("research_required", "Needs research", "This dish is waiting in the Research queue.")],
      last_activity_at: null,
      diagnostics: diagnostics(["research_required"]),
    },
    {
      task_id: "44444444-4444-4444-8444-444444444444",
      title: "Crisp rice and mushroom stock",
      section_label: "Verification Queue",
      workflow_status: noOperation,
      bucket: "workflow_queue",
      attention: [attention("verification_required", "Needs verification", "This dish is waiting in the Verification queue.")],
      last_activity_at: "2026-08-12T04:20:00Z",
      diagnostics: diagnostics(["verification_required"]),
    },
    {
      task_id: "55555555-5555-4555-8555-555555555555",
      title: "Brown butter carrots",
      section_label: "Cooking",
      workflow_status: activeOperation("Initial", "Prepare required"),
      bucket: "system_activity",
      attention: [],
      last_activity_at: "2026-08-12T05:58:00Z",
      diagnostics: diagnostics([]),
    },
  ],
  journal: [
    {
      event_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
      task_id: "22222222-2222-4222-8222-222222222222",
      title: "Preserved lemon chicken",
      occurred_at: "2026-08-12T05:41:00Z",
      summary: "Recovery checkpoint recorded",
      diagnostics: {
        event_type: "section2_recovery_boundary",
        actor: "dish-service",
        request_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1",
        command_execution_id: "cccccccc-cccc-4ccc-8ccc-ccccccccccc1",
        operation_id: "dddddddd-dddd-4ddd-8ddd-ddddddddddd1",
      },
    },
  ],
});

export const adminExtremeFixture = Object.freeze({
  generated_at: generatedAt,
  summary: {
    needs_you: 1,
    human_review: 1,
    recovery: 0,
    workflow_queue: 0,
    research: 0,
    verification: 0,
    system_activity: 1,
    affected_dishes: 2,
  },
  dishes: [
    {
      task_id: "66666666-6666-4666-8666-666666666666",
      title: "A deliberately long operator-facing dish title that should wrap cleanly without allowing the technical diagnostics affordance to dominate the decision context",
      section_label: "Verification Queue with a deliberately extended display label used to review wrapping and hierarchy",
      workflow_status: activeOperation("Verification", "Human review"),
      bucket: "needs_you",
      attention: [attention(
        "verification_attention",
        "Waiting for your decision",
        "The candidate is safe to serve, but the verified batch is materially softer than the agreed texture target after the documented hold. Accept that texture for this service, or require a fresh batch and another independent verification pass before release?",
      )],
      last_activity_at: "2026-08-12T05:59:00Z",
      diagnostics: diagnostics(["verification_attention"]),
    },
    {
      task_id: "77777777-7777-4777-8777-777777777777",
      title: "Long-running stock clarification",
      section_label: "Operations",
      workflow_status: activeOperation("Planning", "Evidence hold"),
      bucket: "system_activity",
      attention: [attention(
        "hold_active",
        "Workflow is deliberately paused",
        "A recorded hold is active; no immediate operator action is inferred.",
      )],
      last_activity_at: "2026-08-11T22:15:00Z",
      diagnostics: diagnostics(["hold_active"]),
    },
  ],
  journal: [],
});

export const adminEmptyFixture = Object.freeze({
  generated_at: generatedAt,
  summary: {
    needs_you: 0,
    human_review: 0,
    recovery: 0,
    workflow_queue: 0,
    research: 0,
    verification: 0,
    system_activity: 0,
    affected_dishes: 0,
  },
  dishes: [],
  journal: [],
});
