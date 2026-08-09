import test from "node:test";
import assert from "node:assert/strict";
import { boardFixture } from "../../fixtures/stage1-board.js";
import { detailFixtures } from "../../fixtures/stage1-details.js";
import { effectiveTaskContributions, groupNotices, noticeHeading } from "../../src/js/features/notices/notice-model.js";

test("repeated task conditions group by distinct current task", () => {
  const board = structuredClone(boardFixture);
  board.sections[1].cards[0].attention.push("lease_attention");
  const groups = groupNotices(effectiveTaskContributions(board));
  const lease = groups.find((group) => group.code === "lease_attention");
  assert.equal(lease.count, 2);
  assert.equal(noticeHeading(lease), "Lease needs attention — 2 tasks");
  assert.deepEqual(lease.tasks.map((task) => task.title), [
    "Aubergine, tomato and chickpea tray — decide the final acid and herb finish",
    "Crisp-skinned fish with preserved lemon potatoes",
  ]);
});

test("single task notice preserves the affected task identity and title", () => {
  const board = structuredClone(boardFixture);
  const groups = groupNotices(effectiveTaskContributions(board));
  const isolated = groups.find((group) => group.code === "isolated");
  assert.equal(noticeHeading(isolated), "ISOLATED");
  assert.equal(isolated.tasks[0].title, "Smoky jollof-style rice");
});

test("fresh detail supersedes the selected card contribution", () => {
  const detail = { ...detailFixtures["task-aubergine"], attention: ["hold_active"] };
  const contributions = effectiveTaskContributions(structuredClone(boardFixture), detail);
  assert.equal(contributions.some((item) => item.taskId === detail.id && item.code === "lease_attention"), false);
  assert.equal(contributions.some((item) => item.taskId === detail.id && item.code === "hold_active"), true);
  assert.equal(contributions.some((item) => item.code === "render_rejected"), true);
});


test("lifecycle contribution preserves an already-known task title", () => {
  const board = structuredClone(boardFixture);
  const contributions = effectiveTaskContributions(board);
  const lifecycle = [{
    code: "lease_attention",
    taskId: "task-aubergine",
    message: "Lease state changed while detail was open.",
  }];
  const groups = groupNotices(contributions, lifecycle);
  const lease = groups.find((group) => group.code === "lease_attention");
  const task = lease.tasks.find((item) => item.id === "task-aubergine");
  assert.equal(
    task.title,
    "Aubergine, tomato and chickpea tray — decide the final acid and herb finish",
  );
  assert.equal(lease.message, "Lease state changed while detail was open.");
});

test("unknown notice codes fail closed", () => {
  assert.throws(() => groupNotices([{ code: "invented_state", taskId: "fixture" }]), /Unknown notice code/);
});
