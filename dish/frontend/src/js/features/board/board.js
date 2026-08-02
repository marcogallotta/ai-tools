import { installBoardKeyboard } from "../accessibility/board-keyboard.js";
import { createTaskCard } from "../cards/card.js";
import { appendContinuation, loadedTaskText, sectionHeading } from "./board-model.js";

function createSection(section, options) {
  const region = document.createElement("section");
  region.className = "board-column";
  region.dataset.sectionId = section.id;
  region.setAttribute("aria-labelledby", `heading-${section.id}`);

  const header = document.createElement("header");
  header.className = "board-column__header";
  const heading = document.createElement("h2");
  heading.id = `heading-${section.id}`;
  heading.className = "board-column__title";
  heading.textContent = sectionHeading(section);
  heading.tabIndex = -1;
  const count = document.createElement("p");
  count.className = "board-column__count";
  count.textContent = loadedTaskText(section.cards.length, section.hasMore);
  header.append(heading, count);

  const list = document.createElement("div");
  list.className = "board-column__cards";
  list.dataset.cardList = section.id;
  if (section.cards.length === 0) {
    const empty = document.createElement("p");
    empty.className = "board-column__empty";
    empty.textContent = "No incomplete tasks";
    list.append(empty);
  } else {
    for (const card of section.cards) list.append(createTaskCard(card, options));
  }

  region.append(header, list);
  if (section.hasMore) {
    const loadMore = document.createElement("button");
    loadMore.className = "button button--secondary board-column__load";
    loadMore.type = "button";
    loadMore.textContent = "Load more";
    loadMore.addEventListener("click", () => options.onLoadMore(section, region));
    region.append(loadMore);
  }
  return region;
}

export function renderBoard(host, board, options) {
  host.replaceChildren();
  host.className = "board-region";
  host.setAttribute("aria-label", "Dish task board");
  host.setAttribute("aria-busy", "false");
  host.tabIndex = 0;

  if (board.sections.length === 0) {
    const empty = document.createElement("section");
    empty.className = "zero-board";
    empty.tabIndex = -1;
    const heading = document.createElement("h2");
    heading.textContent = "No active sections";
    const description = document.createElement("p");
    description.textContent = "The active logical section registry is empty.";
    empty.append(heading, description);
    host.append(empty);
    return;
  }

  const scroller = document.createElement("div");
  scroller.className = "board-scroller";
  for (const section of board.sections) scroller.append(createSection(section, options));
  host.append(scroller);
  installBoardKeyboard(host);
}

export function loadFixtureContinuation(section, region, options) {
  const updated = appendContinuation(section);
  const replacement = createSection(updated, options);
  region.replaceWith(replacement);
  const added = updated.cards.length - section.cards.length;
  options.announce(`${added} tasks added to ${sectionHeading(section)}`);
  return updated;
}
