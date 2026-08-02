export function renderLoadingState(host) {
  host.replaceChildren();
  host.className = "board-region state-shell";
  host.setAttribute("aria-label", "Dish task board");
  host.setAttribute("aria-busy", "true");
  const status = document.createElement("p");
  status.className = "state-shell__message";
  status.setAttribute("role", "status");
  status.textContent = "Loading the board…";
  const skeleton = document.createElement("div");
  skeleton.className = "board-skeleton";
  for (let index = 0; index < 3; index += 1) {
    const column = document.createElement("div");
    column.className = "board-skeleton__column";
    column.setAttribute("aria-hidden", "true");
    skeleton.append(column);
  }
  host.append(status, skeleton);
}

export function renderInitialErrorState(host, onRetry) {
  host.replaceChildren();
  host.className = "board-region state-shell";
  host.setAttribute("aria-label", "Dish task board");
  host.setAttribute("aria-busy", "false");
  const wrapper = document.createElement("section");
  wrapper.className = "initial-error-shell";
  const heading = document.createElement("h2");
  heading.textContent = "Board not loaded";
  const description = document.createElement("p");
  description.textContent = "No usable board is available yet. Retry the fixture load.";
  const retry = document.createElement("button");
  retry.className = "button";
  retry.type = "button";
  retry.textContent = "Retry board load";
  retry.addEventListener("click", onRetry);
  wrapper.append(heading, description, retry);
  host.append(wrapper);
}
