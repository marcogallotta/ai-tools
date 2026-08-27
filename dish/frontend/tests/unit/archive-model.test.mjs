import test from "node:test";
import assert from "node:assert/strict";
import { ArchiveContractMismatch, mapArchiveResponse } from "../../src/js/features/archive/archive-model.js";

const taskId = "12345678-1234-5678-1234-567812345678";

test("archive contract maps PostgreSQL archive rows", () => {
  const mapped = mapArchiveResponse({
    generated_at: "2026-08-27T10:00:00+00:00",
    dishes: [{ task_id: taskId, title: "Miso soup", archived_at: "2026-08-27T09:00:00+00:00" }],
    truncated: false,
  });
  assert.equal(mapped.dishes[0].title, "Miso soup");
  assert.equal(mapped.dishes[0].archivedAt, "2026-08-27T09:00:00+00:00");
});

test("archive contract rejects duplicate task identities and extra fields", () => {
  const dish = { task_id: taskId, title: "Miso soup", archived_at: "2026-08-27T09:00:00+00:00" };
  assert.throws(() => mapArchiveResponse({ generated_at: "2026-08-27T10:00:00+00:00", dishes: [dish, dish], truncated: false }), ArchiveContractMismatch);
  assert.throws(() => mapArchiveResponse({ generated_at: "2026-08-27T10:00:00+00:00", dishes: [], truncated: false, source: "asana" }), ArchiveContractMismatch);
});
