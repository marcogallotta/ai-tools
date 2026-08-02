import { PROTOTYPE_LABEL } from "../config.js";

export function loginShellModel() {
  return {
    kind: "login",
    heading: "Private Dish board",
    description: "Sign in with the environment shared password.",
    prototypeLabel: PROTOTYPE_LABEL,
  };
}

export function applicationShellModel() {
  return {
    kind: "protected-empty",
    heading: "Dish board",
    emptyHeading: "Frontend foundation ready",
    emptyDescription: "Board behavior is intentionally absent from Delivery Stage 0.",
    prototypeLabel: PROTOTYPE_LABEL,
  };
}
