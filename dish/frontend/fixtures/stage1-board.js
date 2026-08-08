export const FIXTURE_NOTICE = "NON-CANONICAL DESIGN FIXTURE";

const active = (operation, phase) => ({ state: "active_operation", operation, phase });
const idle = { state: "no_active_operation" };

export const attentionLabels = Object.freeze({
  isolated: "ISOLATED",
  lease_attention: "Lease needs attention",
  verification_attention: "Verification needs attention",
  hold_active: "On hold",
  recovery_required: "Recovery required",
  abandonment_active: "Abandonment active",
  succession_active: "Succession active",
  projection_abnormal: "Asana projection issue",
});

export const boardFixture = Object.freeze({
  fixtureNotice: FIXTURE_NOTICE,
  snapshotId: "fixture-board-v1",
  sections: [
    {
      id: "section-planning",
      label: "Planning Queue",
      projectLabel: null,
      hasMore: true,
      cards: [
        {
          id: "task-aubergine",
          title: "Aubergine, tomato and chickpea tray — decide the final acid and herb finish",
          status: idle,
          attention: ["lease_attention"],
        },
        {
          id: "task-biryani",
          title: "Chicken biryani",
          status: active("Planning", "Drafting"),
          attention: [],
        },
        {
          id: "task-congee",
          title: "Ginger chicken congee",
          status: active("Change", "Awaiting authorization"),
          attention: ["hold_active"],
        },
      ],
      continuation: [
        {
          id: "task-dal",
          title: "Masoor dal with browned garlic",
          status: idle,
          attention: [],
        },
        {
          id: "task-eggs",
          title: "Soft eggs with chilli crisp rice",
          status: active("Planning", "Review"),
          attention: ["projection_abnormal"],
        },
      ],
    },
    {
      id: "section-verification",
      label: "Verification Queue",
      projectLabel: null,
      hasMore: false,
      cards: [
        {
          id: "task-fish",
          title: "Crisp-skinned fish with preserved lemon potatoes",
          status: active("Verification", "Human review"),
          attention: ["verification_attention", "lease_attention"],
        },
        {
          id: "task-gnocchi",
          title: "Pan-fried gnocchi with peas and mint",
          status: active("Recovery", "Evidence review"),
          attention: ["recovery_required"],
        },
        {
          id: "task-haleem",
          title: "Weeknight chicken haleem",
          status: idle,
          attention: ["abandonment_active"],
        },
        {
          id: "task-iranian-rice",
          title: "Saffron rice with tahdig",
          status: active("Succession", "Successor prepared"),
          attention: ["succession_active"],
        },
      ],
    },
    {
      id: "section-ready",
      label: "Ready",
      projectLabel: "Cooking",
      hasMore: false,
      cards: [],
    },
    {
      id: "section-ready-archive",
      label: "Ready",
      projectLabel: "Seasonal menu",
      hasMore: false,
      cards: [
        {
          id: "task-jollof",
          title: "Smoky jollof-style rice",
          status: idle,
          attention: ["isolated", "projection_abnormal"],
        },
      ],
    },
  ],
});

export const zeroSectionFixture = Object.freeze({
  fixtureNotice: FIXTURE_NOTICE,
  snapshotId: "fixture-empty-registry-v1",
  sections: [],
});
