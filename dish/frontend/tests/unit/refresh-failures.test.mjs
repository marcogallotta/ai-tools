import assert from "node:assert/strict";
import test from "node:test";
import { FrontendApiError, FrontendContractMismatch } from "../../src/js/api/http-transport.js";
import { refreshFailureNotice } from "../../src/js/features/refresh/failures.js";

test("task-data contract mismatch stops automatic reuse and offers a full reload", () => {
  assert.deepEqual(refreshFailureNotice(new FrontendContractMismatch()), {
    code: "contract_mismatch",
    message: "The PostgreSQL response no longer matches this frontend. Reload the page before continuing.",
    action: "reload",
  });
});

test("temporary service failures remain retryable and client-update-required reloads", () => {
  const unavailable = new FrontendApiError({ status: 503, code: "service_unavailable", message: "Try again." });
  assert.deepEqual(refreshFailureNotice(unavailable), { code: "service_unavailable", message: "Try again.", action: "retry" });
  const update = new FrontendApiError({ status: 403, code: "client_update_required", message: "Update." });
  assert.equal(refreshFailureNotice(update).action, "reload");
});
