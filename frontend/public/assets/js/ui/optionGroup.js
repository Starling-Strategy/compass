/**
 * Structured clarification options (#1348).
 *
 * Renders the grounded, clickable choices Compass offers on a clarification
 * turn (e.g. district disambiguation) plus an always-present "type your own
 * answer" affordance. Each choice is born from a real catalog row upstream;
 * clicking one resumes the query deterministically (the click posts the
 * option's machine handle). Free text stays first-class — the composer is
 * never disabled, and the dashed affordance points the user back to it.
 *
 * Options are exposed as an ARIA listbox so keyboard and screen-reader users
 * get first-class access. In `disabled` mode (conversation replay) the group
 * renders as inert context so a stale click from history can't fire a fresh
 * query.
 */

/**
 * @param {Object} opts
 * @param {Array<{value:string,label:string,detail?:string}>} opts.options
 * @param {(option:{value:string,label:string,detail?:string}) => void} [opts.onPick]
 * @param {() => void} [opts.onTypeYourOwn]
 * @param {boolean} [opts.disabled]
 * @returns {HTMLElement}
 */
export function renderOptionGroup({
  options,
  onPick,
  onTypeYourOwn,
  disabled = false,
}) {
  const group = document.createElement("div");
  group.className = "pa-options";
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Choose an option to continue");

  const label = document.createElement("div");
  label.className = "pa-options-label";
  label.textContent = "Pick one to continue — or type your own answer below";
  group.appendChild(label);

  // The free-text affordance is created first so the option handlers can
  // disable it on pick; it is appended last so it reads as the final choice.
  const ownBtn = document.createElement("button");
  ownBtn.type = "button";
  ownBtn.className = "pa-option pa-option-own";
  ownBtn.disabled = disabled;
  const ownLabel = document.createElement("span");
  ownLabel.className = "pa-option-label";
  ownLabel.textContent = "✎ Something else — type your own answer";
  const ownDetail = document.createElement("span");
  ownDetail.className = "pa-option-detail";
  ownDetail.textContent =
    "These are just my closest matches. Tell me in your own words.";
  ownBtn.appendChild(ownLabel);
  ownBtn.appendChild(ownDetail);

  const list = document.createElement("div");
  list.className = "pa-options-list";
  list.setAttribute("role", "listbox");

  const lock = () => {
    group.dataset.resolved = "true";
    group.className = "pa-options pa-options-resolved";
    list.querySelectorAll("button").forEach((button) => {
      button.disabled = true;
    });
    ownBtn.disabled = true;
  };

  (options || []).forEach((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "pa-option";
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", "false");
    button.disabled = disabled;

    const optionLabel = document.createElement("span");
    optionLabel.className = "pa-option-label";
    optionLabel.textContent = option.label;
    button.appendChild(optionLabel);

    if (option.detail) {
      const optionDetail = document.createElement("span");
      optionDetail.className = "pa-option-detail";
      optionDetail.textContent = option.detail;
      button.appendChild(optionDetail);
    }

    if (!disabled) {
      button.addEventListener("click", () => {
        if (group.dataset.resolved === "true") return;
        button.setAttribute("aria-selected", "true");
        lock();
        onPick?.(option);
      });
    }
    list.appendChild(button);
  });
  group.appendChild(list);

  if (!disabled) {
    ownBtn.addEventListener("click", () => {
      if (group.dataset.resolved === "true") return;
      onTypeYourOwn?.();
    });
  }
  group.appendChild(ownBtn);

  return group;
}
