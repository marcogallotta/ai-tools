function timeText(value) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

export function renderArchive(host, archive) {
  host.replaceChildren();
  host.className = "admin-region";
  host.setAttribute("aria-label", "Archived dishes");
  host.setAttribute("aria-busy", "false");

  const hero = document.createElement("section");
  hero.className = "admin-hero";
  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = "PostgreSQL archive";
  const heading = document.createElement("h1");
  heading.textContent = `${archive.dishes.length} archived ${archive.dishes.length === 1 ? "dish" : "dishes"}`;
  const description = document.createElement("p");
  description.className = "muted";
  description.textContent = "Archived dishes are kept out of Cooking and retained here from PostgreSQL archive state.";
  hero.append(eyebrow, heading, description);
  host.append(hero);

  const section = document.createElement("section");
  section.className = "admin-group";
  section.setAttribute("aria-labelledby", "archive-list-title");
  const sectionHeading = document.createElement("div");
  sectionHeading.className = "admin-group__heading";
  const title = document.createElement("h2");
  title.id = "archive-list-title";
  title.textContent = "Archived";
  const generated = document.createElement("p");
  generated.className = "muted";
  generated.textContent = `Current as of ${timeText(archive.generatedAt)}`;
  sectionHeading.append(title, generated);
  section.append(sectionHeading);

  if (!archive.dishes.length) {
    const empty = document.createElement("p");
    empty.className = "admin-empty";
    empty.textContent = "No dishes have been archived.";
    section.append(empty);
  } else {
    const list = document.createElement("div");
    list.className = "admin-dish-list";
    for (const dish of archive.dishes) {
      const article = document.createElement("article");
      article.className = "admin-dish admin-dish--archived";
      const header = document.createElement("header");
      header.className = "admin-dish__header";
      const identity = document.createElement("div");
      const itemTitle = document.createElement("h3");
      itemTitle.textContent = dish.title;
      identity.append(itemTitle);
      const archivedAt = document.createElement("time");
      archivedAt.className = "admin-dish__time";
      archivedAt.dateTime = dish.archivedAt;
      archivedAt.textContent = `Archived ${timeText(dish.archivedAt)}`;
      header.append(identity, archivedAt);
      article.append(header);
      list.append(article);
    }
    section.append(list);
  }
  if (archive.truncated) {
    const warning = document.createElement("p");
    warning.className = "admin-empty";
    warning.setAttribute("role", "status");
    warning.textContent = "Only the newest 5,000 archived dishes are shown.";
    section.append(warning);
  }
  host.append(section);
}
