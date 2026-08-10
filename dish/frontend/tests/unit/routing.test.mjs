import test from "node:test";
import assert from "node:assert/strict";
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
