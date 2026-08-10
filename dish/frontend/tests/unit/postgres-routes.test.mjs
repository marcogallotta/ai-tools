import assert from "node:assert/strict";
import test from "node:test";
import { parsePostgresTaskRoute, postgresSourceSuffix, postgresTaskRoute, titleSlug, writePostgresRoute } from "../../src/js/features/routing/routes.js";

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


test("PostgreSQL navigation preserves only the approved local source selector", () => {
  assert.equal(postgresSourceSuffix("?source=postgresql"), "?source=postgresql");
  assert.equal(postgresSourceSuffix("?source=postgresql&x=1"), "?source=postgresql");
  assert.equal(postgresSourceSuffix("?source=fixture"), "");
  assert.equal(postgresSourceSuffix("?source=postgresql&source=postgresql"), "");
});


test("PostgreSQL history navigation drops unrelated query parameters", () => {
  const priorWindow = globalThis.window;
  const priorHistory = globalThis.history;
  const calls = [];
  globalThis.window = { location: { search: "?source=postgresql&x=1" } };
  globalThis.history = { replaceState: (...args) => calls.push(args) };
  try {
    assert.equal(writePostgresRoute("/", "replace", { marker: true }), true);
    assert.deepEqual(calls, [[{ marker: true }, "", "/?source=postgresql"]]);
  } finally {
    globalThis.window = priorWindow;
    globalThis.history = priorHistory;
  }
});
