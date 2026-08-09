import assert from "node:assert/strict";
import test from "node:test";
import { parsePostgresTaskRoute, postgresTaskRoute, titleSlug } from "../../src/js/features/routing/routes.js";

const TASK_ID = `r1t-${"a".repeat(27)}`;

test("PostgreSQL deep link uses opaque identity and decorative current-title slug", () => {
  const path = postgresTaskRoute(TASK_ID, "[ready] Crème brûlée / Test");
  assert.equal(path, `/tasks/${TASK_ID}/ready-creme-brulee-test`);
  assert.deepEqual(parsePostgresTaskRoute(path), { taskId: TASK_ID, slug: "ready-creme-brulee-test" });
  assert.equal(parsePostgresTaskRoute(`/tasks/${TASK_ID}/old-title`).taskId, TASK_ID);
});

test("invalid PostgreSQL route identity fails closed", () => {
  assert.equal(parsePostgresTaskRoute("/tasks/00000000-0000-0000-0000-000000000001/title"), null);
  assert.throws(() => postgresTaskRoute("raw-id", "Title"));
  assert.equal(titleSlug("***"), "task");
});
