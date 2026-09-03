import test from "node:test";
import assert from "node:assert/strict";
import { ArchiveContractMismatch, mapArchiveResponse } from "../../src/js/features/archive/archive-model.js";

const taskId = "12345678-1234-5678-1234-567812345678";

test("archive adapter maps cooked rows and their logs", () => {
  const mapped = mapArchiveResponse({
    generated_at: "2026-08-27T10:00:00+00:00",
    dishes: [{ task_id: taskId, title: "Miso soup", archived_at: "2026-08-27T09:00:00+00:00", cook_logs: [{ recorded_at: "2026-08-27T08:00:00+00:00", text: "Served" }] }],
    truncated: false,
  });
  assert.equal(mapped.dishes[0].title, "Miso soup");
  assert.equal(mapped.dishes[0].archivedAt, "2026-08-27T09:00:00+00:00");
  assert.deepEqual(mapped.dishes[0].cookLogs, [{ recordedAt: "2026-08-27T08:00:00+00:00", text: "Served" }]);
});

test("archive contract rejects duplicate task identities and extra fields", () => {
  const dish = { task_id: taskId, title: "Miso soup", archived_at: "2026-08-27T09:00:00+00:00", cook_logs: [] };
  assert.throws(() => mapArchiveResponse({ generated_at: "2026-08-27T10:00:00+00:00", dishes: [dish, dish], truncated: false }), ArchiveContractMismatch);
  assert.throws(() => mapArchiveResponse({ generated_at: "2026-08-27T10:00:00+00:00", dishes: [], truncated: false, source: "asana" }), ArchiveContractMismatch);
});
