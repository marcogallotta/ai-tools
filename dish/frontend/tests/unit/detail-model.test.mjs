import test from "node:test";
import assert from "node:assert/strict";
import { detailFixtures, detailForCard } from "../../fixtures/stage1-details.js";
import { contentPresentation, detailStatusText } from "../../src/js/features/detail/detail-model.js";

test("rendered detail accepts only the supported structured block registry", () => {
  const detail = detailFixtures["task-biryani"];
  const presentation = contentPresentation(detail);
  assert.equal(presentation.mode, "rendered");
  assert.equal(presentation.blocks[0].kind, "heading");
  assert.equal(detailStatusText(detail), "Planning · Drafting");
});

test("renderer fallback remains inert plain text", () => {
  const detail = detailFixtures["task-aubergine"];
  const presentation = contentPresentation(detail);
  assert.equal(presentation.mode, "fallback");
  assert.match(presentation.text, /No links or markup are active/);
});

test("unmapped cards receive bounded representative detail", () => {
  const detail = detailForCard({
    id: "fixture-unmapped",
    title: "Fixture task",
    status: { state: "no_active_operation" },
    attention: [],
  });
  assert.equal(detail.id, "fixture-unmapped");
  assert.match(detail.nextStep, /descriptive only/);
});
