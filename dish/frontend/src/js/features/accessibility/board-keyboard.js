function cardsInColumn(column) {
  return [...column.querySelectorAll(".task-card")];
}

const keyboardHosts = new WeakSet();

function focusColumnTarget(column) {
  const card = column.querySelector(".task-card");
  const target = card ?? column.querySelector(".board-column__title");
  target?.focus({ preventScroll: false });
}

function adjacentColumn(column, direction) {
  const columns = [...column.parentElement.querySelectorAll(".board-column")];
  const index = columns.indexOf(column);
  return columns[index + direction] ?? null;
}

function handleCardKey(event, card) {
  const column = card.closest(".board-column");
  if (!column) return false;
  const cards = cardsInColumn(column);
  const index = cards.indexOf(card);
  if (event.key === "ArrowDown" && cards[index + 1]) {
    cards[index + 1].focus();
    return true;
  }
  if (event.key === "ArrowUp" && cards[index - 1]) {
    cards[index - 1].focus();
    return true;
  }
  if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const target = adjacentColumn(column, direction);
    if (target) focusColumnTarget(target);
    return Boolean(target);
  }
  return false;
}

export function boardScrollBehavior(matchMedia = globalThis.matchMedia) {
  if (typeof matchMedia !== "function") return "smooth";
  return matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

function handleBoardKeydown(event) {
  const host = event.currentTarget;
  const card = event.target.closest?.(".task-card");
  if (card && handleCardKey(event, card)) {
    event.preventDefault();
    return;
  }
  if (event.target !== host) return;
  const scroller = host.querySelector(".board-scroller");
  if (!scroller) return;
  const distance = event.key === "ArrowRight" ? 320 : event.key === "ArrowLeft" ? -320 : 0;
  if (distance) {
    scroller.scrollBy({ left: distance, behavior: boardScrollBehavior() });
    event.preventDefault();
  }
}

export function installBoardKeyboard(host) {
  if (keyboardHosts.has(host)) return;
  keyboardHosts.add(host);
  host.addEventListener("keydown", handleBoardKeydown);
}
