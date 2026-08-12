/**
 * Message Feedback UI — thumbs up/down ratings on assistant messages.
 *
 * Each assistant message gets a pair of feedback buttons. Clicking thumbs-down
 * expands a detail panel with tag pills and an optional text field.
 */

const FEEDBACK_TAGS = [
  { key: "inaccurate", label: "Inaccurate" },
  { key: "incomplete", label: "Incomplete" },
  { key: "wrong_format", label: "Wrong format" },
  { key: "wrong_data", label: "Wrong data" },
];

// SVG icons
const THUMB_UP_SVG = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>`;
const THUMB_DOWN_SVG = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L14 22a3.13 3.13 0 0 1-3-3.88Z"/></svg>`;

/**
 * Render feedback buttons for an assistant message.
 *
 * @param {Object} opts
 * @param {number|null} opts.messageId   - DB message ID (null during streaming, set on done)
 * @param {string} opts.sessionId        - Chat session ID
 * @param {string|null} opts.traceId     - Logfire trace ID
 * @param {Object|null} opts.existing    - Existing feedback state {rating, feedback_tags, feedback_text}
 * @returns {HTMLElement} The feedback container element
 */
export function renderFeedbackButtons({ messageId = null, sessionId, traceId = null, existing = null }) {
  const container = document.createElement("div");
  container.className = "feedback-container flex items-center gap-2 mt-3 mb-1";
  container.dataset.sessionId = sessionId;
  if (messageId) container.dataset.messageId = messageId;
  if (traceId) container.dataset.traceId = traceId;

  // State
  let currentRating = existing?.rating || null; // -1, 1, or null
  let selectedTags = existing?.feedback_tags || [];
  let feedbackText = existing?.feedback_text || "";

  const upBtn = createThumbButton("up", THUMB_UP_SVG);
  const downBtn = createThumbButton("down", THUMB_DOWN_SVG);

  // Detail panel (shown on thumbs-down)
  const detailPanel = document.createElement("div");
  detailPanel.className = "feedback-detail hidden mt-2 p-3 bg-[#F5F7FA] rounded-lg border border-[#DCE2EA]";
  detailPanel.innerHTML = buildDetailPanelHTML();

  // Apply existing state
  if (currentRating === 1) {
    upBtn.classList.add("feedback-active-up");
  } else if (currentRating === -1) {
    downBtn.classList.add("feedback-active-down");
    detailPanel.classList.remove("hidden");
    applyExistingTags(detailPanel, selectedTags);
    if (feedbackText) {
      const textarea = detailPanel.querySelector("textarea");
      if (textarea) textarea.value = feedbackText;
    }
  }

  // Click handlers
  upBtn.addEventListener("click", async () => {
    if (currentRating === 1) {
      // Undo
      currentRating = null;
      upBtn.classList.remove("feedback-active-up");
      detailPanel.classList.add("hidden");
      await deleteFeedback(container);
    } else {
      // Set thumbs up (or switch from down)
      currentRating = 1;
      upBtn.classList.add("feedback-active-up");
      downBtn.classList.remove("feedback-active-down");
      detailPanel.classList.add("hidden");
      selectedTags = [];
      feedbackText = "";
      await submitFeedback(container, 1, [], null);
    }
    updateStoredFeedback(container, currentRating, selectedTags, feedbackText);
  });

  downBtn.addEventListener("click", async () => {
    if (currentRating === -1) {
      // Undo
      currentRating = null;
      downBtn.classList.remove("feedback-active-down");
      detailPanel.classList.add("hidden");
      await deleteFeedback(container);
    } else {
      // Set thumbs down (or switch from up)
      currentRating = -1;
      downBtn.classList.remove("feedback-active-down");
      upBtn.classList.remove("feedback-active-up");
      downBtn.classList.add("feedback-active-down");
      detailPanel.classList.remove("hidden");
      await submitFeedback(container, -1, selectedTags, feedbackText);
    }
    updateStoredFeedback(container, currentRating, selectedTags, feedbackText);
  });

  // Detail panel tag clicks
  detailPanel.addEventListener("click", async (e) => {
    const sendBtn = e.target.closest(".feedback-send");
    if (sendBtn) {
      // Explicit Send: capture latest textarea value, submit, surface status.
      const textarea = detailPanel.querySelector(".feedback-textarea");
      feedbackText = textarea ? textarea.value.trim() : feedbackText;
      const ok = await submitFeedback(container, -1, selectedTags, feedbackText);
      setFeedbackStatus(detailPanel, ok ? "Thanks — feedback recorded." : "Couldn't send — please try again.", ok);
      updateStoredFeedback(container, currentRating, selectedTags, feedbackText);
      return;
    }

    const tagEl = e.target.closest("[data-tag]");
    if (!tagEl) return;

    const tag = tagEl.dataset.tag;
    if (selectedTags.includes(tag)) {
      selectedTags = selectedTags.filter(t => t !== tag);
      tagEl.classList.remove("feedback-tag-active");
    } else {
      selectedTags.push(tag);
      tagEl.classList.add("feedback-tag-active");
    }
    // Tags persist immediately (toggle UX). Surface a soft confirmation so
    // users see something happened — distinct from the explicit Send path.
    const ok = await submitFeedback(container, -1, selectedTags, feedbackText);
    if (!ok) setFeedbackStatus(detailPanel, "Couldn't save tag — please try again.", false);
    updateStoredFeedback(container, currentRating, selectedTags, feedbackText);
  });

  // Detail panel text change — keep the legacy auto-submit on blur as a
  // fallback so feedback isn't lost if the user doesn't click Send. The
  // Send button is the primary affordance.
  detailPanel.addEventListener("change", async (e) => {
    if (e.target.tagName === "TEXTAREA") {
      feedbackText = e.target.value.trim();
      const ok = await submitFeedback(container, -1, selectedTags, feedbackText);
      if (!ok) setFeedbackStatus(detailPanel, "Couldn't save text — please try again.", false);
      updateStoredFeedback(container, currentRating, selectedTags, feedbackText);
    }
  });

  const buttonRow = document.createElement("div");
  buttonRow.className = "flex items-center gap-1";
  buttonRow.append(upBtn, downBtn);

  const wrapper = document.createElement("div");
  wrapper.className = "feedback-wrapper";
  wrapper.append(buttonRow, detailPanel);
  container.appendChild(wrapper);

  return container;
}

// ─── Internal helpers ────────────────────────────────────────────────

function createThumbButton(direction, svg) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "feedback-btn p-1.5 rounded-md text-on-light-muted hover:bg-[#EDF1F7] transition-colors";
  btn.innerHTML = svg;
  btn.title = direction === "up" ? "Helpful" : "Not helpful";
  btn.dataset.direction = direction;
  return btn;
}

function buildDetailPanelHTML() {
  const tags = FEEDBACK_TAGS.map(
    t => `<button type="button" data-tag="${t.key}" class="feedback-tag px-3 py-1 text-xs rounded-full border border-[#DCE2EA] text-[#5A6A7A] hover:border-[#4A91D0] hover:text-[#4A91D0] transition-colors">${t.label}</button>`
  ).join("");

  // Per Ashley 2026-04-30: previously the textarea auto-submitted on blur
  // with no Send button and no Enter affordance, so users couldn't tell
  // their feedback had been recorded. Add an explicit Send button + a
  // visible status line.
  return `
    <div class="flex flex-wrap gap-2 mb-2">${tags}</div>
    <textarea
      rows="2"
      maxlength="2000"
      placeholder="What went wrong? (optional)"
      class="feedback-textarea w-full text-sm p-2 border border-[#DCE2EA] rounded-md resize-none focus:outline-none focus:border-[#4A91D0] text-[#0F223D]"
    ></textarea>
    <div class="flex items-center justify-between mt-2 gap-2">
      <span class="feedback-status text-xs text-[#5A6A7A]" aria-live="polite"></span>
      <button
        type="button"
        class="feedback-send px-3 py-1 text-xs font-medium rounded-md bg-[#1D6CD0] text-white hover:bg-[#1855A0] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
      >
        Send
      </button>
    </div>
  `;
}

function applyExistingTags(panel, tags) {
  tags.forEach(tag => {
    const el = panel.querySelector(`[data-tag="${tag}"]`);
    if (el) el.classList.add("feedback-tag-active");
  });
}

async function submitFeedback(container, rating, tags, text) {
  const messageId = parseInt(container.dataset.messageId, 10);
  const sessionId = container.dataset.sessionId;
  const traceId = container.dataset.traceId || null;

  if (!messageId || !sessionId) return false;

  try {
    const resp = await fetch(`/api/feedback.php`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        message_id: messageId,
        rating,
        feedback_tags: tags,
        feedback_text: text || null,
        trace_id: traceId,
      }),
    });
    return resp.ok;
  } catch (err) {
    console.error("Failed to submit feedback:", err);
    return false;
  }
}

/**
 * Surface a transient confirmation/error message in the feedback panel.
 * Per Ashley 2026-04-30: silent submits left users wondering whether
 * their feedback got recorded. The status line is `aria-live="polite"`
 * so screen readers announce the change.
 */
function setFeedbackStatus(panel, message, success) {
  const statusEl = panel.querySelector(".feedback-status");
  if (!statusEl) return;
  statusEl.textContent = message;
  statusEl.classList.toggle("text-[#1D6CD0]", success);
  statusEl.classList.toggle("text-[#B23A3A]", !success);
  // Auto-clear success messages after 3s; errors stay until next attempt.
  if (success) {
    setTimeout(() => {
      if (statusEl.textContent === message) statusEl.textContent = "";
    }, 3000);
  }
}

async function deleteFeedback(container) {
  const messageId = container.dataset.messageId;
  const sessionId = container.dataset.sessionId;

  if (!messageId || !sessionId) return;

  try {
    await fetch(`/api/feedback.php?session_id=${encodeURIComponent(sessionId)}&message_id=${encodeURIComponent(messageId)}`, {
      method: "DELETE",
    });
  } catch (err) {
    console.error("Failed to delete feedback:", err);
  }
}

/**
 * Update the feedback state stored on the message in localStorage.
 * Finds the message by matching messageId or by position in the conversation.
 */
function updateStoredFeedback(container, rating, tags, text) {
  const messageId = parseInt(container.dataset.messageId, 10);
  if (!messageId) return;

  // Find the conversation in state and update the message
  try {
    const { state } = window.__feedbackState || {};
    if (!state?.activeConversation) return;

    const msg = state.activeConversation.messages.find(
      m => m.role === "assistant" && m.message_id === messageId
    );
    if (msg) {
      msg.feedback = rating ? { rating, feedback_tags: tags, feedback_text: text } : null;
      // Persist
      const { persistConversation } = window.__feedbackState || {};
      if (persistConversation) persistConversation(state.activeConversation);
    }
  } catch (err) {
    // Non-critical — localStorage update failed
  }
}

