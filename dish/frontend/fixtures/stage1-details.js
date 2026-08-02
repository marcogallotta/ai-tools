export const DETAIL_FIXTURE_NOTICE = "NON-CANONICAL DESIGN FIXTURE";

const baseDetail = {
  fixtureNotice: DETAIL_FIXTURE_NOTICE,
  projectLabel: "Cooking",
  destinationLabel: null,
  disclosures: [],
};

export const detailFixtures = Object.freeze({
  "task-aubergine": {
    ...baseDetail,
    id: "task-aubergine",
    title: "Aubergine, tomato and chickpea tray — decide the final acid and herb finish",
    sectionLabel: "Planning Queue",
    status: { state: "no_active_operation" },
    attention: ["lease_attention"],
    contentMode: "plain_text_fallback",
    fallbackText: "Canonical content could not be rendered by the approved renderer.\n\nThis inert fixture demonstrates the bounded plain-text fallback. No links or markup are active.",
    disclosures: [
      { label: "Lease", detail: "The current lease needs human attention before governed work continues." },
    ],
    nextStep: "Review the lease condition in the authoritative Dish workflow before asking an agent to continue.",
  },
  "task-biryani": {
    ...baseDetail,
    id: "task-biryani",
    title: "Chicken biryani",
    sectionLabel: "Planning Queue",
    destinationLabel: "Verification Queue",
    status: { state: "active_operation", operation: "Planning", phase: "Drafting" },
    attention: [],
    contentMode: "rendered",
    content: [
      { kind: "heading", text: "Current brief" },
      { kind: "paragraph", text: "A focused biryani plan for the current equipment and serving window." },
      { kind: "heading", text: "Decision still open" },
      { kind: "list_item", text: "Confirm whether the rice should be parboiled before the final covered cook." },
      { kind: "list_item", text: "Keep the browned onion finish separate until serving." },
    ],
    disclosures: [
      { label: "Planning", detail: "A Planning operation is active and currently in Drafting." },
    ],
    nextStep: "The assigned planning agent needs to complete the current draft and submit it through the governed workflow.",
  },
  "task-fish": {
    ...baseDetail,
    id: "task-fish",
    title: "Crisp-skinned fish with preserved lemon potatoes",
    sectionLabel: "Verification Queue",
    status: { state: "active_operation", operation: "Verification", phase: "Human review" },
    attention: ["verification_attention"],
    contentMode: "rendered",
    content: [
      { kind: "heading", text: "Verification focus" },
      { kind: "paragraph", text: "Confirm that the pan sequence preserves dry fish skin and a fresh herb finish." },
    ],
    disclosures: [
      { label: "Verification", detail: "The current verification result is awaiting human review." },
    ],
    nextStep: "A human reviewer needs to resolve the current Verification attention before sign-off can continue.",
  },
});

export function detailForCard(card) {
  return detailFixtures[card.id] ?? {
    ...baseDetail,
    id: card.id,
    title: card.title,
    sectionLabel: "Fixture section",
    status: card.status,
    attention: card.attention,
    contentMode: "rendered",
    content: [
      { kind: "paragraph", text: "Representative canonical content is intentionally brief in this design fixture." },
    ],
    nextStep: "Continue through the named governed operation shown above; this guidance is descriptive only.",
  };
}
