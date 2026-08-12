/**
 * Per-conversation "Reference: <session_id>" footer with click-to-copy.
 *
 * Returns a detached <div> the caller appends. Two callers share this:
 * the live streaming flow in app.js (post-`done` event) and the
 * conversation-restoration flow in chat.js's addAgentMessage.
 */

const COPY_ICON = `
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
  </svg>`;

const CHECK_ICON = `
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
    <polyline points="20 6 9 17 4 12"></polyline>
  </svg>`;

function defaultMarkup(sessionId) {
  return `
    <span class="flex items-center gap-1">${COPY_ICON}
      Reference: ${sessionId}
    </span>`;
}

function copiedMarkup() {
  return `
    <span class="flex items-center gap-1 text-[#A0C620]">${CHECK_ICON}
      Copied!
    </span>`;
}

export function createReferenceFooter(sessionId) {
  const ref = document.createElement("div");
  ref.className =
    "text-xs text-on-light-muted mt-4 pt-3 border-t border-[#DCE2EA] " +
    "cursor-pointer hover:text-[#4A91D0] transition-colors";
  ref.innerHTML = defaultMarkup(sessionId);
  ref.title = "Click to copy";
  ref.onclick = () => {
    navigator.clipboard?.writeText(sessionId);
    ref.innerHTML = copiedMarkup();
    setTimeout(() => {
      ref.innerHTML = defaultMarkup(sessionId);
    }, 2000);
  };
  return ref;
}
