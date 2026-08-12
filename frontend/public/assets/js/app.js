import { state } from "./state.js";
import {
  deleteConversation,
  loadConversations,
  updateConversation,
} from "./storage.js";
import {
  createConversation,
  persistConversation,
  setTitleIfEmpty,
  updateUrl,
  getSessionFromUrl,
} from "./conversations.js";
import {
  addUserMessage,
  addAgentMessage,
  clearChat,
  maximizeChatSize,
} from "./ui/chat.js?v=20260608a";
import { collapsifyAgentSections } from "./ui/collapsibles.js";
import { renderConversationList } from "./ui/sidebar.js";
import {
  addActivity,
  resetActivtyLogs,
  resetProgressBar,
} from "./ui/activity.js";
import { resetChatUi, resetUIafterFirstMessage } from "./ui/cleanup.js";
import { startAgent } from "./agentSSE.js";
import { toggleLeftSidebar } from "./ui/sidebar.js";
import { toggleActivityLogSidebar } from "./ui/activity.js";
import { initFlyoutA11y } from "./ui/flyoutA11y.js";
import { renderChart } from "./utils/chartRenderer.js";
import { addTableExportButtons } from "./ui/tableExport.js";
import { mountDownloadButton, refreshDownloadButton } from "./ui/downloadButton.js";
import { transformChartData } from "./utils/chartDataTransformer.js";
import { renderSourcesPanel, processCitationLinks, renumberCitations } from "./ui/sources.js";
import { processQuoteLinks } from "./ui/quotes.js";
import { renderFeedbackButtons } from "./ui/feedback.js";
import { createReferenceFooter } from "./ui/referenceFooter.js";
import {
  createActivityIndicator,
  applyActivityEvent,
  STAGE_LABELS,
  friendlyToolLabel,
} from "./ui/activityIndicator.js";
import { renderMarkdown } from "./utils/markdown.js";
import { renderOptionGroup } from "./ui/optionGroup.js";
import { renderFollowupChips } from "./ui/followupChips.js";
import {
  isDebugMode, getCaseId, fetchScenarioCase, normalizeScenarioCaseSteps,
  legacyScenarioLaunchError, debugState, createDebugBar,
  fillDebugResults, fetchAndFillCostData, initReviewWidget,
} from "./debug.js?v=20260608a";

// Embed mode detection
const isEmbedMode = window.APP_CONFIG?.embedMode === true;

// #1348: machine handle of a clicked clarification option, set just before the
// form submit it triggers and consumed by that turn. Null on every typed turn.
let pendingSelectedOption = null;

function escapeHtml(str) {
  return String(str)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export const chatWrapper = document.getElementById("chatWrapper");
export const chat = document.getElementById("chat");
export const form = document.getElementById("form");
export const input = document.getElementById("input");
const conversationList = document.getElementById("conversationList");
const newConversationBtn = document.getElementById("newConversationBtn");
const leftSidebarButtons = document.querySelectorAll(".toggleLeftSidebarBtn");
const activitySidebarButtons = document.querySelectorAll(
  ".toggleActivityLogBtn"
);
const downloadConversationBtn = document.querySelector(".downloadConversationBtn");
if (downloadConversationBtn) mountDownloadButton(downloadConversationBtn);
const leftSidebar = document.getElementById("leftSidebar");
const activityLog = document.getElementById("activityLog");
const activityLogSidebar = document.getElementById("activityLogSidebar");
const leftBackdrop = document.getElementById("leftBackdrop");
const rightBackdrop = document.getElementById("rightBackdrop");

export const submitBtns = document.getElementsByClassName("submit-btn");

// Character counter and error elements
const charCounter = document.getElementById("charCounter");
const charCount = document.getElementById("charCount");
const MAX_CHARS = window.COMPASS_CHAT_MESSAGE_MAX_CHARS || 2000;

/* ---------------- CHARACTER COUNTER & ERROR HANDLING ---------------- */

function updateCharCounter() {
  const length = input.value.length;
  charCount.textContent = length;
  
  // Update styling based on character count
  if (length > MAX_CHARS) {
    charCounter.classList.remove("text-on-dark-muted");
    charCounter.classList.add("text-status-error-on-dark", "font-medium");
  } else if (length >= MAX_CHARS * 0.9) {
    charCounter.classList.remove("text-on-dark-muted", "text-status-error-on-dark");
    charCounter.classList.add("text-[#F68E1E]", "font-medium");
  } else {
    charCounter.classList.remove("text-status-error-on-dark", "text-[#F68E1E]", "font-medium");
    charCounter.classList.add("text-on-dark-muted");
  }
}

// Input event listener for character counting
input.addEventListener("input", updateCharCounter);

// Initialize counter on page load
updateCharCounter();

leftSidebarButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    toggleLeftSidebar(leftSidebar, leftBackdrop);
  });
});

activitySidebarButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    toggleActivityLogSidebar(activityLogSidebar, rightBackdrop);
  });
});

// Close sidebars when clicking on backdrop
leftBackdrop?.addEventListener("click", () => {
  toggleLeftSidebar(leftSidebar, leftBackdrop);
});

rightBackdrop?.addEventListener("click", () => {
  toggleActivityLogSidebar(activityLogSidebar, rightBackdrop);
});

// Flyout accessibility (audit 2026-06-22). Set the resting closed state
// (inert + aria-expanded=false) on load, and let Escape close whichever panel
// is open — focus is restored to its opener inside the toggle functions.
initFlyoutA11y(leftSidebar, ".toggleLeftSidebarBtn");
initFlyoutA11y(activityLogSidebar, ".toggleActivityLogBtn");

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (leftSidebar?.classList.contains("min-w-80")) {
    toggleLeftSidebar(leftSidebar, leftBackdrop);
  }
  if (activityLogSidebar?.classList.contains("min-w-80")) {
    toggleActivityLogSidebar(activityLogSidebar, rightBackdrop);
  }
});

document.querySelectorAll(".sampleQuestionCard").forEach((card) => {
  card.addEventListener("click", () => {
    if (state.isRunning) return;

    const text = card.querySelector("p")?.textContent.trim();
    if (!text) return;

    input.value = text;
    updateCharCounter(); // Update counter when sample question is selected

    form.dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true })
    );
  });
});

/* ---------------- INIT ---------------- */

function refreshSidebar(activeConversationId = null) {
  renderConversationList(
    conversationList,
    loadConversations(),
    {
      activeConversationId,
      onRename: (id, newTitle) => {
        updateConversation(id, { title: newTitle });
        refreshSidebar(id);
      },
      onDelete: (id) => {
        deleteConversation(id);
        refreshSidebar();
      },
    }
  );
}

// Expose references for the feedback module to update localStorage
window.__feedbackState = { state, persistConversation };

refreshSidebar();

// On page load, check URL for ?session=UUID and restore that conversation.
// Skip in debug mode — debug URLs always rerun the scenario fresh.
const urlSession = getSessionFromUrl();
if (urlSession && !isDebugMode()) {
  const convs = loadConversations();
  const match = convs.find((c) => c.session_id === urlSession);
  if (match) {
    // Found in localStorage — render it
    const { renderSelectedChat } = await import("./ui/chat.js?v=20260608a");
    renderSelectedChat(match.id);
    refreshSidebar(match.id);
  } else {
    // Not in this user's localStorage — show a friendly gate linking to the dashboard.
    const { renderSessionUnavailable } = await import("./ui/chat.js?v=20260608a");
    renderSessionUnavailable(chat, urlSession);
  }
}

// ── Debug Mode: initialize debug bar if ?debug=true ──
let debugBar = null;
let debugCaseSteps = [];
let debugNextStepIndex = 0;
// Scorecard dimension for the review widget. The B-spine case exposes the
// canonical accuracy_primary_dimension as its `feature` field (see
// db/scenarios.py); stash it at mount so onFinish can pass it to the widget.
let debugDimension = null;

function submitNextDebugCaseStep() {
  if (!isDebugMode() || state.isRunning) return;
  const nextStep = debugCaseSteps[debugNextStepIndex];
  if (!nextStep?.prompt) return;
  debugNextStepIndex += 1;
  input.value = nextStep.prompt;
  updateCharCounter();
  setTimeout(() => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  }, 100);
}

if (isDebugMode()) {
  const caseId = getCaseId();
  const scenarioCase = caseId
    ? await fetchScenarioCase(caseId)
    : legacyScenarioLaunchError();
  debugBar = createDebugBar(scenarioCase);
  debugDimension = scenarioCase?.feature ?? null;
  // Insert at the top of body so it's above everything
  document.body.prepend(debugBar);
  // Push content down to account for the fixed debug bar
  requestAnimationFrame(() => {
    document.body.style.paddingTop = debugBar.offsetHeight + "px";
  });

  // Auto-submit every B-spine case step. Source of truth is the DB, so editing
  // a case updates every debug link that references it.
  debugCaseSteps = normalizeScenarioCaseSteps(scenarioCase);
  if (debugCaseSteps.length > 0 && !state.isRunning) {
    submitNextDebugCaseStep();
  }
}

// Check URL for ?prompt=<text> and auto-submit a new conversation.
// Skipped in debug mode — scenario-driven auto-submit handles that path.
const urlPrompt = new URL(window.location).searchParams.get("prompt");
if (urlPrompt && !state.isRunning && !isDebugMode()) {
  input.value = urlPrompt;
  updateCharCounter();
  // Clear prompt from URL so refresh doesn't re-send
  const cleanUrl = new URL(window.location);
  cleanUrl.searchParams.delete("prompt");
  window.history.replaceState({}, "", cleanUrl);
  // Auto-submit after brief delay to let DOM settle
  setTimeout(() => {
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  }, 100);
}

/* ---------------- CONVERSATIONS ---------------- */

if (newConversationBtn) {
  newConversationBtn.onclick = () => {
    state.activeConversation = createConversation();
    resetChatUi(submitBtns);
    clearChat(chat);
    refreshDownloadButton();
    resetProgressBar();
    resetActivtyLogs(activityLog);
    updateUrl(null);
  };
}

// SSN-84: Stop generating button. Click delegates to the closure attached
// to state by the form-submit handler so we have access to per-turn locals.
document.querySelectorAll('[data-role="stop"]').forEach((btn) => {
  btn.addEventListener("click", () => {
    state.cancelInFlight?.();
  });
});

/* ---------------- FORM SUBMIT ---------------- */

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || state.isRunning) return;

  // Check character limit
  if (text.length > MAX_CHARS) {
    return;
  }

  if (!state.activeConversation) {
    state.activeConversation = createConversation();
  }

  resetUIafterFirstMessage(submitBtns);
  maximizeChatSize();
  setTitleIfEmpty(state.activeConversation, text);

  addUserMessage(chat, text);
  state.activeConversation.messages.push({
    role: "user",
    content: text,
    timestamp: Date.now(),
  });
  persistConversation(state.activeConversation);
  refreshSidebar();

  input.value = "";
  updateCharCounter(); // Reset counter after clearing input
  state.isRunning = true;
  refreshDownloadButton();
  updateNewConversationsState();

  let agentBubble = addAgentMessage(chat, "");

  const loadingWrapper = document.createElement("div");
  loadingWrapper.className = "flex flex-col gap-1 text-gray-400";
  loadingWrapper.setAttribute("role", "status");
  loadingWrapper.setAttribute("aria-label", "Compass is working");

  const activityIndicatorEl = createActivityIndicator();

  const ephemeraSlot = document.createElement("div");
  ephemeraSlot.id = "ephemera-slot";
  ephemeraSlot.className = "ephemera-slot";

  loadingWrapper.append(activityIndicatorEl, ephemeraSlot);

  agentBubble.appendChild(loadingWrapper);

  let fullResponse = "";
  let chartDataArray = [];         // Transformed charts for rendering
  let originalChartDataArray = []; // Original backend charts for export
  let metricDataArray = [];        // Metric data for export
  let citationsArray = [];         // Citations for sources panel
  let quotesArray = [];            // Validated contract language quotes
  let turnOptions = null;          // #1348: structured clarification options for this turn
  let turnFollowups = null;        // #1555: suggested follow-up prompts for this turn
  let threadId = "";

  // #1348: if this turn was triggered by clicking a structured clarification
  // option, the option's machine handle was stashed here; capture it for this
  // turn and clear it so the next typed turn isn't affected.
  const turnSelectedOption = pendingSelectedOption;
  pendingSelectedOption = null;

  // Ephemera rotation — frontend owns the timer so cadence is independent
  // of backend pipeline phase timing.
  const EPHEMERA_ROTATE_MS = 8000;
  const EPHEMERA_FADE_MS = 200;
  let ephemeraCards = [];
  let ephemeraIndex = 0;
  let ephemeraTimerId = null;
  let ephemeraSwapTimerId = null;

  function renderEphemeraCard(card) {
    const slot = document.getElementById("ephemera-slot");
    if (!slot || !card) return;
    slot.classList.add("ephemera-slot--active");
    slot.innerHTML = `
      <p class="ephemera-card">
        <span class="ephemera-card__label">Did you know —</span>${escapeHtml(card.content)}
      </p>
    `;
  }

  function crossfadeToEphemeraCard(card) {
    const slot = document.getElementById("ephemera-slot");
    if (!slot) return;
    const existing = slot.querySelector(".ephemera-card");
    if (!existing) {
      renderEphemeraCard(card);
      return;
    }
    existing.classList.add("ephemera-card--fading-out");
    ephemeraSwapTimerId = setTimeout(() => {
      ephemeraSwapTimerId = null;
      renderEphemeraCard(card);
    }, EPHEMERA_FADE_MS);
  }

  function stopEphemeraRotation() {
    if (ephemeraTimerId) {
      clearInterval(ephemeraTimerId);
      ephemeraTimerId = null;
    }
    if (ephemeraSwapTimerId) {
      clearTimeout(ephemeraSwapTimerId);
      ephemeraSwapTimerId = null;
    }
    ephemeraCards = [];
  }

  state.evtSource = startAgent({
    question: text,
    sessionId: state.activeConversation?.session_id ?? null,
    debug: isDebugMode(),
    selectedOption: turnSelectedOption,

    onOptions(optionsPayload) {
      // #1348: stash this turn's structured clarification options; they're
      // rendered + persisted in onFinish (after the prose + sources land).
      turnOptions =
        optionsPayload && Array.isArray(optionsPayload.options)
          ? optionsPayload.options
          : null;
    },

    onFollowups(prompts) {
      // #1555: stash this turn's suggested follow-up prompts; they're rendered
      // in onFinish (after the answer + sources land). agentSSE already
      // guarantees a non-empty array of strings.
      turnFollowups = Array.isArray(prompts) ? prompts : null;
    },

    onTrace(traceId) {
      threadId = traceId;
      if (isDebugMode()) debugState.traceId = traceId;
    },

    onDelta(chunk) {
      fullResponse += chunk;
      if (loadingWrapper.parentNode) loadingWrapper.remove();
      stopEphemeraRotation();
      agentBubble.innerHTML = renderMarkdown(fullResponse);
      chat.scrollTop = chat.scrollHeight;
    },

    onStatus(message) {
      // Status messages are logged to the activity panel only; the live
      // progress timeline is driven by stage/tool events (onStage*/onToolCall*).
      addActivity(activityLog, "Status", message);
    },

    onEphemeraBatch(cards) {
      if (!Array.isArray(cards) || cards.length === 0) return;
      stopEphemeraRotation();
      ephemeraCards = cards;
      ephemeraIndex = 0;
      renderEphemeraCard(cards[0]);
      addActivity(activityLog, "Ephemera batch", `${cards.length} card(s)`);
      if (cards.length > 1) {
        ephemeraTimerId = setInterval(() => {
          if (ephemeraCards.length === 0) return;
          ephemeraIndex = (ephemeraIndex + 1) % ephemeraCards.length;
          crossfadeToEphemeraCard(ephemeraCards[ephemeraIndex]);
        }, EPHEMERA_ROTATE_MS);
      }
    },

    onStageStart(payload) {
      applyActivityEvent(activityIndicatorEl, {
        type: "begin",
        key: `stage:${payload.stage}`,
        label: STAGE_LABELS[payload.stage] || payload.message || "Working…",
      });
      addActivity(activityLog, "Stage", payload.stage);
    },

    onStageEnd(payload) {
      applyActivityEvent(activityIndicatorEl, {
        type: "complete",
        key: `stage:${payload.stage}`,
        durationMs: payload.duration_ms,
      });
    },

    onToolCallStart(payload) {
      applyActivityEvent(activityIndicatorEl, {
        type: "begin",
        key: `tool:${payload.tool_call_id}`,
        label: friendlyToolLabel(payload.tool_name),
      });
      addActivity(activityLog, "Tool", payload.tool_name);
    },

    onToolCallEnd(payload) {
      applyActivityEvent(activityIndicatorEl, {
        type: "complete",
        key: `tool:${payload.tool_call_id}`,
      });
      if (payload.result_summary) {
        applyActivityEvent(activityIndicatorEl, {
          type: "detail",
          key: `tool:${payload.tool_call_id}`,
          detail: payload.result_summary,
        });
      }
      addActivity(activityLog, "Tool result", payload.result_summary || payload.status || "");
    },

    onGlossary(terms) {
      // Store glossary terms (rendered inline by the Writer agent)
      if (terms && terms.length > 0) {
        addActivity(activityLog, "Glossary", `${terms.length} term(s) defined`);
      }
    },

    onChart(backendChartData) {
      if (loadingWrapper.parentNode) loadingWrapper.remove();
      stopEphemeraRotation();

      // Store original backend format for export (deep clone to avoid mutation)
      originalChartDataArray.push(JSON.parse(JSON.stringify(backendChartData)));
      
      // Transform to chartRenderer format
      const transformedChart = transformChartData(backendChartData);
      chartDataArray.push(transformedChart);

      // Render the chart
      const chartWrapper = renderChart(agentBubble, transformedChart);

      // Log in activity
      addActivity(activityLog, "Chart Generated", transformedChart.title || "Visualization created");

      // #1467: a requested chart is appended to the bubble *below* the answer's
      // markdown table. For a long table (e.g. the 77 ranked districts in case
      // 470) the chart lands well below the fold, so it reads as "missing" even
      // though it rendered. Scroll the chart itself into view rather than the
      // chat bottom — the sources/feedback blocks that land later in onFinish
      // would otherwise push the chart back past the visible area.
      if (chartWrapper) {
        chartWrapper.scrollIntoView({ behavior: "smooth", block: "nearest" });
      } else {
        chat.scrollTop = chat.scrollHeight;
      }
    },

    onData(metricData) {
      
      // Capture metric/comparison data for CSV export (deep clone)
      metricDataArray.push(JSON.parse(JSON.stringify(metricData)));
      
      // Log in activity
      addActivity(activityLog, "Data Generated", "Metric comparison data available");
    },

    onCitations(citations) {

      // Store citations (can be array or object with citations property)
      if (Array.isArray(citations)) {
        citationsArray = citations;
      } else if (citations.citations && Array.isArray(citations.citations)) {
        citationsArray = citations.citations;
      }

      // Log in activity
      addActivity(activityLog, "Sources", `${citationsArray.length} source(s) cited`);
    },

    onQuotes(quotes) {
      if (Array.isArray(quotes)) {
        quotesArray = quotes;
        addActivity(activityLog, "Quotes", `${quotes.length} contract excerpt(s)`);
      }
    },

    onCriticVerdict(verdict) {
      if (isDebugMode()) debugState.criticVerdict = verdict;
    },

    onFinish({
      session_id,
      trace_id,
      timing,
      message_ids,
      exports,
      engine,
      v3_mode,
      fallback_reason,
      v3_data_source,
    }) {
      state.isRunning = false;
      stopEphemeraRotation();
      updateNewConversationsState();

      if (!state.activeConversation.session_id) {
        state.activeConversation.session_id = session_id;
      }
      updateUrl(session_id);

      // Extract the assistant message DB ID from the done event
      const assistantMessageId = message_ids && message_ids.length > 0 ? message_ids[message_ids.length - 1] : null;

      // Populate debug bar if in debug mode
      if (isDebugMode() && debugBar) {
        debugState.sessionId = session_id;
        if (timing) debugState.timing = timing;
        if (trace_id && !debugState.traceId) debugState.traceId = trace_id;
        debugState.routing = {
          engine,
          v3_mode,
          fallback_reason,
          v3_data_source,
        };
        fillDebugResults(debugBar);
        // Fetch cost/brief data from the debug endpoint (async, fills in after)
        if (session_id) fetchAndFillCostData(debugBar, session_id);
        // Wire the "Did that happen?" review widget now that the session/case
        // context exists (the session is created by the auto-submitted turn).
        if (session_id) {
          initReviewWidget(debugBar, {
            sessionId: session_id,
            caseId: getCaseId(),
            traceId: trace_id ?? debugState.traceId,
            dimension: debugDimension,
          });
        }
      }

      // #1228: fold the renderer's secondary sections (coverage disclosure,
      // methodology, …) into collapsibles now that streaming is done and the
      // markdown body is final. The streaming path renders the body via
      // onDelta's innerHTML assignment, which doesn't fold — so this is where
      // the live answer gets folded (addAgentMessage's fold only covers the
      // history-replay path). Runs before sources/feedback/footer are appended;
      // the chart (already present from onChart) is a <div>, which the fold
      // never absorbs.
      collapsifyAgentSections(agentBubble);

      // Renumber citations to be sequential [1],[2],[3] with no gaps (FIX-92)
      renumberCitations(agentBubble, citationsArray);

      // Process citation links in the response text (pass citations for proper ID mapping)
      processCitationLinks(agentBubble, citationsArray);

      // Process blockquote links for document viewer
      processQuoteLinks(agentBubble, quotesArray);

      // Add per-table CSV export buttons (mirrors per-chart pattern)
      // metrics order must match rendered table order — see tableExport.js
      addTableExportButtons(agentBubble, session_id, {
        metrics: metricDataArray,
        exports: Array.isArray(exports) ? exports : [],
        charts: originalChartDataArray,
      });

      // Render sources panel LAST (always at the bottom)
      if (citationsArray.length > 0) {
        const sourcesPanel = renderSourcesPanel(citationsArray);
        if (sourcesPanel) {
          agentBubble.appendChild(sourcesPanel);
        }
      }

      // #1348: structured clarification options — clickable, grounded choices
      // under the question. Clicking one re-submits with the option's machine
      // handle so the backend resumes deterministically; free text stays live.
      if (turnOptions && turnOptions.length > 0) {
        const optionGroup = renderOptionGroup({
          options: turnOptions,
          onPick(option) {
            if (state.isRunning) return;
            input.value = option.label;
            pendingSelectedOption = option.value;
            form.requestSubmit();
          },
          onTypeYourOwn() {
            input.focus();
            input.scrollIntoView({ behavior: "smooth", block: "center" });
          },
        });
        agentBubble.appendChild(optionGroup);
      }

      // #1555: suggested follow-up prompts (e.g. "Show me this as a bar
      // chart"). Clicking a chip resubmits its text as a fresh typed user
      // message through the existing submit path — it carries no machine
      // handle, so it must NOT touch pendingSelectedOption (that's the #1348
      // clarification mechanism). Suppressed when this turn also offered
      // clarification options, to avoid stacking two choice rows.
      if (turnFollowups && turnFollowups.length > 0 && !(turnOptions && turnOptions.length > 0)) {
        const followupChips = renderFollowupChips({
          prompts: turnFollowups,
          onPick(prompt) {
            // Decline (don't lock the chips) if a turn is already in flight, so
            // the user can pick again once it finishes instead of dead-ending.
            if (state.isRunning) return false;
            input.value = prompt;
            form.requestSubmit();
            return true;
          },
        });
        agentBubble.appendChild(followupChips);
      }

      // Add feedback buttons (before reference line)
      const feedbackEl = renderFeedbackButtons({
        messageId: assistantMessageId,
        sessionId: session_id,
        traceId: trace_id,
      });
      agentBubble.appendChild(feedbackEl);

      // Prepare export data using original backend formats
      const exportData = {
        charts: originalChartDataArray,
        metrics: metricDataArray,
        exports: Array.isArray(exports) ? exports : [],
      };

      // Save message with charts, citations, and export data
      state.activeConversation.messages.push({
        role: "assistant",
        content: fullResponse,
        charts: chartDataArray.length > 0 ? chartDataArray : undefined,
        citations: citationsArray.length > 0 ? citationsArray : undefined,
        quotes: quotesArray.length > 0 ? quotesArray : undefined,
        options: turnOptions && turnOptions.length > 0 ? turnOptions : undefined,
        exportData: (
          originalChartDataArray.length > 0 ||
          metricDataArray.length > 0 ||
          (Array.isArray(exports) && exports.length > 0)
        ) ? exportData : undefined,
        timestamp: Date.now(),
        session_id: session_id,
        message_id: assistantMessageId,
      });
      persistConversation(state.activeConversation);
      refreshDownloadButton();

      // Show reference number at the very end
      agentBubble.appendChild(createReferenceFooter(session_id));

      if (debugNextStepIndex < debugCaseSteps.length) {
        submitNextDebugCaseStep();
      }
    },

    onError(err) {
      state.isRunning = false;
      stopEphemeraRotation();
      updateNewConversationsState();
      refreshDownloadButton();
      loadingWrapper.remove();
      
      const errorMsg = document.createElement("div");
      
      // Special handling for rate limit errors
      if (err.error === "rate_limit") {
        errorMsg.className = "text-text-accent bg-[#1B3862] p-4 rounded-lg";
        
        let secondsLeft = err.retry_after || 60;
        
        const updateMessage = () => {
          if (secondsLeft > 0) {
            errorMsg.innerHTML = `
              <div class="flex items-center gap-2">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <circle cx="12" cy="12" r="10" stroke-width="2"/>
                  <path stroke-width="2" d="M12 6v6l4 2"/>
                </svg>
                <span><strong>Slow down!</strong> Please wait <strong>${secondsLeft}</strong> second${secondsLeft !== 1 ? 's' : ''} before asking another question.</span>
              </div>
            `;
            secondsLeft--;
            setTimeout(updateMessage, 1000);
          } else {
            errorMsg.innerHTML = `
              <div class="flex items-center gap-2 text-status-success-on-dark">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path stroke-width="2" d="M5 13l4 4L19 7"/>
                </svg>
                <span>You can ask another question now!</span>
              </div>
            `;
          }
        };

        updateMessage();
      } else {
        // Standard error handling
        errorMsg.className = "text-status-error";
        errorMsg.textContent = "Error: " + (err.message || err.error || "Something went wrong");
      }
      
      agentBubble.appendChild(errorMsg);
      
      addActivity(activityLog, "Error", err.message || err.error || "Unknown error");
    },
  });

  // SSN-84: expose a one-shot cancel closure so the Stop button can reach
  // into this turn's locals (loadingWrapper, agentBubble, ephemera timers)
  // without promoting them to module scope.
  state.cancelInFlight = () => {
    state.evtSource?.stop();
    state.isRunning = false;
    stopEphemeraRotation();
    if (loadingWrapper && loadingWrapper.parentNode) loadingWrapper.remove();
    if (agentBubble && !agentBubble.querySelector('[data-role="cancelled-marker"]')) {
      const marker = document.createElement("div");
      marker.dataset.role = "cancelled-marker";
      marker.className = "text-on-light-muted italic text-sm mt-2";
      marker.textContent = "Response cancelled";
      agentBubble.appendChild(marker);
    }
    refreshDownloadButton();
    updateNewConversationsState();
    addActivity(activityLog, "Cancelled", "User stopped generation");
    state.cancelInFlight = null;
  };
});

function updateNewConversationsState() {
  input.disabled = state.isRunning;
  if (newConversationBtn) newConversationBtn.disabled = state.isRunning;
  Array.from(submitBtns).forEach((btn) => {
    btn.disabled = state.isRunning;
    // Inline `display: none` overrides Tailwind responsive classes
    // (the desktop submit has `hidden md:block`; toggling .hidden alone
    // wouldn't beat `md:block` at desktop). Clearing the inline value lets
    // the stylesheet's responsive display win at idle.
    btn.style.display = state.isRunning ? "none" : "";
  });
  // Stop button has no responsive variants; class toggle is sufficient.
  document.querySelectorAll('[data-role="stop"]').forEach((btn) => {
    btn.classList.toggle("hidden", !state.isRunning);
  });
}

// Initialize embed messaging when in embed mode
if (isEmbedMode) {
  import("./embed.js").then(({ initEmbedMessaging }) => {
    initEmbedMessaging();
  });
}
