export const RECONCILIATION_FEATURE_STATUS = "delivery-stage-5";

export function refreshRetryDelayMs(failureCount, intervalMs, randomValue = Math.random()) {
  if (!Number.isInteger(failureCount) || failureCount < 1) throw new Error("failure count must be positive");
  if (!Number.isInteger(intervalMs) || intervalMs < 1000 || intervalMs > 30000) throw new Error("refresh interval is invalid");
  const boundedRandom = Math.min(1, Math.max(0, Number(randomValue) || 0));
  const base = Math.min(intervalMs, 1000 * (2 ** Math.min(failureCount - 1, 8)));
  return Math.max(250, Math.floor(base * (0.75 + (0.25 * boundedRandom))));
}

export function reconcileBoard(previous, incoming) {
  if (!previous) return incoming;
  const previousBySection = new Map(previous.sections.map((section) => [section.id, section]));
  const incomingFirstPageIds = new Set(incoming.sections.flatMap((section) => section.cards.map((card) => card.id)));
  const retainedIds = new Set(incomingFirstPageIds);
  const sections = incoming.sections.map((section) => {
    const prior = previousBySection.get(section.id);
    if (!prior || prior.continuityId !== section.continuityId || prior.resetPending) return section;
    const firstPageCount = prior.firstPageCount ?? Math.min(prior.cards.length, previous.pageSize);
    const retained = prior.cards.slice(firstPageCount).filter((card) => {
      if (retainedIds.has(card.id)) return false;
      retainedIds.add(card.id);
      return true;
    });
    return {
      ...section,
      cards: [...section.cards, ...retained],
      nextCursor: prior.nextCursor,
      hasMore: prior.nextCursor !== null,
      firstPageCount: section.cards.length,
    };
  });
  return { ...incoming, sections };
}

export function resetSectionContinuation(board, sectionId, { blockLoadMore = false } = {}) {
  return {
    ...board,
    sections: board.sections.map((section) => {
      if (section.id !== sectionId) return section;
      const firstPageCount = section.firstPageCount ?? Math.min(section.cards.length, board.pageSize);
      return {
        ...section,
        cards: section.cards.slice(0, firstPageCount),
        nextCursor: null,
        hasMore: false,
        resetPending: true,
        loadMoreBlocked: blockLoadMore,
      };
    }),
  };
}

export function blockRepeatedInvalidCursor(board, sectionId, rejectedCursor) {
  return {
    ...board,
    sections: board.sections.map((section) => (
      section.id === sectionId && section.nextCursor === rejectedCursor
        ? { ...section, loadMoreBlocked: true }
        : section
    )),
  };
}

export function captureBoardViewState(host, board) {
  const scroller = host.querySelector?.(".board-scroller");
  const vertical = new Map();
  for (const section of board?.sections ?? []) {
    const list = host.querySelector?.(`[data-card-list="${CSS.escape(section.id)}"]`);
    if (list) vertical.set(section.id, { continuityId: section.continuityId, scrollTop: list.scrollTop });
  }
  const focused = document.activeElement?.closest?.(".task-card[data-task-id]");
  return {
    horizontal: scroller?.scrollLeft ?? 0,
    vertical,
    focusedTaskId: focused?.dataset.taskId ?? null,
  };
}

export function restoreBoardViewState(host, board, state) {
  if (!state) return;
  const scroller = host.querySelector?.(".board-scroller");
  if (scroller) scroller.scrollLeft = state.horizontal;
  for (const section of board?.sections ?? []) {
    const saved = state.vertical.get(section.id);
    if (!saved || saved.continuityId !== section.continuityId) continue;
    const list = host.querySelector?.(`[data-card-list="${CSS.escape(section.id)}"]`);
    if (list) list.scrollTop = saved.scrollTop;
  }
  if (!state.focusedTaskId) return;
  const card = host.querySelector?.(`.task-card[data-task-id="${CSS.escape(state.focusedTaskId)}"]`);
  if (!card) return;
  card.focus({ preventScroll: true });
  card.scrollIntoView?.({ block: "nearest", inline: "nearest" });
}
