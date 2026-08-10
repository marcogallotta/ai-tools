export function sectionHeading(section) {
  return section.projectLabel ? `${section.projectLabel} / ${section.label}` : section.label;
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
