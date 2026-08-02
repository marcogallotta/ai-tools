import { attentionLabels, boardFixture, zeroSectionFixture } from "../../../fixtures/stage1-board.js";
import { detailForCard } from "../../../fixtures/stage1-details.js";
import { loadFixtureContinuation, renderBoard } from "../features/board/board.js";
import { closeTaskDetail, openTaskDetail } from "../features/detail/task-detail.js";
import { createApplicationFrame } from "../shell/application-shell.js";

export function fixtureForScenario(name) {
  return name === "zero" ? structuredClone(zeroSectionFixture) : structuredClone(boardFixture);
}

export function renderFixturePrototype(root, scenario = "board") {
  closeTaskDetail({ restoreFocus: false });
  const { shell, main } = createApplicationFrame();
  shell.dataset.shellState = "fixture-board";
  const live = document.createElement("p");
  live.className = "sr-only";
  live.setAttribute("aria-live", "polite");
  shell.append(live);

  const board = fixtureForScenario(scenario);
  const options = {
    attentionLabels,
    onSelect: (card, origin) => openTaskDetail(detailForCard(card), origin),
    announce: (message) => { live.textContent = message; },
    onLoadMore: (section, region) => {
      const updated = loadFixtureContinuation(section, region, options);
      const index = board.sections.findIndex((item) => item.id === section.id);
      board.sections[index] = updated;
    },
  };
  renderBoard(main, board, options);
  root.replaceChildren(shell);
  root.dataset.shellState = "fixture-board";
  root.dataset.fixtureScenario = scenario;
}
