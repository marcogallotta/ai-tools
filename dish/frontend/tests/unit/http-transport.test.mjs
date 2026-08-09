import assert from "node:assert/strict";
import test from "node:test";
import {
  FrontendApiError,
  FrontendContractMismatch,
  FrontendHttpClient,
} from "../../src/js/api/http-transport.js";
import { FRONTEND_CONTRACT_VERSION } from "../../src/js/config.js";

function response(payload, { status = 200, contract = FRONTEND_CONTRACT_VERSION, contentType = "application/json" } = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": contentType,
      "X-Dish-Frontend-Contract": contract,
    },
  });
}

test("same-origin client sends contract header and binds continuation cursor", async () => {
  const calls = [];
  const client = new FrontendHttpClient({
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      return response({ ok: true });
    },
  });
  await client.board();
  await client.sectionTasks("r1s-safe", "c1.cursor/value");
  await client.taskDetail("r1t-safe/value");
  assert.equal(calls[0].path, "/frontend/board");
  assert.equal(calls[0].options.headers["X-Dish-Frontend-Contract"], FRONTEND_CONTRACT_VERSION);
  assert.equal(calls[1].path, "/frontend/sections/r1s-safe/tasks?cursor=c1.cursor%2Fvalue");
  assert.equal(calls[2].path, "/frontend/tasks/r1t-safe%2Fvalue");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.redirect, "manual");
});

test("transport invokes browser fetch with the global receiver", async () => {
  let receiver;
  const client = new FrontendHttpClient({
    fetchImpl: function () {
      receiver = this;
      return response({ ok: true });
    },
  });
  await client.board();
  assert.equal(receiver, globalThis);
});

test("response contract mismatch remains client-local", async () => {
  const client = new FrontendHttpClient({
    fetchImpl: async () => response({ snapshot_id: "ignored" }, { contract: "other" }),
  });
  await assert.rejects(
    client.board(),
    (error) => error instanceof FrontendContractMismatch && error.code === "contract_mismatch",
  );
});

test("known server errors remain typed and server contract_mismatch is rejected", async () => {
  const staleClient = new FrontendHttpClient({
    fetchImpl: async () => response(
      { error: { code: "cursor_stale", message: "Cursor is stale." } },
      { status: 409 },
    ),
  });
  await assert.rejects(
    staleClient.sectionTasks("r1s-safe", "c1.cursor"),
    (error) => error instanceof FrontendApiError && error.code === "cursor_stale" && error.status === 409,
  );

  const invalidServerCode = new FrontendHttpClient({
    fetchImpl: async () => response(
      { error: { code: "contract_mismatch", message: "Wrong." } },
      { status: 503 },
    ),
  });
  await assert.rejects(invalidServerCode.board(), FrontendContractMismatch);
});

test("HTTP status and registered error code pairing fails closed", async () => {
  const client = new FrontendHttpClient({
    fetchImpl: async () => response(
      { error: { code: "cursor_stale", message: "Cursor is stale." } },
      { status: 503 },
    ),
  });
  await assert.rejects(client.sectionTasks("r1s-safe", "c1.cursor"), FrontendContractMismatch);
});

test("JSON-like but incompatible media types fail closed", async () => {
  const client = new FrontendHttpClient({
    fetchImpl: async () => response({ sections: [] }, { contentType: "application/json-patch+json" }),
  });
  await assert.rejects(client.board(), FrontendContractMismatch);
});

test("non-JSON success responses fail closed", async () => {
  const client = new FrontendHttpClient({
    fetchImpl: async () => response({ ok: true }, { contentType: "text/plain" }),
  });
  await assert.rejects(client.board(), FrontendContractMismatch);
});


test("task detail errors are status-bound", async () => {
  const missing = new FrontendHttpClient({
    fetchImpl: async () => response({ error: { code: "task_not_found", message: "Missing." } }, { status: 404 }),
  });
  await assert.rejects(missing.taskDetail("r1t-safe"), (error) => error instanceof FrontendApiError && error.code === "task_not_found");
  const wrong = new FrontendHttpClient({
    fetchImpl: async () => response({ error: { code: "task_ineligible", message: "No." } }, { status: 404 }),
  });
  await assert.rejects(wrong.taskDetail("r1t-safe"), FrontendContractMismatch);
});
