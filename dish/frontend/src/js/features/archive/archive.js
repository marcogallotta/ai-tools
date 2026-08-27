function timeText(value) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

export function renderArchive(host, archive) {
  host.replaceChildren();
  host.className = "admin-region";
  host.setAttribute("aria-label", "Archived dishes");
  host.setAttribute("aria-busy", "false");
  const hero = document.createElement("section"); hero.className = "admin-hero";
  const eyebrow = document.createElement("p"); eyebrow.className = "eyebrow"; eyebrow.textContent = "PostgreSQL archive";
  const heading = document.createElement("h1"); heading.textContent = `${archive.dishes.length} archived ${archive.dishes.length === 1 ? "dish" : "dishes"}`;
  hero.append(eyebrow, heading); host.append(hero);
  const section = document.createElement("section"); section.className = "admin-group";
  if (!archive.dishes.length) {
    const empty = document.createElement("p"); empty.className = "admin-empty"; empty.textContent = "No dishes have been archived."; section.append(empty);
  } else {
    const list = document.createElement("div"); list.className = "admin-dish-list";
    for (const dish of archive.dishes) {
      const article = document.createElement("article"); article.className = "admin-dish admin-dish--archived";
      const title = document.createElement("h3"); title.textContent = dish.title;
      const time = document.createElement("time"); time.className = "admin-dish__time"; time.dateTime = dish.archivedAt; time.textContent = `Archived ${timeText(dish.archivedAt)}`;
      article.append(title, time); list.append(article);
    }
    section.append(list);
  }
  if (archive.truncated) { const warning = document.createElement("p"); warning.className = "admin-empty"; warning.textContent = "Only the newest 5,000 archived dishes are shown."; section.append(warning); }
  host.append(section);
}
