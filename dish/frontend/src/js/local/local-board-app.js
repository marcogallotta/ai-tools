import { FrontendApiError, FrontendContractMismatch, FrontendHttpClient } from "../api/http-transport.js";
import { appendSectionPage, BoardContractMismatch, mapBoardResponse, mapSectionPageResponse } from "../features/board/api-board-model.js";
import { renderBoard } from "../features/board/board.js";
import { effectiveTaskContributions, groupNotices } from "../features/notices/notice-model.js";
import { noticeRegistry } from "../features/notices/notice-registry.js";
import { renderNotices } from "../features/notices/notices.js";
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
    this.inFlightBySection = new Map();
  }

  beginBootstrap() {
    this.bootstrapSequence += 1;
    this.inFlightBySection.clear();
    return this.bootstrapSequence;
  }

  isCurrentBootstrap(generation) {
    return generation === this.bootstrapSequence;
  }

  acceptBootstrap(generation) {
    if (!this.isCurrentBootstrap(generation)) return false;
    this.acceptedBoardGeneration = generation;
    this.inFlightBySection.clear();
    return true;
  }

  beginContinuation(section) {
    if (
      !section?.nextCursor
      || this.acceptedBoardGeneration === 0
      || this.acceptedBoardGeneration !== this.bootstrapSequence
    ) return null;
    if (this.inFlightBySection.has(section.id)) return null;
    const request = Object.freeze({
      requestId: ++this.continuationSequence,
      boardGeneration: this.acceptedBoardGeneration,
      sectionId: section.id,
      continuityId: section.continuityId,
      cursor: section.nextCursor,
    });
    this.inFlightBySection.set(section.id, request);
    return request;
  }

  currentContinuationSection(request, board) {
    if (
      !request
      || this.bootstrapSequence !== request.boardGeneration
      || this.acceptedBoardGeneration !== request.boardGeneration
    ) return null;
    if (this.inFlightBySection.get(request.sectionId) !== request) return null;
    const section = board?.sections.find((item) => item.id === request.sectionId);
    if (!section) return null;
    if (section.continuityId !== request.continuityId) return null;
    if (section.nextCursor !== request.cursor) return null;
    return section;
  }

  finishContinuation(request) {
    if (request && this.inFlightBySection.get(request.sectionId) === request) {
      this.inFlightBySection.delete(request.sectionId);
    }
  }
}

function localErrorMessage(error) {
  if (error instanceof FrontendApiError && error.code === "client_update_required") {
    return "The local frontend and server contracts differ. Rebuild and reload the frontend.";
  }
  if (error instanceof FrontendContractMismatch || error instanceof BoardContractMismatch) {
    return "The local PostgreSQL response did not match the frontend contract. Rebuild and reload before using it.";
  }
  if (error instanceof FrontendApiError) return error.message;
  return "The local PostgreSQL board is unavailable.";
}

function renderDetailPlaceholder(card, origin) {
  document.querySelector("[data-local-detail-placeholder]")?.remove();
  const panel = document.createElement("aside");
  panel.className = "task-detail";
  panel.dataset.localDetailPlaceholder = "true";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-labelledby", "local-detail-title");
  const header = document.createElement("header");
  header.className = "task-detail__header";
  const heading = document.createElement("h2");
  heading.id = "local-detail-title";
  heading.textContent = card.title;
  const close = document.createElement("button");
  close.className = "button button--secondary task-detail__close";
  close.type = "button";
  close.textContent = "Close";
  header.append(heading, close);
  const body = document.createElement("div");
  body.className = "task-detail__body";
  const message = document.createElement("p");
  message.textContent = "Task detail is not available in the Stage 3 local PostgreSQL board.";
  body.append(message);
  panel.append(header, body);
  document.body.append(panel);
  document.body.dataset.detailOpen = "true";
  const dismiss = () => {
    panel.remove();
    delete document.body.dataset.detailOpen;
    origin?.focus({ preventScroll: true });
  };
  close.addEventListener("click", dismiss, { once: true });
  close.focus();
}

export async function renderLocalPostgresqlBoard(root, { fetchImpl = globalThis.fetch } = {}) {
  const client = new FrontendHttpClient({ fetchImpl });
  const { shell, main, noticeHost } = createApplicationFrame({
    prototypeLabel: "LOCAL POSTGRESQL — NON-AUTHORITATIVE",
  });
  const live = document.createElement("p");
  live.className = "sr-only";
  live.setAttribute("aria-live", "polite");
  shell.append(live);
  root.replaceChildren(shell);
  root.dataset.shellState = "local-postgresql-loading";
  let board = null;
  const requestState = new LocalBoardRequestState();

  const renderCurrent = () => {
    const options = {
      attentionLabels: localAttentionLabels,
      announce: (message) => { live.textContent = message; },
      onSelect: (card, origin) => renderDetailPlaceholder(card, origin),
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
          if (error instanceof FrontendApiError && ["cursor_invalid", "cursor_stale", "request_invalid"].includes(error.code)) {
            await loadBoard();
            live.textContent = "The board was refreshed because the continuation cursor changed.";
            return;
          }
          live.textContent = localErrorMessage(error);
        } finally {
          requestState.finishContinuation(request);
        }
      },
    };
    renderBoard(main, board, options);
    renderNotices(noticeHost, groupNotices(effectiveTaskContributions(board)));
    root.dataset.shellState = "local-postgresql-board";
  };

  const loadBoard = async () => {
    const generation = requestState.beginBootstrap();
    renderLoadingState(main);
    try {
      const nextBoard = mapBoardResponse(await client.board());
      if (!requestState.acceptBootstrap(generation)) return;
      board = nextBoard;
      renderCurrent();
    } catch (error) {
      if (!requestState.isCurrentBootstrap(generation)) return;
      renderNotices(noticeHost, []);
      renderInitialErrorState(main, loadBoard, { description: localErrorMessage(error) });
      root.dataset.shellState = "local-postgresql-error";
    }
  };

  await loadBoard();
}
