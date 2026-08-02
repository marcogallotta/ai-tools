export const BOARD_ROUTE = "/";

export function isPrototypeApplicationView(search) {
  return new URLSearchParams(search).get("view") === "app";
}
