/**
 * Suggested follow-up chips (#1555).
 *
 * After an answer that contains chartable data, Compass offers short
 * follow-up prompts (e.g. "Show me this as a bar chart"). Each chip is a
 * native <button>, so it is focusable and activates on Enter/Space for
 * keyboard and screen-reader users without extra wiring. Clicking a chip
 * resubmits its text as a brand-new typed user message through the normal
 * submit path — it carries no machine handle (that is the #1348 clarification
 * mechanism, which is a different thing).
 *
 * In `disabled` mode (conversation replay) the chips render as inert context
 * so a stale click from history can't fire a fresh query.
 */

/**
 * @param {Object} opts
 * @param {string[]} opts.prompts - suggested follow-up prompt strings
 * @param {(prompt:string) => (boolean|void)} [opts.onPick] - return false to
 *   decline the pick (leaves the chips live); any other value accepts and
 *   locks the row.
 * @param {boolean} [opts.disabled]
 * @returns {HTMLElement}
 */
export function renderFollowupChips({ prompts, onPick, disabled = false }) {
  const group = document.createElement("div");
  group.className = "pa-followups";
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Suggested follow-ups");

  const label = document.createElement("div");
  label.className = "pa-followups-label";
  label.textContent = "Suggested follow-ups";
  group.appendChild(label);

  const list = document.createElement("div");
  list.className = "pa-followups-list";

  (prompts || []).forEach((prompt) => {
    if (typeof prompt !== "string" || prompt.trim() === "") return;
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "pa-followup-chip";
    chip.textContent = prompt;
    chip.disabled = disabled;
    if (!disabled) {
      chip.addEventListener("click", () => {
        if (group.dataset.resolved === "true") return;
        // Lock the row only once the pick is actually accepted. onPick returns
        // false to decline (e.g. another turn is already in flight); declining
        // must leave the chips live, not strand them disabled with no submit.
        // Any other return value (including undefined) accepts and locks.
        if (onPick?.(prompt) === false) return;
        group.dataset.resolved = "true";
        group.className = "pa-followups pa-followups-resolved";
        list
          .querySelectorAll("button")
          .forEach((button) => (button.disabled = true));
      });
    }
    list.appendChild(chip);
  });

  group.appendChild(list);
  return group;
}
