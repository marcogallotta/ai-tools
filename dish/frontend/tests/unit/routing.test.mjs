import test from "node:test";
import assert from "node:assert/strict";
import { adminInspectRoute } from "../../src/js/features/admin/admin.js";
import { PROTOTYPE_BOARD_ROUTE, parseTaskRoute, taskRoute } from "../../src/js/prototype/prototype-routes.js";

test("fixture task identities round-trip through the closed route grammar", () => {
  assert.equal(PROTOTYPE_BOARD_ROUTE, "/");
  assert.equal(taskRoute("task-biryani"), "/task/task-biryani");
  assert.equal(parseTaskRoute("/task/task-biryani"), "task-biryani");
});

test("invalid and multi-segment task routes normalize to the board", () => {
  assert.equal(parseTaskRoute("/task/"), null);
  assert.equal(parseTaskRoute("/task/task/extra"), null);
  assert.equal(parseTaskRoute("/frontend/session"), null);
  assert.throws(() => taskRoute("task/unsafe"), /Invalid fixture route identity/);
});

test("admin inspect route accepts canonical Dish UUID only and preserves approved source", () => {
  const dish = "a13a0eb9-2d84-4d0b-92d1-672004db78ba";
  assert.equal(adminInspectRoute(`  ${dish.toUpperCase()}  `), `/dishes/${dish}/dish`);
  assert.equal(adminInspectRoute(dish, "?source=postgresql"), `/dishes/${dish}/dish?source=postgresql`);
  assert.throws(() => adminInspectRoute("not-a-dish"), /Invalid PostgreSQL task route identity/);
  assert.throws(() => adminInspectRoute("00000000-0000-0000-0000-000000000000"), /Invalid PostgreSQL task route identity/);
});
