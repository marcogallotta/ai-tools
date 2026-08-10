import assert from "node:assert/strict";
import test from "node:test";
import {
  appendSectionPage,
  BoardContractMismatch,
  mapBoardResponse,
  mapSectionPageResponse,
} from "../../src/js/features/board/api-board-model.js";

const sectionId = `r1s-${"s".repeat(27)}`;
const taskA = "12345678-1234-5678-1234-567812345678";
const taskB = "12345678-1234-5678-1234-567812345679";

function boardDto() {
  return {
    snapshot_id: "d1-snapshot",
    page_size: 1,
    sections: [{
      section_id: sectionId,
      section_label: "Research Queue",
      continuity_id: "d1-continuity",
      cards: [{
        task_id: taskA,
        title: "First task",
        section_id: sectionId,
        workflow_status: { state: "no_active_operation" },
        attention_codes: ["projection_abnormal", "isolated"],
      }],
      next_cursor: "c1.next",
    }],
    notices: [
      { code: "isolated", task_id: taskA, severity: "warning" },
      { code: "projection_abnormal", task_id: taskA, severity: "warning" },
    ],
  };
}

test("Stage 3 board DTO maps into existing board objects with ISOLATED first", () => {
  const board = mapBoardResponse(boardDto());
  assert.equal(board.sections[0].id, sectionId);
  assert.equal(board.sections[0].hasMore, true);
  assert.deepEqual(board.sections[0].cards[0].attention, ["isolated", "projection_abnormal"]);
  assert.equal(board.sections[0].cards[0].id, taskA);
});

test("continuation is bound to section continuity and rejects duplicate task identities", () => {
  const board = mapBoardResponse(boardDto());
  const raw = {
    section_id: sectionId,
    continuity_id: "d1-continuity",
    cards: [{
      task_id: taskB,
      title: "Second task",
      section_id: sectionId,
      workflow_status: { state: "active_operation", operation: "Planning", phase: "Prepare required" },
      attention_codes: [],
    }],
    next_cursor: null,
    notices: [],
  };
  const page = mapSectionPageResponse(raw, board.sections[0]);
  const appended = appendSectionPage(board, sectionId, page);
  assert.deepEqual(appended.sections[0].cards.map((card) => card.id), [taskA, taskB]);
  assert.equal(appended.sections[0].hasMore, false);

  raw.cards[0].task_id = taskA;
  const duplicate = mapSectionPageResponse(raw, board.sections[0]);
  assert.throws(() => appendSectionPage(board, sectionId, duplicate), BoardContractMismatch);
});

test("legacy opaque task identities and mismatched notice sets fail closed", () => {
  const raw = boardDto();
  raw.sections[0].cards[0].task_id = `r1t-${"x".repeat(27)}`;
  assert.throws(() => mapBoardResponse(raw), BoardContractMismatch);

  const badNotices = boardDto();
  badNotices.notices.pop();
  assert.throws(() => mapBoardResponse(badNotices), BoardContractMismatch);
});

test("workflow status accepts only the closed active and inactive shapes", () => {
  const active = boardDto();
  active.sections[0].cards[0].workflow_status = {
    state: "active_operation",
    operation: "Initial",
    phase: "Await verification",
  };
  assert.equal(mapBoardResponse(active).sections[0].cards[0].status.phase, "Await verification");

  for (const workflowStatus of [
    { state: "no_active_operation", operation: "Initial" },
    { state: "active_operation", operation: "Initial" },
    { state: "active_operation", operation: "Invented", phase: "Await verification" },
    { state: "active_operation", operation: "Initial", phase: "Invented phase" },
  ]) {
    const raw = boardDto();
    raw.sections[0].cards[0].workflow_status = workflowStatus;
    assert.throws(() => mapBoardResponse(raw), BoardContractMismatch);
  }
});

test("bounded strings count Unicode code points like the server contract", () => {
  const exact = boardDto();
  exact.sections[0].cards[0].title = "😀".repeat(500);
  assert.equal(Array.from(mapBoardResponse(exact).sections[0].cards[0].title).length, 500);

  const tooLong = boardDto();
  tooLong.sections[0].cards[0].title = "😀".repeat(501);
  assert.throws(() => mapBoardResponse(tooLong), BoardContractMismatch);
});
