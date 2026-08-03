import { FIXTURE_NOTICE } from "./stage1-board.js";

const repeated = "A deliberately long phrase used to verify wrapping, scanability, and bounded overflow without truncating factual content";

export const extremeBoardFixture = Object.freeze({
  fixtureNotice: FIXTURE_NOTICE,
  snapshotId: "fixture-extreme-content-v1",
  sections: [
    {
      id: "section-extraordinarily-long-planning-queue-name",
      label: "Planning Queue With A Deliberately Long Logical Section Label",
      projectLabel: "Cooking programme with an unusually descriptive project identity",
      hasMore: false,
      cards: [
        {
          id: "task-extreme",
          title: `${repeated}: pressure-test the full detail presentation before canonical integration`,
          status: { state: "active_operation", operation: "Verification and recovery coordination", phase: "Awaiting a carefully documented human decision" },
          attention: ["lease_attention", "verification_attention", "hold_active", "projection_abnormal"],
        },
        {
          id: "task-extreme-secondary",
          title: `${repeated}: secondary card with no active operation`,
          status: { state: "no_active_operation" },
          attention: ["recovery_required", "abandonment_active", "succession_active"],
        },
      ],
      continuation: [],
    },
    {
      id: "section-empty-long-label",
      label: "Ready For A Later Human Review Window",
      projectLabel: "Seasonal menu planning and verification",
      hasMore: false,
      cards: [],
      continuation: [],
    },
  ],
});

export const extremeDetailFixture = Object.freeze({
  fixtureNotice: FIXTURE_NOTICE,
  id: "task-extreme",
  title: `${repeated}: pressure-test the full detail presentation before canonical integration`,
  projectLabel: "Cooking programme with an unusually descriptive project identity",
  sectionLabel: "Planning Queue With A Deliberately Long Logical Section Label",
  destinationLabel: "Verification Queue After All Required Evidence Has Been Reviewed",
  status: { state: "active_operation", operation: "Verification and recovery coordination", phase: "Awaiting a carefully documented human decision" },
  attention: ["lease_attention", "verification_attention", "hold_active", "projection_abnormal"],
  contentMode: "rendered",
  content: [
    { kind: "heading", text: "Long-content resilience review" },
    { kind: "paragraph", text: `${repeated}. ${repeated}.` },
    { kind: "heading", text: "Open factual checks" },
    { kind: "list_item", text: `${repeated}; confirm the panel keeps one readable scroll surface.` },
    { kind: "list_item", text: `${repeated}; confirm no content becomes an accidental action or authority claim.` },
  ],
  disclosures: [
    { label: "Lease and verification", detail: `${repeated}. This disclosure is intentionally verbose so the review build exercises realistic wrapping and vertical rhythm.` },
    { label: "Projection", detail: `${repeated}. The Asana projection warning remains factual and does not imply that the browser can repair it.` },
  ],
  nextStep: `${repeated}. A human reviewer must resolve the named governing condition through the authoritative Dish workflow; this fixture guidance is descriptive only.`,
});
