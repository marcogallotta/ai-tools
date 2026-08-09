import assert from "node:assert/strict";
import test from "node:test";
import {
  blockRepeatedInvalidCursor,
  reconcileBoard,
  refreshRetryDelayMs,
  resetSectionContinuation,
} from "../../src/js/features/refresh/reconciliation.js";

const sectionA = `r1s-${"a".repeat(27)}`;
const sectionB = `r1s-${"b".repeat(27)}`;
const task = (letter) => ({ id: `r1t-${letter.repeat(27)}`, title: letter, attention: [], status: { state: "no_active_operation" } });

function section(id, continuityId, cards, nextCursor = null, firstPageCount = cards.length) {
  return { id, label: id, projectLabel: null, continuityId, cards, firstPageCount, nextCursor, hasMore: nextCursor !== null, loadMoreBlocked: false, resetPending: false };
}

function board(sections, snapshotId = "snap") { return { snapshotId, pageSize: 2, sections }; }

test("refresh retry delay is jittered, bounded, and never exceeds the active-view ceiling", () => {
  assert.equal(refreshRetryDelayMs(1, 30000, 0), 750);
  assert.equal(refreshRetryDelayMs(1, 30000, 1), 1000);
  assert.equal(refreshRetryDelayMs(6, 30000, 1), 30000);
  assert.ok(refreshRetryDelayMs(9, 10000, 0.5) <= 10000);
  assert.throws(() => refreshRetryDelayMs(0, 30000, 0.5));
  assert.throws(() => refreshRetryDelayMs(1, 30001, 0.5));
});

test("refresh replaces first pages but retains compatible loaded continuation pages", () => {
  const old = board([
    section(sectionA, "same", [task("a"), task("b"), task("c"), task("d")], "cursor-after-d", 2),
    section(sectionB, "same-b", [task("e")]),
  ], "old");
  const fresh = board([
    section(sectionA, "same", [task("x"), task("a")], "fresh-page-two", 2),
    section(sectionB, "same-b", [task("e")]),
  ], "new");
  const result = reconcileBoard(old, fresh);
  assert.deepEqual(result.sections[0].cards.map((item) => item.title), ["x", "a", "c", "d"]);
  assert.equal(result.sections[0].nextCursor, "cursor-after-d");
  assert.equal(result.sections[0].firstPageCount, 2);
  assert.equal(result.snapshotId, "new");
});

test("refresh drops loaded pages when continuity changes and removes cross-section duplicates", () => {
  const old = board([
    section(sectionA, "old-a", [task("a"), task("b"), task("c")], "cursor-a", 2),
    section(sectionB, "same-b", [task("d"), task("e"), task("x")], "cursor-b", 2),
  ]);
  const fresh = board([
    section(sectionA, "new-a", [task("q")], "fresh-a", 1),
    section(sectionB, "same-b", [task("x"), task("d")], "fresh-b", 2),
  ]);
  const result = reconcileBoard(old, fresh);
  assert.deepEqual(result.sections[0].cards.map((item) => item.title), ["q"]);
  assert.equal(result.sections[0].nextCursor, "fresh-a");
  assert.deepEqual(result.sections[1].cards.map((item) => item.title), ["x", "d"]);
});

test("cursor reset retains only the compatible first page and a repeated invalid request blocks load more", () => {
  const original = board([section(sectionA, "same", [task("a"), task("b"), task("c")], "bad-cursor", 2)]);
  const reset = resetSectionContinuation(original, sectionA);
  assert.deepEqual(reset.sections[0].cards.map((item) => item.title), ["a", "b"]);
  assert.equal(reset.sections[0].hasMore, false);
  assert.equal(reset.sections[0].resetPending, true);

  const fresh = board([section(sectionA, "same", [task("a"), task("b")], "bad-cursor", 2)]);
  const reconciled = reconcileBoard(reset, fresh);
  const blocked = blockRepeatedInvalidCursor(reconciled, sectionA, "bad-cursor");
  assert.equal(blocked.sections[0].loadMoreBlocked, true);
});
