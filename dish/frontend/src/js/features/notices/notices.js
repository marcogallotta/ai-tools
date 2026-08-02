import { noticeHeading } from "./notice-model.js";

export function renderNotices(host, notices) {
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
    banner.append(text);
    host.append(banner);
  }
}
