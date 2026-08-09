import { noticeHeading } from "./notice-model.js";

export function renderNotices(host, notices, { onSelectTask = null } = {}) {
  host.replaceChildren();
  host.className = "notice-stack";
  host.setAttribute("aria-label", "Current notices");
  for (const notice of notices) {
    const banner = document.createElement("section");
    banner.className = `notice-banner notice-banner--${notice.severity}`;
    banner.dataset.noticeCode = notice.code;
    banner.setAttribute("role", notice.severity === "error" ? "alert" : "status");
    banner.setAttribute("aria-live", notice.severity === "error" ? "assertive" : "polite");

    const text = document.createElement("div");
    const heading = document.createElement("h2");
    heading.textContent = noticeHeading(notice);
    const detail = document.createElement("p");
    detail.textContent = notice.message ?? "Review the current factual state before continuing.";
    text.append(heading, detail);
    if (notice.tasks?.length && onSelectTask) {
      const tasks = document.createElement("div");
      tasks.className = "notice-banner__tasks";
      for (const task of notice.tasks) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "notice-banner__task";
        button.textContent = task.title;
        button.addEventListener("click", () => onSelectTask(task.id));
        tasks.append(button);
      }
      text.append(tasks);
    }
    banner.append(text);
    host.append(banner);
  }
}
