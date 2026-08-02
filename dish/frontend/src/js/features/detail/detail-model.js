import { workflowStatusText } from "../cards/card-model.js";

const supportedKinds = new Set(["heading", "paragraph", "list_item"]);

export function detailStatusText(detail) {
  return workflowStatusText(detail.status);
}

export function contentPresentation(detail) {
  if (detail.contentMode === "plain_text_fallback") {
    return { mode: "fallback", text: detail.fallbackText ?? "Content is unavailable." };
  }
  const blocks = Array.isArray(detail.content) ? detail.content : [];
  if (blocks.every((block) => supportedKinds.has(block.kind) && typeof block.text === "string")) {
    return { mode: "rendered", blocks };
  }
  return { mode: "fallback", text: "Content was rejected by the fixture renderer." };
}
