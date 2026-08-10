import assert from "node:assert/strict";
import test from "node:test";
import { DetailContractMismatch, mapTaskDetailResponse } from "../../src/js/features/detail/api-detail-model.js";

const TASK_ID = "12345678-1234-5678-1234-567812345678";

function payload() {
  return {
    task_id: TASK_ID,
    title: "Exact task",
    project_label: "Cooking",
    section_label: "Research Queue",
    destination_label: null,
    workflow_status: { state: "active_operation", operation: "Initial", phase: "Prepare required" },
    attention_codes: ["isolated", "lease_attention", "projection_abnormal"],
    body_presentation: { state: "sanitized_html", html: "<p>Safe</p>" },
    disclosures: [{ code: "lease", label: "Lease", detail: "constructor lease is expired." }],
    advisory: { code: "workflow.prepare_required", message: "Preparation is required.", perspective: "workflow", invokable_by_frontend: false },
    projection: { state: "drifted", message: "Projection drift exists.", observation_time: "2026-08-08T22:00:00+00:00" },
    notices: [
      { code: "isolated", severity: "warning", message: "Isolated.", target: { type: "task", route_identity: TASK_ID } },
      { code: "lease_attention", severity: "warning", message: "Lease.", target: { type: "task", route_identity: TASK_ID } },
      { code: "projection_abnormal", severity: "warning", message: "Projection.", target: { type: "task", route_identity: TASK_ID } },
    ],
  };
}

test("Stage 4 detail maps closed DTO with canonical Dish UUID and non-authorizing advisory", () => {
  const detail = mapTaskDetailResponse(payload());
  assert.equal(detail.id, TASK_ID);
  assert.equal(detail.bodyPresentation.state, "sanitized_html");
  assert.deepEqual(detail.attention, ["isolated", "lease_attention", "projection_abnormal"]);
  assert.equal(detail.advisory.invokableByFrontend, false);
  assert.equal(detail.projection.state, "drifted");
});

test("fallback body requires exactly one render_rejected notice", () => {
  const raw = payload();
  raw.attention_codes = [];
  raw.disclosures = [];
  raw.projection = null;
  raw.body_presentation = { state: "plain_text_fallback", text: "raw <script>" };
  raw.notices = [{ code: "render_rejected", severity: "warning", message: "Fallback.", target: { type: "task", route_identity: TASK_ID } }];
  assert.equal(mapTaskDetailResponse(raw).bodyPresentation.text, "raw <script>");
  raw.notices = [];
  assert.throws(() => mapTaskDetailResponse(raw), DetailContractMismatch);
});

test("detail rejects missing disclosure and invalid task route identity", () => {
  const missing = payload();
  missing.disclosures = [];
  assert.throws(() => mapTaskDetailResponse(missing), DetailContractMismatch);
  const raw = payload();
  raw.task_id = `r1t-${"x".repeat(27)}`;
  assert.throws(() => mapTaskDetailResponse(raw), DetailContractMismatch);
});
