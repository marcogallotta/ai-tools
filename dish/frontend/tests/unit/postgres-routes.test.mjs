import assert from "node:assert/strict";
import test from "node:test";
import { parsePostgresTaskRoute, postgresTaskRoute, titleSlug } from "../../src/js/features/routing/routes.js";

const TASK_ID = "12345678-1234-5678-1234-567812345678";

test("PostgreSQL deep link uses stored Dish UUID and decorative current-title slug", () => {
  const path = postgresTaskRoute(TASK_ID, "[ready] Crème brûlée / Test");
  assert.equal(path, `/dishes/${TASK_ID}/ready-creme-brulee-test`);
  assert.deepEqual(parsePostgresTaskRoute(path), { taskId: TASK_ID, slug: "ready-creme-brulee-test" });
  assert.equal(parsePostgresTaskRoute(`/dishes/${TASK_ID}/old-title`).taskId, TASK_ID);
});

test("invalid PostgreSQL route identity fails closed", () => {
  assert.equal(parsePostgresTaskRoute("/dishes/00000000-0000-0000-0000-000000000000/title"), null);
  assert.throws(() => postgresTaskRoute("raw-id", "Title"));
  assert.equal(titleSlug("***"), "task");
});
