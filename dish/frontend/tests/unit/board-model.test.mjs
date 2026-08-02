import test from "node:test";
import assert from "node:assert/strict";
import { boardFixture, attentionLabels } from "../../fixtures/stage1-board.js";
import { appendContinuation, loadedTaskText, sectionHeading } from "../../src/js/features/board/board-model.js";
import { cardAccessibleName, workflowStatusText } from "../../src/js/features/cards/card-model.js";

test("fixture covers every approved attention category", () => {
  const represented = new Set(boardFixture.sections.flatMap((section) => section.cards.flatMap((card) => card.attention)));
  assert.deepEqual([...represented].sort(), Object.keys(attentionLabels).sort());
});

test("section headings disambiguate duplicate labels with project labels", () => {
  const ready = boardFixture.sections.filter((section) => section.label === "Ready");
  assert.deepEqual(ready.map(sectionHeading), ["Cooking / Ready", "Seasonal menu / Ready"]);
});

test("continuation appends once and becomes terminal", () => {
  const section = boardFixture.sections[0];
  const updated = appendContinuation(section);
  assert.equal(updated.cards.length, section.cards.length + section.continuation.length);
  assert.equal(updated.hasMore, false);
  assert.deepEqual(appendContinuation(updated), updated);
});

test("card labels remain factual and concise", () => {
  const card = boardFixture.sections[0].cards[0];
  assert.equal(workflowStatusText(card.status), "No active operation");
  assert.match(cardAccessibleName(card, attentionLabels), /Lease needs attention/);
  assert.equal(loadedTaskText(1, true), "1 task loaded; more available");
});
