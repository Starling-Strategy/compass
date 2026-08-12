import { renderMarkdown } from "../utils/markdown.js";
import { getConversationById } from "../storage.js";
import { resetUIafterFirstMessage } from "./cleanup.js";
import { state } from "../state.js";
import { renderChart } from "../utils/chartRenderer.js";
import { addTableExportButtons } from "./tableExport.js";
import { renderSourcesPanel, processCitationLinks, renumberCitations } from "./sources.js";
import { renderFeedbackButtons } from "./feedback.js";
import { createReferenceFooter } from "./referenceFooter.js";
import { renderOptionGroup } from "./optionGroup.js";
import { updateUrl } from "../conversations.js";
import { refreshDownloadButton } from "./downloadButton.js";
import { collapsifyAgentSections } from "./collapsibles.js";

// Get DOM elements directly to avoid circular import with app.js
const chat = document.getElementById("chat");
const chatWrapper = document.getElementById("chatWrapper");
const submitBtns = document.getElementsByClassName("submit-btn");

export function clearChat(chat) {
  chat.innerHTML = "";
}

export function addUserMessage(chat, text) {
  const wrap = document.createElement("div");
  wrap.className = "flex justify-end pr-3 md:pr-5";

  const bubble = document.createElement("div");
  bubble.className =
    "chat-bubble user p-[10px] bg-outline-dark font-sans text-[18px] leading-[1.2] font-semibold";
  bubble.textContent = text;

  wrap.appendChild(bubble);
  chat.appendChild(wrap);
  scrollChat();
}

export function addAgentMessage(chat, markdown, session_id = null, charts = null, exportData = null, citations = null, quotes = null, messageId = null, feedback = null, options = null) {
  const wrap = document.createElement("div");
  wrap.className = "agent pl-3 md:pl-5";

  // 1. Render markdown content first
  wrap.innerHTML = renderMarkdown(markdown);

  // 1.5 #1228: fold secondary sections into collapsibles (answer leads).
  collapsifyAgentSections(wrap);

  // 2. Render any associated charts (with their export buttons)
  if (charts && Array.isArray(charts)) {
    charts.forEach(chartData => {
      try {
        renderChart(wrap, chartData);
      } catch (error) {
        console.error("Error rendering chart:", error);
      }
    });
  }
  
  // 3. Renumber citations to sequential [1],[2],[3] then make them clickable
  if (citations && citations.length > 0) {
    renumberCitations(wrap, citations);
    processCitationLinks(wrap, citations);
  }

  // 3.5 Process blockquote links for document viewer
  if (quotes && quotes.length > 0) {
    import("./quotes.js").then(({ processQuoteLinks }) => {
      processQuoteLinks(wrap, quotes);
    });
  }

  // 4. Add per-table CSV export buttons
  addTableExportButtons(wrap, session_id);

  // 5. Render sources panel LAST
  if (citations && citations.length > 0) {
    const sourcesPanel = renderSourcesPanel(citations);
    if (sourcesPanel) {
      wrap.appendChild(sourcesPanel);
    }
  }
  
  // 5.5 #1348: replay structured clarification options as disabled context —
  // a stale click from history must not fire a fresh query.
  if (options && options.length > 0) {
    wrap.appendChild(renderOptionGroup({ options, disabled: true }));
  }

  // 6. Add feedback buttons (before reference number)
  if (session_id && messageId) {
    const feedbackEl = renderFeedbackButtons({
      messageId,
      sessionId: session_id,
      existing: feedback,
    });
    wrap.appendChild(feedbackEl);
  }

  // 7. Add reference number at the very end
  if (session_id) {
    wrap.appendChild(createReferenceFooter(session_id));
  }

  chat.appendChild(wrap);
  return wrap;
}

function scrollChat() {
  chat.scrollTop = chat.scrollHeight;
}

export function maximizeChatSize() {
  chatWrapper.classList.add("flex-1");
  chat.classList.add("flex-1");
}

export function resetChatSize() {
  chatWrapper.classList.remove("flex-1");
  chat.classList.remove("flex-1");
}

export function renderSessionUnavailable(chat, sessionId) {
  clearChat(chat);
  maximizeChatSize();
  resetUIafterFirstMessage(submitBtns);

  const dashboardBase = window.APP_CONFIG?.dashboardBaseUrl || "";
  const dashboardUrl = dashboardBase
    ? `${dashboardBase.replace(/\/$/, "")}/compass/conversations/${encodeURIComponent(sessionId)}`
    : null;

  const card = document.createElement("div");
  card.className = "flex items-center justify-center w-full py-12";
  card.innerHTML = `
    <div class="max-w-md text-center px-6 py-8 border border-[#DCE2EA] rounded-lg bg-white">
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#7A91B2" stroke-width="2" class="mx-auto mb-3">
        <circle cx="12" cy="12" r="10"/>
        <line x1="12" y1="8" x2="12" y2="12"/>
        <line x1="12" y1="16" x2="12.01" y2="16"/>
      </svg>
      <h2 class="text-[17px] font-semibold text-text-primary mb-2">This conversation isn't available here</h2>
      <p class="text-[14px] text-[#7A91B2] leading-[1.5] mb-4">
        It doesn't appear in this browser's history. If you have dashboard access, you can view it there.
      </p>
      ${dashboardUrl
        ? `<a href="${dashboardUrl}" class="inline-flex items-center gap-1 text-[14px] text-[#4A91D0] hover:underline font-medium">
            View in dashboard
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="7" y1="17" x2="17" y2="7"/>
              <polyline points="7 7 17 7 17 17"/>
            </svg>
          </a>`
        : `<p class="text-[12px] text-[#7A91B2] font-mono">${encodeURIComponent(sessionId)}</p>`}
    </div>
  `;
  chat.appendChild(card);
}

export async function renderSelectedChat(id) {
  clearChat(chat);
  maximizeChatSize();
  resetUIafterFirstMessage(submitBtns);
  const conversation = getConversationById(id);

  if (!conversation) {
    console.error("Conversation not found:", id);
    return;
  }

  state.activeConversation = conversation;
  updateUrl(conversation.session_id);

  conversation.messages.forEach((msg) => {
    if (msg.role === "user") {
      addUserMessage(chat, msg.content);
    } else if (msg.role === "assistant") {
      // Pass all data when restoring conversation
      addAgentMessage(
        chat,
        msg.content,
        msg.session_id || conversation.session_id,
        msg.charts,
        msg.exportData,
        msg.citations,
        msg.quotes,
        msg.message_id || null,
        msg.feedback || null,
        msg.options || null,
      );
    }
    scrollChat();
  });
  refreshDownloadButton();
}
