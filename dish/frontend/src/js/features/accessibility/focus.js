export function focusElement(element) {
  if (element instanceof HTMLElement) {
    element.focus({ preventScroll: true });
    return true;
  }
  return false;
}
