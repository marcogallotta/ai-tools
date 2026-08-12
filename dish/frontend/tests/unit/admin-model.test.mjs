import test from "node:test";
import assert from "node:assert/strict";
import { adminEmptyFixture, adminExtremeFixture, adminFixture } from "../../fixtures/stage1-states.js";
import { AdminContractMismatch, mapAdminResponse, workflowText } from "../../src/js/features/admin/admin-model.js";

const taskId = "12345678-1234-5678-1234-567812345678";
const eventId = "22345678-1234-5678-1234-567812345678";
const requestId = "32345678-1234-5678-1234-567812345678";

function payload() {
  return {
    generated_at: "2026-08-10T18:00:00+00:00",
    summary: { needs_you: 1, human_review: 1, recovery: 0, workflow_queue: 0, research: 0, verification: 0, system_activity: 0, affected_dishes: 1 },
    dishes: [{
      task_id: taskId,
      title: "Miso soup",
      section_label: "Verification Queue",
      workflow_status: { state: "no_active_operation" },
      bucket: "needs_you",
      attention: [{ code: "verification_attention", label: "Waiting for your decision", message: "Human Review is open for this dish." }],
      last_activity_at: "2026-08-10T17:59:00+00:00",
      diagnostics: { attention_codes: ["verification_attention"] },
    }],
    journal: [{
      event_id: eventId,
      task_id: taskId,
      title: "Miso soup",
      occurred_at: "2026-08-10T17:59:00+00:00",
      summary: "Workflow action was rejected",
      diagnostics: { event_type: "workflow_action_rejected", actor: "agent", request_id: requestId, command_execution_id: null, operation_id: null },
    }],
  };
}

test("admin contract maps dish-first attention and operator journal", () => {
  const mapped = mapAdminResponse(payload());
  assert.equal(mapped.summary.needsYou, 1);
  assert.equal(mapped.dishes[0].title, "Miso soup");
  assert.equal(mapped.dishes[0].attention[0].code, "verification_attention");
  assert.equal(mapped.journal[0].summary, "Workflow action was rejected");
  assert.equal(workflowText(mapped.dishes[0].status), "No active operation");
});

test("admin contract accepts a durable Human Review question beyond the old generic-message bound", () => {
  const value = payload();
  const question = `Choose between the documented operator alternatives after reviewing this full context: ${"material evidence and consequence; ".repeat(20)}`;
  assert.ok(question.length > 300);
  value.dishes[0].attention[0].message = question;
  const mapped = mapAdminResponse(value);
  assert.equal(mapped.dishes[0].attention[0].message, question);
});

test("admin contract rejects summary counts that disagree with dishes", () => {
  const value = payload();
  value.summary.needs_you = 2;
  assert.throws(() => mapAdminResponse(value), AdminContractMismatch);
});


test("admin contract maps ordinary workflow queue work without needs-you inflation", () => {
  const value = payload();
  value.summary = { needs_you: 0, human_review: 0, recovery: 0, workflow_queue: 1, research: 0, verification: 1, system_activity: 0, affected_dishes: 1 };
  value.dishes[0].bucket = "workflow_queue";
  value.dishes[0].attention = [{ code: "verification_required", label: "Needs verification", message: "This dish is waiting in the Verification queue." }];
  value.dishes[0].diagnostics.attention_codes = ["verification_required"];
  const mapped = mapAdminResponse(value);
  assert.equal(mapped.summary.needsYou, 0);
  assert.equal(mapped.summary.verification, 1);
  assert.equal(mapped.dishes[0].bucket, "workflow_queue");
});

test("admin contract maps an ordinary active operation without synthetic attention", () => {
  const value = payload();
  value.summary = { needs_you: 0, human_review: 0, recovery: 0, workflow_queue: 0, research: 0, verification: 0, system_activity: 1, affected_dishes: 1 };
  value.dishes[0].workflow_status = { state: "active_operation", operation: "Initial", phase: "Prepare required" };
  value.dishes[0].bucket = "system_activity";
  value.dishes[0].attention = [];
  value.dishes[0].diagnostics.attention_codes = [];
  const mapped = mapAdminResponse(value);
  assert.equal(mapped.summary.systemActivity, 1);
  assert.equal(mapped.dishes[0].bucket, "system_activity");
  assert.equal(mapped.dishes[0].attention.length, 0);
  assert.equal(workflowText(mapped.dishes[0].status), "Initial · Prepare required");
});

test("admin contract still rejects empty attention outside active system work", () => {
  const value = payload();
  value.dishes[0].attention = [];
  value.dishes[0].diagnostics.attention_codes = [];
  assert.throws(() => mapAdminResponse(value), AdminContractMismatch);
});

test("stable admin fixture covers operator hierarchy with exact Human Review context", () => {
  const admin = mapAdminResponse(structuredClone(adminFixture));
  assert.equal(admin.summary.needsYou, 2);
  assert.equal(admin.summary.humanReview, 1);
  assert.equal(admin.summary.recovery, 1);
  assert.equal(admin.summary.workflowQueue, 2);
  assert.equal(admin.summary.systemActivity, 1);
  assert.deepEqual(new Set(admin.dishes.map((dish) => dish.bucket)), new Set(["needs_you", "workflow_queue", "system_activity"]));
  const review = admin.dishes.find((dish) => dish.attention.some((item) => item.code === "verification_attention"));
  assert.match(review.attention[0].message, /Accept the softer-than-target center/);
});

test("admin extreme and empty fixtures stay inside the production response contract", () => {
  const extreme = mapAdminResponse(structuredClone(adminExtremeFixture));
  assert.equal(extreme.summary.needsYou, 1);
  assert.ok(extreme.dishes[0].title.length > 100);
  assert.ok(extreme.dishes[0].attention[0].message.length > 180);

  const empty = mapAdminResponse(structuredClone(adminEmptyFixture));
  assert.equal(empty.summary.affectedDishes, 0);
  assert.deepEqual(empty.dishes, []);
  assert.deepEqual(empty.journal, []);
});
