/**
 * Top-bar "Download (.zip)" button wiring.
 *
 * The button is enabled iff:
 *   - there is no in-flight chat (state.isRunning === false), AND
 *   - the active conversation has at least one assistant message with
 *     metric data or chart data (conversationHasExportableData).
 *
 * The PHP markup ships with the `disabled` attribute set so the button
 * is safely off until JS mounts.
 */

import { state } from "../state.js";
import {
  conversationHasExportableData,
  downloadConversation,
} from "./downloadConversation.js";

let buttonEl = null;

const DISABLED_CLASSES = ["opacity-50", "cursor-not-allowed"];

/**
 * Mount the download button: store the reference, wire its click handler,
 * and refresh its enabled/disabled state.
 *
 * @param {HTMLButtonElement|null} btn
 */
export function mountDownloadButton(btn) {
  if (!btn) return;
  buttonEl = btn;

  btn.addEventListener("click", async (event) => {
    event.preventDefault();
    if (btn.disabled) return;

    // Disable the button while the download is in flight.
    btn.disabled = true;
    btn.classList.add(...DISABLED_CLASSES);
    delete btn.dataset.error;

    try {
      await downloadConversation(state.activeConversation);
    } catch (err) {
      console.error("Download conversation failed:", err);
      btn.dataset.error = err && err.message ? err.message : String(err);
    } finally {
      // Re-evaluate enabled state (may still be disabled if state.isRunning
      // changed or no exportable data).
      refreshDownloadButton();
    }
  });

  refreshDownloadButton();
}

/**
 * Sync the button's disabled state + Tailwind classes with the
 * current app state. Safe to call before mount (no-op).
 */
export function refreshDownloadButton() {
  if (!buttonEl) return;
  const enabled = !state.isRunning && conversationHasExportableData(state.activeConversation);
  buttonEl.disabled = !enabled;
  if (enabled) {
    buttonEl.classList.remove(...DISABLED_CLASSES);
  } else {
    buttonEl.classList.add(...DISABLED_CLASSES);
  }
}
