import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  WORKFLOW_LABEL_MAX_LENGTH,
  WORKFLOW_OPERATION_LABELS,
  WORKFLOW_PHASE_LABELS,
} from "../../src/js/features/board/api-board-model.js";

const openapi = JSON.parse(
  await readFile(new URL("../../openapi/frontend.openapi.json", import.meta.url)),
);

test("WorkflowStatus OpenAPI and browser validation share one presentation registry", () => {
  const variants = openapi.components.schemas.WorkflowStatus.oneOf;
  const inactive = variants.find((item) => item.properties.state.enum.includes("no_active_operation"));
  const active = variants.find((item) => item.properties.state.enum.includes("active_operation"));

  assert.deepEqual(inactive.required, ["state"]);
  assert.deepEqual(active.required, ["state", "operation", "phase"]);
  assert.deepEqual(active.properties.operation.enum, [...WORKFLOW_OPERATION_LABELS]);
  assert.deepEqual(active.properties.phase.enum, [...WORKFLOW_PHASE_LABELS]);
  assert.equal(active.properties.operation.maxLength, WORKFLOW_LABEL_MAX_LENGTH);
  assert.equal(active.properties.phase.maxLength, WORKFLOW_LABEL_MAX_LENGTH);
});
