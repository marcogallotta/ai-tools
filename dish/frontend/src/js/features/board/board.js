import { installBoardKeyboard } from "../accessibility/board-keyboard.js";
import { createTaskCard } from "../cards/card.js";
import { appendContinuation, sectionHeading } from "./board-model.js";

function createSection(section, options) {
  const region = document.createElement("section");
  region.className = "board-column";
  region.dataset.sectionId = section.id;
  region.setAttribute("aria-labelledby", `heading-${section.id}`);
  region.setAttribute("aria-busy", "false");

  const header = document.createElement("header");
  header.className = "board-column__header";
  const heading = document.createElement("h2");
  heading.id = `heading-${section.id}`;
  heading.className = "board-column__title";
  heading.textContent = sectionHeading(section);
  heading.tabIndex = -1;
  header.append(heading);

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
  if (section.hasMore || section.loadMoreBlocked) {
    const loadMore = document.createElement("button");
    loadMore.className = "button button--secondary board-column__load";
    loadMore.type = "button";
    loadMore.textContent = section.loadMoreBlocked ? "Reload required" : "Load more";
    loadMore.disabled = Boolean(section.loadMoreBlocked);
    if (!section.loadMoreBlocked) loadMore.addEventListener("click", async () => {
      if (region.getAttribute("aria-busy") === "true") return;
      region.setAttribute("aria-busy", "true");
      loadMore.disabled = true;
      try {
        await options.onLoadMore(section, region);
      } finally {
        if (region.isConnected) {
          region.setAttribute("aria-busy", "false");
          loadMore.disabled = false;
        }
      }
    });
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

export function createActiveTitleSearch(host, {
  search,
  onSelect,
  onError,
} = {}) {
  let requestGeneration = 0;
  let stopped = false;

  const form = document.createElement("form");
  form.className = "board-search__form";
  form.setAttribute("role", "search");
  const label = document.createElement("label");
  label.className = "board-search__label";
  label.htmlFor = "active-dish-search";
  label.textContent = "Search active dishes";
  const controls = document.createElement("div");
  controls.className = "board-search__controls";
  const input = document.createElement("input");
  input.id = "active-dish-search";
  input.className = "board-search__input";
  input.type = "search";
  input.name = "q";
  input.maxLength = 160;
  input.autocomplete = "off";
  const submit = document.createElement("button");
  submit.className = "button button--secondary board-search__submit";
  submit.type = "submit";
  submit.textContent = "Search";
  controls.append(input, submit);
  const status = document.createElement("p");
  status.className = "board-search__status muted";
  status.setAttribute("aria-live", "polite");
  const resultsHost = document.createElement("div");
  resultsHost.className = "board-search__results";
  form.append(label, controls);
  host.append(form, status, resultsHost);

  const renderResults = ({ results, truncated }, query) => {
    resultsHost.replaceChildren();
    if (results.length === 0) {
      status.textContent = `No active dishes match “${query}”.`;
      return;
    }
    status.textContent = `${results.length} active ${results.length === 1 ? "dish" : "dishes"} found${truncated ? "; refine the title to see more" : ""}.`;
    const list = document.createElement("ul");
    list.className = "board-search__list";
    for (const result of results) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "board-search__result";
      const title = document.createElement("span");
      title.className = "board-search__result-title";
      title.textContent = result.title;
      const context = document.createElement("span");
      context.className = "board-search__result-context";
      context.textContent = `${result.projectLabel} · ${result.sectionLabel}`;
      button.append(title, context);
      button.addEventListener("click", () => onSelect(result, button));
      item.append(button);
      list.append(item);
    }
    resultsHost.append(list);
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (!query) {
      resultsHost.replaceChildren();
      status.textContent = "Enter part of a dish title.";
      return;
    }
    const request = ++requestGeneration;
    submit.disabled = true;
    input.setAttribute("aria-busy", "true");
    status.textContent = "Searching…";
    try {
      const result = await search(query);
      if (stopped || request !== requestGeneration) return;
      renderResults(result, query);
    } catch (error) {
      if (stopped || request !== requestGeneration) return;
      if (onError?.(error)) return;
      resultsHost.replaceChildren();
      status.textContent = "Search is temporarily unavailable. The board is still available.";
    } finally {
      if (!stopped && request === requestGeneration) {
        submit.disabled = false;
        input.removeAttribute("aria-busy");
      }
    }
  });

  return Object.freeze({
    stop() {
      stopped = true;
      requestGeneration += 1;
    },
  });
}

