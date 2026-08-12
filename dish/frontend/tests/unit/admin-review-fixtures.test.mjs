import assert from "node:assert/strict";
import test from "node:test";
import { adminEmptyFixture, adminExtremeFixture, adminFixture } from "../../fixtures/stage1-admin.js";
import { mapAdminResponse } from "../../src/js/features/admin/admin-model.js";

test("admin review fixture covers operator hierarchy with exact Human Review context", () => {
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

test("admin extreme and empty review fixtures stay inside the production response contract", () => {
  const extreme = mapAdminResponse(structuredClone(adminExtremeFixture));
  assert.equal(extreme.summary.needsYou, 1);
  assert.ok(extreme.dishes[0].title.length > 100);
  assert.ok(extreme.dishes[0].attention[0].message.length > 180);

  const empty = mapAdminResponse(structuredClone(adminEmptyFixture));
  assert.equal(empty.summary.affectedDishes, 0);
  assert.deepEqual(empty.dishes, []);
  assert.deepEqual(empty.journal, []);
});
