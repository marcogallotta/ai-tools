import test from "node:test";
import assert from "node:assert/strict";
import { FRONTEND_OPERATION_IDS, GeneratedFrontendClient } from "../../src/js/api/generated/frontend-client.js";

 test("generated client includes the bounded frontend route set", () => {
  assert.deepEqual(FRONTEND_OPERATION_IDS, [
    "frontendLogin",
    "frontendLogout",
    "getFrontendAdmin",
    "getFrontendBoard",
    "getFrontendSearch",
    "getFrontendSectionTasks",
    "getFrontendSession",
    "getFrontendTaskDetail",
  ]);
});

test("generated task-detail client encodes route identities", async () => {
  const calls = [];
  const client = new GeneratedFrontendClient({ request: (request) => calls.push(request) });
  client.getFrontendTaskDetail("task/unsafe");
  assert.equal(calls[0].path, "/frontend/tasks/task%2Funsafe");
});
