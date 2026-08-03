import assert from "node:assert/strict";
import test from "node:test";
import { REVIEW_SCENARIOS, isReviewScenario, scenarioHref, scenarioTaskId } from "../../src/js/review/review-catalog.js";

test("review catalogue exposes stable principal scenarios", () => {
  assert.deepEqual(REVIEW_SCENARIOS.map((item) => item.id), [
    "board", "attention", "detail", "fallback", "extreme", "zero", "loading", "initial-error", "last-safe", "login",
  ]);
  assert.equal(isReviewScenario("extreme"), true);
  assert.equal(isReviewScenario("unknown"), false);
  assert.equal(scenarioTaskId("detail"), "task-biryani");
  assert.equal(scenarioHref("fallback", "http://review.test"), "/task/task-aubergine?review=1&scenario=fallback");
  assert.equal(scenarioHref("login", "http://review.test"), "/?review=1&view=login");
});
