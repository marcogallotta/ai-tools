export function loginShellModel() {
  return {
    kind: "login",
    heading: "Private Dish board",
    description: "Sign in with the environment shared password.",
  };
}

export function applicationShellModel() {
  return {
    kind: "protected-empty",
    heading: "Dish board",
    emptyHeading: "Frontend foundation ready",
    emptyDescription: "Board behavior is intentionally absent from Delivery Stage 0.",
  };
}
