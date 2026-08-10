import { postgresSourceSuffix, postgresTaskRoute } from "../routing/routes.js";
import { workflowText } from "./admin-model.js";

function timeText(value) {
  if (!value) return "No recent recorded activity";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function summaryCard(value, label, className = "") {
  const card = document.createElement("div");
  card.className = `admin-summary-card ${className}`.trim();
  const count = document.createElement("strong"); count.textContent = String(value);
  const text = document.createElement("span"); text.textContent = label;
  card.append(count, text); return card;
}

function attentionList(items) {
  const list = document.createElement("div"); list.className = "admin-attention-list";
  for (const item of items) {
    const row = document.createElement("div"); row.className = `admin-attention admin-attention--${item.code}`;
    const title = document.createElement("strong"); title.textContent = item.label;
    const message = document.createElement("p"); message.textContent = item.message;
    row.append(title, message); list.append(row);
  }
  return list;
}

function dishCard(dish) {
  const article = document.createElement("article"); article.className = `admin-dish admin-dish--${dish.bucket}`; article.id = `dish-${dish.id}`;
  const header = document.createElement("header"); header.className = "admin-dish__header";
  const identity = document.createElement("div");
  const section = document.createElement("p"); section.className = "eyebrow"; section.textContent = dish.sectionLabel;
  const title = document.createElement("h3");
  const link = document.createElement("a"); link.href = `${postgresTaskRoute(dish.id, dish.title)}${postgresSourceSuffix()}`; link.textContent = dish.title; title.append(link);
  const state = document.createElement("p"); state.className = "muted admin-dish__state"; state.textContent = workflowText(dish.status);
  identity.append(section, title, state);
  const activity = document.createElement("time"); activity.className = "admin-dish__time"; if (dish.lastActivityAt) activity.dateTime = dish.lastActivityAt; activity.textContent = timeText(dish.lastActivityAt);
  header.append(identity, activity); article.append(header, attentionList(dish.attention));
  const diagnostics = document.createElement("details"); diagnostics.className = "admin-diagnostics";
  const summary = document.createElement("summary"); summary.textContent = "Diagnostics";
  const pre = document.createElement("pre"); pre.textContent = JSON.stringify({ task_id: dish.id, attention_codes: dish.diagnostics.attentionCodes }, null, 2);
  diagnostics.append(summary, pre); article.append(diagnostics); return article;
}

function group(titleText, descriptionText, dishes, id) {
  const section = document.createElement("section"); section.className = "admin-group"; section.setAttribute("aria-labelledby", id);
  const heading = document.createElement("div"); heading.className = "admin-group__heading";
  const title = document.createElement("h2"); title.id = id; title.textContent = titleText;
  const description = document.createElement("p"); description.className = "muted"; description.textContent = descriptionText;
  heading.append(title, description); section.append(heading);
  if (!dishes.length) { const empty = document.createElement("p"); empty.className = "admin-empty"; empty.textContent = "None right now."; section.append(empty); }
  else { const list = document.createElement("div"); list.className = "admin-dish-list"; dishes.forEach((dish) => list.append(dishCard(dish))); section.append(list); }
  return section;
}

function journalEvent(event) {
  const article = document.createElement("article"); article.className = "journal-event";
  const when = document.createElement("time"); when.dateTime = event.occurredAt; when.textContent = timeText(event.occurredAt);
  const body = document.createElement("div");
  const title = document.createElement("p"); title.className = "journal-event__title";
  const link = document.createElement("a"); link.href = `${postgresTaskRoute(event.taskId, event.title)}${postgresSourceSuffix()}`; link.textContent = event.title;
  title.append(link);
  const summary = document.createElement("p"); summary.textContent = event.summary;
  const details = document.createElement("details"); details.className = "admin-diagnostics";
  const detailsSummary = document.createElement("summary"); detailsSummary.textContent = "Technical details";
  const pre = document.createElement("pre"); pre.textContent = JSON.stringify({ event_id: event.id, ...event.diagnostics }, null, 2);
  details.append(detailsSummary, pre); body.append(title, summary, details); article.append(when, body); return article;
}

export function renderAdmin(host, admin) {
  host.replaceChildren(); host.className = "admin-region"; host.setAttribute("aria-label", "Dish administration"); host.setAttribute("aria-busy", "false");
  const intro = document.createElement("section"); intro.className = "admin-hero";
  const eyebrow = document.createElement("p"); eyebrow.className = "eyebrow"; eyebrow.textContent = "Read-only administration";
  const heading = document.createElement("h1"); heading.textContent = admin.summary.needsYou ? `${admin.summary.needsYou} ${admin.summary.needsYou === 1 ? "dish needs" : "dishes need"} you` : "Nothing needs you right now";
  const description = document.createElement("p"); description.className = "muted"; description.textContent = "Operational state first. Workflow mechanics and identifiers stay in diagnostics.";
  const counts = document.createElement("div"); counts.className = "admin-summary-grid";
  counts.append(
    summaryCard(admin.summary.humanReview, "Human Review"),
    summaryCard(admin.summary.recovery, "Recovery"),
    summaryCard(admin.summary.research, "Needs research"),
    summaryCard(admin.summary.verification, "Needs verification"),
    summaryCard(admin.summary.systemActivity, "System handling it"),
  );
  intro.append(eyebrow, heading, description, counts); host.append(intro);
  host.append(group("Needs you", "Only states that require an operator decision or explicit recovery action.", admin.dishes.filter((dish) => dish.bucket === "needs_you"), "admin-needs-you"));
  host.append(group("Workflow queue", "Dishes waiting for ordinary Research or Verification work. These do not increase the Marco-only needs-you count.", admin.dishes.filter((dish) => dish.bucket === "workflow_queue"), "admin-workflow"));
  host.append(group("System handling it", "Operationally relevant states that are visible without increasing the main needs-you count.", admin.dishes.filter((dish) => dish.bucket === "system_activity"), "admin-system"));
  const journal = document.createElement("section"); journal.className = "admin-journal"; journal.setAttribute("aria-labelledby", "operator-journal-title");
  const journalHeading = document.createElement("div"); journalHeading.className = "admin-group__heading";
  const title = document.createElement("h2"); title.id = "operator-journal-title"; title.textContent = "Operator journal";
  const journalDescription = document.createElement("p"); journalDescription.className = "muted"; journalDescription.textContent = "Recent durable Dish events in plain language. Raw identifiers are expandable per event.";
  journalHeading.append(title, journalDescription); journal.append(journalHeading);
  if (!admin.journal.length) { const empty = document.createElement("p"); empty.className = "admin-empty"; empty.textContent = "No recent events for active dishes."; journal.append(empty); }
  else { const list = document.createElement("div"); list.className = "journal-list"; admin.journal.forEach((event) => list.append(journalEvent(event))); journal.append(list); }
  host.append(journal);
  const hash = new URLSearchParams(window.location.hash.slice(1)).get("dish");
  if (hash) document.getElementById(`dish-${hash}`)?.scrollIntoView({ block: "start" });
}

export function renderBoardAdminSummary(host, admin) {
  host.replaceChildren(); host.className = "board-admin-summary";
  if (!admin || admin.summary.needsYou === 0) { host.hidden = true; return; }
  host.hidden = false;
  const link = document.createElement("a"); link.className = "board-admin-summary__link"; link.href = `/admin${postgresSourceSuffix()}`;
  const strong = document.createElement("strong"); strong.textContent = admin.summary.needsYou ? `${admin.summary.needsYou} ${admin.summary.needsYou === 1 ? "dish needs" : "dishes need"} you` : "No dishes need you";
  const detail = document.createElement("span");
  const parts = [];
  if (admin.summary.humanReview) parts.push(`${admin.summary.humanReview} review`);
  if (admin.summary.recovery) parts.push(`${admin.summary.recovery} recovery`);
  detail.textContent = parts.join(" · "); link.append(strong, detail); host.append(link);
}
