import test from "node:test";
import assert from "node:assert/strict";
import { boardScrollBehavior } from "../../src/js/features/accessibility/board-keyboard.js";
import { cardAccessibleName } from "../../src/js/features/cards/card-model.js";
import { attentionLabels, boardFixture } from "../../fixtures/stage1-board.js";

test("card accessible names include factual status and approved attention labels", () => {
  const card = boardFixture.sections[1].cards[0];
  const name = cardAccessibleName(card, attentionLabels);
  assert.match(name, /Verification · Human review/);
  assert.match(name, /PENDING REVIEW/);
  assert.match(name, /Lease needs attention/);
});

test("board horizontal scrolling respects reduced-motion preference", () => {
  assert.equal(boardScrollBehavior(() => ({ matches: true })), "auto");
  assert.equal(boardScrollBehavior(() => ({ matches: false })), "smooth");
  assert.equal(boardScrollBehavior(undefined), "smooth");
});
