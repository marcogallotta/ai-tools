import { attentionLabels, boardFixture, zeroSectionFixture } from "../../../fixtures/stage1-board.js";
import { detailForCard } from "../../../fixtures/stage1-details.js";
import { lifecycleFixtures } from "../../../fixtures/stage1-states.js";
import { loadFixtureContinuation, renderBoard } from "../features/board/board.js";
import { closeTaskDetail, openTaskDetail } from "../features/detail/task-detail.js";
import { effectiveTaskContributions, groupNotices } from "../features/notices/notice-model.js";
import { renderNotices } from "../features/notices/notices.js";
import { renderInitialErrorState, renderLoadingState } from "../features/refresh/state-shells.js";
import { createApplicationFrame } from "../shell/application-shell.js";

export function fixtureForScenario(name) {
  return name === "zero" ? structuredClone(zeroSectionFixture) : structuredClone(boardFixture);
}

function lifecycleNotice(state) {
  return state?.code ? [{ code: state.code, message: state.message }] : [];
}

export function renderFixturePrototype(root, scenario = "board") {
  closeTaskDetail({ restoreFocus: false });
  const { shell, main, noticeHost } = createApplicationFrame();
  shell.dataset.shellState = "fixture-board";
  const live = document.createElement("p");
  live.className = "sr-only";
  live.setAttribute("aria-live", "polite");
  shell.append(live);
  const board = fixtureForScenario(scenario);
  let selectedDetail = null;

  const updateNotices = (state = null) => {
    const taskNotices = effectiveTaskContributions(board, selectedDetail);
    renderNotices(noticeHost, groupNotices(taskNotices, lifecycleNotice(state)));
  };
  const options = {
    attentionLabels,
    onSelect: (card, origin) => {
      selectedDetail = detailForCard(card);
      openTaskDetail(selectedDetail, origin, {
        onClose: () => { selectedDetail = null; updateNotices(); },
      });
      updateNotices();
    },
    announce: (message) => { live.textContent = message; },
    onLoadMore: (section, region) => {
      const updated = loadFixtureContinuation(section, region, options);
      const index = board.sections.findIndex((item) => item.id === section.id);
      board.sections[index] = updated;
      updateNotices();
    },
  };

  if (scenario === "loading") {
    renderLoadingState(main);
  } else if (scenario === "initial-error") {
    renderInitialErrorState(main, () => renderFixturePrototype(root, "board"));
    updateNotices(lifecycleFixtures.initialError);
  } else {
    renderBoard(main, board, options);
    updateNotices(scenario === "last-safe" ? lifecycleFixtures.lastSafe : null);
  }
  root.replaceChildren(shell);
  root.dataset.shellState = "fixture-board";
  root.dataset.fixtureScenario = scenario;
}
