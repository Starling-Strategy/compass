/**
 * Shared accessibility behavior for the slide-out flyout panels (left menu +
 * Activity Log) so the two toggle functions stay in sync. Audit 2026-06-22:
 *   #6  inert closed panels — `w-0 + overflow-hidden` hides them visually but
 *       leaves their controls in the tab order / accessibility tree.
 *   #7  move focus into the panel on open and restore it to the opener on close
 *       (mobile modal-overlay state only).
 *   #18 reflect the open/closed state on the trigger button(s) via aria-expanded.
 *
 * Not a full focus trap: the close button is always reachable (WCAG 2.1.2 is
 * satisfied), and the desktop panels are persistent sticky columns, not modals.
 */

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

// On mobile the flyouts are modal overlays (focus should move into them); on
// desktop they're persistent sticky columns that don't need focus stealing.
// Guard `window` so importing this module never throws under the Node test
// runner (a non-browser env); it falls back to the desktop branch there.
const mobileQuery =
  typeof window !== "undefined" && window.matchMedia
    ? window.matchMedia("(max-width: 767px)")
    : { matches: false };

// Remember which element opened each panel so focus can return there on close.
const lastTrigger = new WeakMap();

/**
 * Reflect a flyout's open/closed state to assistive tech and manage focus.
 * Call AFTER the panel's width classes have been toggled.
 * @param {HTMLElement} panel - the slide-out panel
 * @param {boolean} isOpen - the state it has just transitioned INTO
 * @param {string} triggerSelector - selector matching the toggle button(s)
 */
export function applyFlyoutA11y(panel, isOpen, triggerSelector) {
  if (!panel) return;

  panel.inert = !isOpen; // #6

  document.querySelectorAll(triggerSelector).forEach((btn) => {
    btn.setAttribute("aria-expanded", String(isOpen)); // #18
  });

  if (isOpen) {
    if (mobileQuery.matches) {
      lastTrigger.set(panel, document.activeElement);
      const first = panel.querySelector(FOCUSABLE);
      if (first) first.focus(); // #7 — move focus in (mobile overlay)
    }
  } else {
    const trigger = lastTrigger.get(panel);
    if (trigger && typeof trigger.focus === "function") trigger.focus(); // #7 — restore
    lastTrigger.delete(panel);
  }
}

/**
 * Set the resting (closed) a11y state on load: panels render closed in markup
 * but without `inert`, so mark them inert and their triggers collapsed.
 * @param {HTMLElement} panel
 * @param {string} triggerSelector
 */
export function initFlyoutA11y(panel, triggerSelector) {
  if (panel) panel.inert = true;
  document.querySelectorAll(triggerSelector).forEach((btn) => {
    btn.setAttribute("aria-expanded", "false");
  });
}
