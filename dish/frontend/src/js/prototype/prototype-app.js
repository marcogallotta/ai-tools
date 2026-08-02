import { attentionLabels, boardFixture, zeroSectionFixture } from "../../../fixtures/stage1-board.js";
import { detailForCard } from "../../../fixtures/stage1-details.js";
import { lifecycleFixtures } from "../../../fixtures/stage1-states.js";
import { loadFixtureContinuation, renderBoard } from "../features/board/board.js";
import { closeTaskDetail, openTaskDetail } from "../features/detail/task-detail.js";
import { effectiveTaskContributions, groupNotices } from "../features/notices/notice-model.js";
import { renderNotices } from "../features/notices/notices.js";
import { BOARD_ROUTE, parseTaskRoute, taskRoute, writePrototypeRoute } from "../features/routing/routes.js";
import { renderInitialErrorState, renderLoadingState } from "../features/refresh/state-shells.js";
import { createApplicationFrame } from "../shell/application-shell.js";

let removeRouteListener = null;

export function fixtureForScenario(name) {
  return name === "zero" ? structuredClone(zeroSectionFixture) : structuredClone(boardFixture);
}

function lifecycleNotice(state) {
  return state?.code ? [{ code: state.code, message: state.message }] : [];
}

function findCard(board, taskId) {
  return board.sections.flatMap((section) => section.cards).find((card) => card.id === taskId) ?? null;
}

export function renderFixturePrototype(root, scenario = "board", initialTaskId = null) {
  removeRouteListener?.();
  closeTaskDetail({ restoreFocus: false });
  const { shell, main, noticeHost } = createApplicationFrame();
  const live = document.createElement("p");
  live.className = "sr-only";
  live.setAttribute("aria-live", "polite");
  shell.append(live);
  const board = fixtureForScenario(scenario);
  let selectedDetail = null;
  let selectedOrigin = null;

  const updateNotices = (state = null) => {
    const taskNotices = effectiveTaskContributions(board, selectedDetail);
    renderNotices(noticeHost, groupNotices(taskNotices, lifecycleNotice(state)));
  };
  const closePanel = ({ fromHistory = false } = {}) => {
    const origin = selectedOrigin;
    selectedDetail = null;
    selectedOrigin = null;
    closeTaskDetail({ restoreFocus: true });
    updateNotices();
    if (!fromHistory) writePrototypeRoute(BOARD_ROUTE, "replace", {});
    origin?.removeAttribute("aria-current");
  };
  const requestPanelClose = () => {
    if (history.state?.dishPanelEntry) {
      try { history.back(); return; } catch { /* use local fallback */ }
    }
    closePanel();
  };
  const openCard = (card, origin, { historyMode = "push" } = {}) => {
    selectedOrigin?.removeAttribute("aria-current");
    selectedDetail = detailForCard(card);
    selectedOrigin = origin;
    origin?.setAttribute("aria-current", "true");
    const fallback = origin?.closest(".board-column")?.querySelector(".board-column__title") ?? main;
    openTaskDetail(selectedDetail, origin, { onRequestClose: requestPanelClose, focusFallback: fallback });
    updateNotices();
    if (historyMode !== "none") {
      const state = { dishPanelEntry: historyMode === "push" };
      writePrototypeRoute(taskRoute(card.id), historyMode, state);
    } else {
      document.body.dataset.prototypeRoute = taskRoute(card.id);
    }
  };
  const options = {
    attentionLabels,
    onSelect: (card, origin) => openCard(card, origin, { historyMode: selectedDetail ? "replace" : "push" }),
    announce: (message) => { live.textContent = message; },
    onLoadMore: (section, region) => {
      const updated = loadFixtureContinuation(section, region, options);
      const index = board.sections.findIndex((item) => item.id === section.id);
      board.sections[index] = updated;
      updateNotices();
    },
  };

  if (scenario === "loading") renderLoadingState(main);
  else if (scenario === "initial-error") {
    renderInitialErrorState(main, () => renderFixturePrototype(root, "board"));
    updateNotices(lifecycleFixtures.initialError);
  } else {
    renderBoard(main, board, options);
    updateNotices(scenario === "last-safe" ? lifecycleFixtures.lastSafe : null);
  }
  root.replaceChildren(shell);
  root.dataset.shellState = "fixture-board";
  root.dataset.fixtureScenario = scenario;

  const openRoutedTask = (taskId) => {
    const card = findCard(board, taskId);
    const origin = card ? main.querySelector(`[data-task-id="${card.id}"]`) : null;
    if (card && origin) openCard(card, origin, { historyMode: "none" });
    else closePanel({ fromHistory: true });
  };
  const popstate = () => {
    const taskId = parseTaskRoute(location.pathname);
    if (taskId) openRoutedTask(taskId);
    else closePanel({ fromHistory: true });
  };
  window.addEventListener("popstate", popstate);
  removeRouteListener = () => window.removeEventListener("popstate", popstate);
  if (initialTaskId && scenario === "board") openRoutedTask(initialTaskId);
  else document.body.dataset.prototypeRoute = BOARD_ROUTE;
}
