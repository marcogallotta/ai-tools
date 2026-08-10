import { activeRefreshIntervalMs } from "../config.js";
import { FrontendApiError, FrontendHttpClient } from "../api/http-transport.js";
import { mapAdminResponse } from "../features/admin/admin-model.js";
import { renderBoardAdminSummary } from "../features/admin/admin.js";
import { appendSectionPage, mapBoardResponse, mapSectionPageResponse } from "../features/board/api-board-model.js";
import { renderBoard } from "../features/board/board.js";
import { mapTaskDetailResponse } from "../features/detail/api-detail-model.js";
import { closeTaskDetail, openTaskDetail } from "../features/detail/task-detail.js";
import { effectiveTaskContributions, groupNotices } from "../features/notices/notice-model.js";
import { noticeRegistry } from "../features/notices/notice-registry.js";
import { renderNotices } from "../features/notices/notices.js";
import {
  blockRepeatedInvalidCursor,
  captureBoardViewState,
  reconcileBoard,
  refreshRetryDelayMs,
  resetSectionContinuation,
  restoreBoardViewState,
} from "../features/refresh/reconciliation.js";
import { BOARD_ROUTE, parsePostgresTaskRoute, postgresSourceSuffix, postgresTaskRoute, writePostgresRoute } from "../features/routing/routes.js";
import { renderInitialErrorState, renderLoadingState } from "../features/refresh/state-shells.js";
import { createApplicationFrame } from "../shell/application-shell.js";
import { LocalBoardRequestState } from "../features/refresh/request-state.js";
import { refreshFailureNotice } from "../features/refresh/failures.js";

const localAttentionLabels = Object.freeze(Object.fromEntries(
  Object.entries(noticeRegistry).map(([code, presentation]) => [code, presentation.label]),
));

export { LocalBoardRequestState } from "../features/refresh/request-state.js";

export async function renderLocalPostgresqlBoard(root, {
  fetchImpl = globalThis.fetch,
  initialTaskId = null,
  environmentLabel = "LOCAL POSTGRESQL — NON-AUTHORITATIVE",
  onAuthenticationLost = () => false,
  refreshIntervalMs = activeRefreshIntervalMs(),
  setTimer = globalThis.setTimeout.bind(globalThis),
  clearTimer = globalThis.clearTimeout.bind(globalThis),
  random = Math.random,
} = {}) {
  const client = new FrontendHttpClient({ fetchImpl });
  const { shell, main, noticeHost, utilityHost } = createApplicationFrame({ environmentLabel, navigationSuffix: postgresSourceSuffix() });
  const live = document.createElement("p");
  live.className = "sr-only"; live.setAttribute("aria-live", "polite"); shell.append(live);
  root.replaceChildren(shell); root.dataset.shellState = "local-postgresql-loading";
  let board = null; let selectedDetail = null; let selectedOrigin = null;
  let refreshTimer = null; let refreshFailures = 0; let boardRefreshPromise = null; let queuedRefresh = false;
  let stopped = false; let refreshSuspended = false; let lastNoticeSignature = null;
  const invalidRequestCursors = new Map(); const blockedInvalidRequestCursors = new Map(); const requestNotices = new Map();
  const requestState = new LocalBoardRequestState();

  const reloadPage = () => window.location.reload();
  const renderCurrentNotices = () => {
    const serverDetail = selectedDetail?.notices?.map((notice) => ({ ...notice })) ?? [];
    const notices = groupNotices(
      effectiveTaskContributions(board ?? { sections: [] }, selectedDetail),
      [...serverDetail, ...requestNotices.values()],
    );
    const signature = JSON.stringify(notices.map((notice) => [notice.code, notice.message, notice.action, notice.tasks]));
    if (signature === lastNoticeSignature) return;
    lastNoticeSignature = signature;
    renderNotices(noticeHost, notices, {
      onSelectTask: (taskId) => {
        const origin = document.querySelector(`.task-card[data-task-id="${CSS.escape(taskId)}"]`);
        void openDetail(taskId, origin, { navigation: selectedDetail ? "replace" : "push", fromBoard: !selectedDetail });
      },
      onRetry: () => { void requestBoardRefresh({ manual: true, forceAfterCurrent: true }); },
      onReload: reloadPage,
    });
  };
  const setRequestNotice = (scope, notice) => { requestNotices.set(scope, notice); renderCurrentNotices(); };
  const clearRequestNotice = (scope) => { if (requestNotices.delete(scope)) renderCurrentNotices(); };
  const normalizeBoardRoute = (mode = "replace") => writePostgresRoute(BOARD_ROUTE, mode, {});

  const refreshAdminSummary = async () => {
    try {
      renderBoardAdminSummary(utilityHost, mapAdminResponse(await client.admin()));
      const link = utilityHost.querySelector(".board-admin-summary__link");
      if (link) link.href = `/admin${postgresSourceSuffix()}`;
    } catch (error) {
      if (onAuthenticationLost(error)) stop();
      else utilityHost.replaceChildren();
    }
  };

  const closeDetailForRoute = ({ restoreFocus = true } = {}) => {
    requestState.cancelDetail(); selectedDetail = null;
    closeTaskDetail({ restoreFocus }); renderCurrentNotices();
  };
  const requestClose = () => {
    if (history.state?.dishLocalDetailFromBoard) { history.back(); return; }
    normalizeBoardRoute("replace"); closeDetailForRoute();
  };

  const showDetail = (detail, { refresh = false } = {}) => {
    selectedDetail = detail;
    selectedOrigin = document.querySelector(`.task-card[data-task-id="${CSS.escape(detail.id)}"]`) ?? selectedOrigin;
    openTaskDetail(detail, selectedOrigin, { onRequestClose: requestClose, focusFallback: main, refresh });
    renderCurrentNotices(); root.dataset.shellState = "local-postgresql-detail";
  };

  const refreshDetail = async ({ background = false } = {}) => {
    if (!selectedDetail) return true;
    const taskId = selectedDetail.id; const request = requestState.beginDetail(taskId);
    try {
      const detail = mapTaskDetailResponse(await client.taskDetail(taskId));
      if (!requestState.isCurrentDetail(request) || selectedDetail?.id !== taskId) return true;
      const path = postgresTaskRoute(detail.id, detail.title);
      if (window.location.pathname !== path) writePostgresRoute(path, "replace", history.state ?? {});
      clearRequestNotice("detail-refresh"); showDetail(detail, { refresh: true }); return true;
    } catch (error) {
      if (!requestState.isCurrentDetail(request) || selectedDetail?.id !== taskId) return true;
      if (onAuthenticationLost(error)) { stop(); return false; }
      if (error instanceof FrontendApiError && ["task_not_found", "task_ineligible"].includes(error.code)) {
        setRequestNotice("task-lifecycle", { code: error.code, message: error.message });
        normalizeBoardRoute("replace"); closeDetailForRoute({ restoreFocus: false });
        void requestBoardRefresh({ background: true, forceAfterCurrent: true }); return false;
      }
      const notice = refreshFailureNotice(error);
      setRequestNotice("detail-refresh", notice);
      if (notice.action === "reload") refreshSuspended = true;
      if (!background) live.textContent = notice.message;
      return false;
    }
  };

  const openDetail = async (taskId, origin = null, { navigation = "none", fromBoard = false } = {}) => {
    const request = requestState.beginDetail(taskId);
    try {
      const detail = mapTaskDetailResponse(await client.taskDetail(taskId));
      if (!requestState.isCurrentDetail(request)) return;
      const path = postgresTaskRoute(detail.id, detail.title); const current = parsePostgresTaskRoute(window.location.pathname);
      if (navigation === "push") writePostgresRoute(path, "push", { dishLocalDetailFromBoard: fromBoard });
      else if (!current || current.taskId !== detail.id || window.location.pathname !== path) writePostgresRoute(path, "replace", history.state ?? {});
      clearRequestNotice("detail-refresh");
      selectedOrigin = origin; showDetail(detail);
    } catch (error) {
      if (!requestState.isCurrentDetail(request)) return;
      if (onAuthenticationLost(error)) { stop(); return; }
      if (error instanceof FrontendApiError && ["task_not_found", "task_ineligible"].includes(error.code)) {
        setRequestNotice("task-lifecycle", { code: error.code, message: error.message });
        normalizeBoardRoute("replace"); closeDetailForRoute({ restoreFocus: false });
        void requestBoardRefresh({ background: true, forceAfterCurrent: true }); return;
      }
      const notice = refreshFailureNotice(error); setRequestNotice("detail-refresh", notice);
      if (notice.action === "reload") refreshSuspended = true;
      live.textContent = notice.message;
    }
  };

  const renderCurrent = ({ viewState = null } = {}) => {
    renderBoard(main, board, {
      attentionLabels: localAttentionLabels,
      announce: (message) => { live.textContent = message; },
      onSelect: (card, origin) => void openDetail(card.id, origin, { navigation: selectedDetail ? "replace" : "push", fromBoard: !selectedDetail }),
      onLoadMore: async (section) => {
        const request = requestState.beginContinuation(section); if (!request) return;
        const viewStateBefore = captureBoardViewState(main, board);
        try {
          const response = await client.sectionTasks(request.sectionId, request.cursor);
          const currentSection = requestState.currentContinuationSection(request, board); if (!currentSection) return;
          const page = mapSectionPageResponse(response, currentSection);
          board = appendSectionPage(board, currentSection.id, page); invalidRequestCursors.delete(currentSection.id); blockedInvalidRequestCursors.delete(currentSection.id);
          clearRequestNotice(`load-more:${currentSection.id}`); renderCurrent({ viewState: viewStateBefore });
          live.textContent = `${page.cards.length} tasks added to ${currentSection.label}`;
        } catch (error) {
          const currentSection = requestState.currentContinuationSection(request, board); if (!currentSection) return;
          if (onAuthenticationLost(error)) { stop(); return; }
          if (error instanceof FrontendApiError && ["cursor_invalid", "cursor_stale", "request_invalid"].includes(error.code)) {
            const repeatedInvalid = invalidRequestCursors.get(request.sectionId) === request.cursor;
            board = resetSectionContinuation(board, request.sectionId, { blockLoadMore: repeatedInvalid });
            renderCurrent({ viewState: viewStateBefore });
            if (repeatedInvalid) {
              blockedInvalidRequestCursors.set(request.sectionId, request.cursor); setRequestNotice(`load-more:${request.sectionId}`, {
                code: "request_invalid", message: "This continuation request is still invalid after refresh. Reload the page before loading more.", action: "reload",
              });
            } else {
              invalidRequestCursors.set(request.sectionId, request.cursor); blockedInvalidRequestCursors.delete(request.sectionId);
              void requestBoardRefresh({ background: true, forceAfterCurrent: true });
            }
            return;
          }
          setRequestNotice(`load-more:${request.sectionId}`, refreshFailureNotice(error));
        } finally { requestState.finishContinuation(request); }
      },
    });
    restoreBoardViewState(main, board, viewState); renderCurrentNotices();
    root.dataset.shellState = selectedDetail ? "local-postgresql-detail" : "local-postgresql-board";
  };

  const clearRefreshTimer = () => { if (refreshTimer !== null) clearTimer(refreshTimer); refreshTimer = null; };
  const scheduleRefresh = ({ failed = false } = {}) => {
    clearRefreshTimer(); if (stopped || refreshSuspended) return;
    if (failed) refreshFailures += 1; else refreshFailures = 0;
    const delay = failed ? refreshRetryDelayMs(refreshFailures, refreshIntervalMs, random()) : refreshIntervalMs;
    refreshTimer = setTimer(() => {
      refreshTimer = null;
      if (document.visibilityState === "visible") void requestBoardRefresh({ background: true });
      else scheduleRefresh();
    }, delay);
  };

  const performBoardRefresh = async ({ background = false, manual = false } = {}) => {
    const previous = board; const viewState = previous ? captureBoardViewState(main, previous) : null;
    const generation = requestState.beginBootstrap();
    if (!previous) renderLoadingState(main); else main.setAttribute("aria-busy", "true");
    try {
      const incoming = mapBoardResponse(await client.board());
      if (!requestState.acceptBootstrap(generation)) return true;
      board = reconcileBoard(previous, incoming);
      for (const [sectionId, rejectedCursor] of invalidRequestCursors) { const fresh = incoming.sections.find((section) => section.id === sectionId);
        if (!fresh || fresh.nextCursor !== rejectedCursor) { invalidRequestCursors.delete(sectionId); blockedInvalidRequestCursors.delete(sectionId); clearRequestNotice(`load-more:${sectionId}`); if (fresh) board = { ...board, sections: board.sections.map((section) => section.id === sectionId ? fresh : section) }; }
        else if (blockedInvalidRequestCursors.get(sectionId) === rejectedCursor) board = blockRepeatedInvalidCursor(board, sectionId, rejectedCursor);
      }
      clearRequestNotice("board-refresh"); clearRequestNotice("initial-load");
      renderCurrent({ viewState });
      void refreshAdminSummary();
      const detailOkay = await refreshDetail({ background: true });
      if (manual) live.textContent = "Board refreshed.";
      scheduleRefresh({ failed: !detailOkay }); return detailOkay;
    } catch (error) {
      if (!requestState.isCurrentBootstrap(generation)) return false;
      if (onAuthenticationLost(error)) { stop(); return false; }
      const notice = refreshFailureNotice(error);
      if (notice.action === "reload") refreshSuspended = true;
      if (previous) {
        board = previous; main.setAttribute("aria-busy", "false"); setRequestNotice("board-refresh", notice);
      } else {
        setRequestNotice("initial-load", { ...notice, code: notice.code === "service_unavailable" ? "initial_load_failed" : notice.code });
        renderInitialErrorState(main, () => void requestBoardRefresh({ manual: true }), { description: notice.message });
        root.dataset.shellState = "local-postgresql-error";
      }
      scheduleRefresh({ failed: true }); return false;
    }
  };

  const requestBoardRefresh = ({ background = false, manual = false, forceAfterCurrent = false } = {}) => {
    if (stopped || refreshSuspended) return Promise.resolve(false);
    if (manual) { clearRefreshTimer(); refreshFailures = 0; }
    if (boardRefreshPromise) {
      if (forceAfterCurrent) queuedRefresh = true;
      return boardRefreshPromise;
    }
    boardRefreshPromise = performBoardRefresh({ background, manual }).finally(() => {
      boardRefreshPromise = null;
      if (queuedRefresh && !stopped && !refreshSuspended) {
        queuedRefresh = false; void requestBoardRefresh({ background: true });
      }
    });
    return boardRefreshPromise;
  };

  const onVisibility = () => {
    if (document.visibilityState !== "visible") { clearRefreshTimer(); return; }
    void requestBoardRefresh({ background: true, forceAfterCurrent: true });
  };
  const popstate = () => {
    const route = parsePostgresTaskRoute(window.location.pathname);
    if (route) {
      const origin = document.querySelector(`.task-card[data-task-id="${CSS.escape(route.taskId)}"]`);
      void openDetail(route.taskId, origin, { navigation: "none" });
    } else { closeDetailForRoute(); root.dataset.shellState = "local-postgresql-board"; }
  };
  function stop() {
    if (stopped) return; stopped = true; clearRefreshTimer(); requestState.cancelAll();
    document.removeEventListener("visibilitychange", onVisibility); window.removeEventListener("popstate", popstate);
  }
  document.addEventListener("visibilitychange", onVisibility); window.addEventListener("popstate", popstate);

  await requestBoardRefresh();
  const routed = initialTaskId ?? parsePostgresTaskRoute(window.location.pathname)?.taskId;
  if (routed && !stopped) {
    const origin = document.querySelector(`.task-card[data-task-id="${CSS.escape(routed)}"]`);
    await openDetail(routed, origin, { navigation: "none" });
  } else if (!stopped && window.location.pathname !== BOARD_ROUTE) normalizeBoardRoute("replace");
  return Object.freeze({ stop, refresh: () => requestBoardRefresh({ manual: true, forceAfterCurrent: true }) });
}
