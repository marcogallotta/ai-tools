export function sectionHeading(section) {
  return section.projectLabel ? `${section.projectLabel} / ${section.label}` : section.label;
}

export function loadedTaskText(count, hasMore) {
  const noun = count === 1 ? "task" : "tasks";
  return hasMore ? `${count} ${noun} loaded; more available` : `${count} ${noun} loaded`;
}

export function appendContinuation(section) {
  if (!section.hasMore || !section.continuation?.length) return section;
  return {
    ...section,
    cards: [...section.cards, ...section.continuation],
    continuation: [],
    hasMore: false,
  };
}
