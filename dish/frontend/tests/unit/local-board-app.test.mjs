import assert from "node:assert/strict";
import test from "node:test";
import { LocalBoardRequestState } from "../../src/js/features/refresh/request-state.js";

function section(overrides = {}) {
  return {
    id: "r1s-" + "s".repeat(27),
    continuityId: "d1-continuity",
    nextCursor: "c1.cursor-one",
    ...overrides,
  };
}

function board(currentSection) {
  return { sections: [currentSection] };
}

test("newer bootstrap generations fence older bootstrap responses", () => {
  const state = new LocalBoardRequestState();
  const older = state.beginBootstrap();
  const newer = state.beginBootstrap();
  assert.equal(state.acceptBootstrap(older), false);
  assert.equal(state.acceptBootstrap(newer), true);
  assert.equal(state.acceptedBoardGeneration, newer);
});

test("continuation requests are single-flight and bound to current board state", () => {
  const state = new LocalBoardRequestState();
  const generation = state.beginBootstrap();
  assert.equal(state.acceptBootstrap(generation), true);
  const current = section();
  const request = state.beginContinuation(current);
  assert.ok(request);
  assert.equal(state.beginContinuation(current), null);
  assert.equal(state.currentContinuationSection(request, board(current)), current);

  assert.equal(
    state.currentContinuationSection(request, board(section({ continuityId: "d1-new" }))),
    null,
  );
  assert.equal(
    state.currentContinuationSection(request, board(section({ nextCursor: "c1.cursor-two" }))),
    null,
  );
  assert.equal(
    state.currentContinuationSection(request, board(section({ id: "r1s-" + "x".repeat(27) }))),
    null,
  );

  state.finishContinuation(request);
  assert.ok(state.beginContinuation(current));
});

test("accepted bootstrap replacement invalidates older continuation work", () => {
  const state = new LocalBoardRequestState();
  const firstGeneration = state.beginBootstrap();
  assert.equal(state.acceptBootstrap(firstGeneration), true);
  const current = section();
  const request = state.beginContinuation(current);
  assert.ok(request);

  const secondGeneration = state.beginBootstrap();
  assert.equal(state.currentContinuationSection(request, board(current)), null);
  assert.equal(state.beginContinuation(current), null);
  assert.equal(state.acceptBootstrap(secondGeneration), true);
  assert.equal(state.currentContinuationSection(request, board(current)), null);
});


test("newer detail requests fence stale detail responses and close invalidates in-flight work", () => {
  const state = new LocalBoardRequestState();
  const first = state.beginDetail("r1t-" + "a".repeat(27));
  const second = state.beginDetail("r1t-" + "b".repeat(27));
  assert.equal(state.isCurrentDetail(first), false);
  assert.equal(state.isCurrentDetail(second), true);
  state.cancelDetail();
  assert.equal(state.isCurrentDetail(second), false);
});


test("blocked continuation state cannot start another request", () => {
  const state = new LocalBoardRequestState();
  const generation = state.beginBootstrap();
  assert.equal(state.acceptBootstrap(generation), true);
  assert.equal(state.beginContinuation(section({ loadMoreBlocked: true })), null);
});
