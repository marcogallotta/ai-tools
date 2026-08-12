import assert from "node:assert/strict";
import test from "node:test";
import { REVIEW_SCENARIOS, isAdminReviewScenario, isReviewScenario, scenarioHref, scenarioTaskId } from "../../src/js/review/review-catalog.js";

test("review catalogue exposes stable principal scenarios", () => {
  assert.deepEqual(REVIEW_SCENARIOS.map((item) => item.id), [
    "board", "attention", "detail", "fallback", "extreme", "admin", "admin-extreme", "admin-empty", "zero", "loading", "initial-error", "last-safe", "login",
  ]);
  assert.equal(isReviewScenario("extreme"), true);
  assert.equal(isReviewScenario("unknown"), false);
  assert.equal(isAdminReviewScenario("admin"), true);
  assert.equal(isAdminReviewScenario("board"), false);
  assert.equal(scenarioTaskId("detail"), "task-biryani");
  assert.equal(scenarioHref("fallback", "http://review.test"), "/task/task-aubergine?review=1&scenario=fallback");
  assert.equal(scenarioHref("admin", "http://review.test"), "/admin?review=1&scenario=admin");
  assert.equal(scenarioHref("admin-extreme", "http://review.test"), "/admin?review=1&scenario=admin-extreme");
  assert.equal(scenarioHref("admin-empty", "http://review.test"), "/admin?review=1&scenario=admin-empty");
  assert.equal(scenarioHref("login", "http://review.test"), "/?review=1&view=login");
});
