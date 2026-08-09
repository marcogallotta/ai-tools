import { FrontendApiError, FrontendContractMismatch, FrontendHttpClient } from "../api/http-transport.js";
import { appendSectionPage, BoardContractMismatch, mapBoardResponse, mapSectionPageResponse } from "../features/board/api-board-model.js";
import { renderBoard } from "../features/board/board.js";
import { DetailContractMismatch, mapTaskDetailResponse } from "../features/detail/api-detail-model.js";
import { closeTaskDetail, openTaskDetail } from "../features/detail/task-detail.js";
import { effectiveTaskContributions, groupNotices } from "../features/notices/notice-model.js";
import { noticeRegistry } from "../features/notices/notice-registry.js";
import { renderNotices } from "../features/notices/notices.js";
import { BOARD_ROUTE, parsePostgresTaskRoute, postgresTaskRoute, writePostgresRoute } from "../features/routing/routes.js";
import { renderInitialErrorState, renderLoadingState } from "../features/refresh/state-shells.js";
import { createApplicationFrame } from "../shell/application-shell.js";

const localAttentionLabels = Object.freeze(Object.fromEntries(
  Object.entries(noticeRegistry).map(([code, presentation]) => [code, presentation.label]),
));

export class LocalBoardRequestState {
  constructor() {
    this.bootstrapSequence = 0;
    this.acceptedBoardGeneration = 0;
    this.continuationSequence = 0;
    this.detailSequence = 0;
    this.inFlightBySection = new Map();
  }
  beginBootstrap() { this.bootstrapSequence += 1; this.inFlightBySection.clear(); return this.bootstrapSequence; }
  isCurrentBootstrap(generation) { return generation === this.bootstrapSequence; }
  acceptBootstrap(generation) {
    if (!this.isCurrentBootstrap(generation)) return false;
    this.acceptedBoardGeneration = generation; this.inFlightBySection.clear(); return true;
  }
  beginContinuation(section) {
    if (!section?.nextCursor || this.acceptedBoardGeneration === 0 || this.acceptedBoardGeneration !== this.bootstrapSequence) return null;
    if (this.inFlightBySection.has(section.id)) return null;
    const request = Object.freeze({
      requestId: ++this.continuationSequence,
      boardGeneration: this.acceptedBoardGeneration,
      sectionId: section.id,
      continuityId: section.continuityId,
      cursor: section.nextCursor,
    });
    this.inFlightBySection.set(section.id, request); return request;
  }
  currentContinuationSection(request, board) {
    if (!request || this.bootstrapSequence !== request.boardGeneration || this.acceptedBoardGeneration !== request.boardGeneration) return null;
    if (this.inFlightBySection.get(request.sectionId) !== request) return null;
    const section = board?.sections.find((item) => item.id === request.sectionId);
    if (!section || section.continuityId !== request.continuityId || section.nextCursor !== request.cursor) return null;
    return section;
  }
  finishContinuation(request) {
    if (request && this.inFlightBySection.get(request.sectionId) === request) this.inFlightBySection.delete(request.sectionId);
  }
  beginDetail(taskId) { return Object.freeze({ sequence: ++this.detailSequence, taskId }); }
  isCurrentDetail(request) { return request?.sequence === this.detailSequence; }
  cancelDetail() { this.detailSequence += 1; }
}

function localErrorMessage(error) {
  if (error instanceof FrontendApiError && error.code === "client_update_required") return "The local frontend and server contracts differ. Rebuild and reload the frontend.";
  if (error instanceof FrontendContractMismatch || error instanceof BoardContractMismatch || error instanceof DetailContractMismatch) {
    return "The local PostgreSQL response did not match the frontend contract. Rebuild and reload before using it.";
  }
  if (error instanceof FrontendApiError) return error.message;
  return "The local PostgreSQL frontend is unavailable.";
}

export async function renderLocalPostgresqlBoard(
  root,
  {
    fetchImpl = globalThis.fetch,
    initialTaskId = null,
    prototypeLabel = "LOCAL POSTGRESQL — NON-AUTHORITATIVE",
    onAuthenticationLost = () => false,
  } = {},
) {
  const client = new FrontendHttpClient({ fetchImpl });
  const { shell, main, noticeHost } = createApplicationFrame({ prototypeLabel });
  const live = document.createElement("p");
  live.className = "sr-only"; live.setAttribute("aria-live", "polite"); shell.append(live);
  root.replaceChildren(shell); root.dataset.shellState = "local-postgresql-loading";
  let board = null;
  let selectedDetail = null;
  let selectedOrigin = null;
  const requestState = new LocalBoardRequestState();

  const renderCurrentNotices = () => {
    const lifecycle = selectedDetail?.notices?.map((notice) => ({ code: notice.code, taskId: notice.taskId, message: notice.message })) ?? [];
    renderNotices(
      noticeHost,
      groupNotices(effectiveTaskContributions(board, selectedDetail), lifecycle),
      {
        onSelectTask: (taskId) => {
          const origin = document.querySelector(`.task-card[data-task-id="${CSS.escape(taskId)}"]`);
          void openDetail(taskId, origin, { navigation: selectedDetail ? "replace" : "push", fromBoard: !selectedDetail });
        },
      },
    );
  };

  const normalizeBoardRoute = (mode = "replace") => writePostgresRoute(BOARD_ROUTE, mode, {});

  const closeDetailForRoute = ({ restoreFocus = true } = {}) => {
    requestState.cancelDetail(); selectedDetail = null;
    closeTaskDetail({ restoreFocus });
    renderCurrentNotices();
  };

  const requestClose = () => {
    if (history.state?.dishLocalDetailFromBoard) {
      history.back();
      return;
    }
    normalizeBoardRoute("replace");
    closeDetailForRoute();
  };

  const openDetail = async (taskId, origin = null, { navigation = "none", fromBoard = false } = {}) => {
    const request = requestState.beginDetail(taskId);
    try {
      const detail = mapTaskDetailResponse(await client.taskDetail(taskId));
      if (!requestState.isCurrentDetail(request)) return;
      const path = postgresTaskRoute(detail.id, detail.title);
      const current = parsePostgresTaskRoute(window.location.pathname);
      if (navigation === "push") writePostgresRoute(path, "push", { dishLocalDetailFromBoard: fromBoard });
      else if (!current || current.taskId !== detail.id || window.location.pathname !== path) {
        writePostgresRoute(path, "replace", history.state ?? {});
      }
      selectedDetail = detail; selectedOrigin = origin;
      openTaskDetail(detail, origin, { onRequestClose: requestClose, focusFallback: main });
      renderCurrentNotices();
      root.dataset.shellState = "local-postgresql-detail";
    } catch (error) {
      if (!requestState.isCurrentDetail(request)) return;
      if (onAuthenticationLost(error)) return;
      if (error instanceof FrontendApiError && ["task_not_found", "task_ineligible"].includes(error.code)) {
        normalizeBoardRoute("replace");
        closeDetailForRoute({ restoreFocus: false });
        live.textContent = error.code === "task_ineligible" ? "That task is no longer eligible for the board." : "That task could not be found.";
        return;
      }
      live.textContent = localErrorMessage(error);
    }
  };

  const renderCurrent = () => {
    renderBoard(main, board, {
      attentionLabels: localAttentionLabels,
      announce: (message) => { live.textContent = message; },
      onSelect: (card, origin) => {
        const switching = Boolean(selectedDetail);
        selectedOrigin = origin;
        void openDetail(card.id, origin, { navigation: switching ? "replace" : "push", fromBoard: !switching });
      },
      onLoadMore: async (section) => {
        const request = requestState.beginContinuation(section);
        if (!request) return;
        try {
          const response = await client.sectionTasks(request.sectionId, request.cursor);
          const currentSection = requestState.currentContinuationSection(request, board);
          if (!currentSection) return;
          const page = mapSectionPageResponse(response, currentSection);
          board = appendSectionPage(board, currentSection.id, page);
          renderCurrent();
          live.textContent = `${page.cards.length} tasks added to ${currentSection.label}`;
        } catch (error) {
          if (!requestState.currentContinuationSection(request, board)) return;
          if (onAuthenticationLost(error)) return;
          if (error instanceof FrontendApiError && ["cursor_invalid", "cursor_stale", "request_invalid"].includes(error.code)) {
            await loadBoard(); live.textContent = "The board was refreshed because the continuation cursor changed."; return;
          }
          live.textContent = localErrorMessage(error);
        } finally { requestState.finishContinuation(request); }
      },
    });
    renderCurrentNotices();
    root.dataset.shellState = selectedDetail ? "local-postgresql-detail" : "local-postgresql-board";
  };

  const loadBoard = async () => {
    const generation = requestState.beginBootstrap(); renderLoadingState(main);
    try {
      const nextBoard = mapBoardResponse(await client.board());
      if (!requestState.acceptBootstrap(generation)) return;
      board = nextBoard; renderCurrent();
    } catch (error) {
      if (!requestState.isCurrentBootstrap(generation)) return;
      if (onAuthenticationLost(error)) return;
      renderNotices(noticeHost, []); renderInitialErrorState(main, loadBoard, { description: localErrorMessage(error) });
      root.dataset.shellState = "local-postgresql-error";
    }
  };

  const popstate = () => {
    const route = parsePostgresTaskRoute(window.location.pathname);
    if (route) {
      const origin = document.querySelector(`.task-card[data-task-id="${CSS.escape(route.taskId)}"]`);
      void openDetail(route.taskId, origin, { navigation: "none" });
    } else {
      closeDetailForRoute();
      root.dataset.shellState = "local-postgresql-board";
    }
  };
  window.addEventListener("popstate", popstate);

  await loadBoard();
  const routed = initialTaskId ?? parsePostgresTaskRoute(window.location.pathname)?.taskId;
  if (routed) {
    const origin = document.querySelector(`.task-card[data-task-id="${CSS.escape(routed)}"]`);
    await openDetail(routed, origin, { navigation: "none" });
  } else if (window.location.pathname !== BOARD_ROUTE) {
    normalizeBoardRoute("replace");
  }
}
