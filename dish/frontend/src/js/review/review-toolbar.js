import { REVIEW_SCENARIOS, scenarioHref } from "./review-catalog.js";

export function createReviewToolbar(activeScenario) {
  const toolbar = document.createElement("nav");
  toolbar.className = "review-toolbar";
  toolbar.setAttribute("aria-label", "Fixture review scenarios");

  const identity = document.createElement("div");
  identity.className = "review-toolbar__identity";
  const label = document.createElement("strong");
  label.textContent = "Review mode";
  const detail = document.createElement("span");
  detail.textContent = "Fixture-only · network access blocked";
  identity.append(label, detail);

  const field = document.createElement("label");
  field.className = "review-toolbar__field";
  const fieldLabel = document.createElement("span");
  fieldLabel.textContent = "Scenario";
  const select = document.createElement("select");
  select.setAttribute("aria-label", "Review scenario");
  for (const scenario of REVIEW_SCENARIOS) {
    const option = document.createElement("option");
    option.value = scenario.id;
    option.textContent = scenario.label;
    option.selected = scenario.id === activeScenario;
    select.append(option);
  }
  select.addEventListener("change", () => window.location.assign(scenarioHref(select.value)));
  field.append(fieldLabel, select);
  toolbar.append(identity, field);
  return toolbar;
}
