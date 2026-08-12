import { setProgress } from "./ui/activity.js";
import { shouldSuppressDataEvent } from "./utils/ssePayloadGuards.js";
import { getVisitorId } from "./embed.js";

export function startAgent({
  question,
  sessionId,
  debug = false,
  onTrace,
  onDelta,
  onStageStart,
  onStageEnd,
  onToolCallStart,
  onToolCallEnd,
  onChart,
  onData,
  onCitations,
  onQuotes,
  onStatus,
  onEphemera,
  onEphemeraBatch,
  onGlossary,
  onCriticVerdict,
  onOptions,
  onFollowups,
  onFinish,
  onError,
  // #1348: when the user clicks a structured clarification option, the
  // option's machine handle is posted as `selected_option` so the backend
  // resumes deterministically. Null on every normal (typed) turn.
  selectedOption = null,
}) {
  const controller = new AbortController();
  // Captured from the trace event so we can resync by session_id if the
  // stream drops before `done`. Falls back to the caller-provided sessionId.
  let capturedSessionId = sessionId || null;
  let capturedTraceId = null;
  let sawDone = false;

  fetch("/api/stream.php", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message: question,
      session_id: sessionId || null,
      ...(selectedOption ? { selected_option: selectedOption } : {}),
      // WS-5 / G5: pseudonymous, PII-free visitor id so the dashboard can count
      // repeat users (chat is otherwise anonymous). Parent-issued when embedded
      // in nctq.org, else an iframe-local fallback. Always a string.
      visitor_id: getVisitorId(),
    }),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`Chat failed with status ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        buffer = buffer.replace(/\r\n/g, "\n");

        const frames = buffer.split("\n\n");
        buffer = frames.pop(); // keep leftover incomplete frame

        for (const frame of frames) {
          parseSSE(frame);
        }
      }

      // Process any remaining buffer
      if (buffer.trim()) {
        parseSSE(buffer);
      }

      // Reader signalled stream end. If the backend never emitted a `done`
      // SSE event, the connection was cut somewhere upstream even though
      // the response may already be persisted. Try to recover from DB; if
      // that can't produce an answer, surface a terminal error so the UI
      // leaves the loading state instead of hanging on "Understanding…"
      // forever (issue #1352: an early/cold turn cancelled before the
      // `trace` event leaves capturedSessionId null and nothing persisted,
      // so resync no-ops and the spinner would otherwise never clear).
      if (!sawDone) {
        const recovered = await resyncFromDb(capturedSessionId);
        if (!recovered) {
          onError?.({
            error: "stream_truncated",
            message:
              "This turn was interrupted before completing. Please ask again.",
          });
        }
      }
    })
    .catch((error) => {
      // Ignore AbortError — it's self-inflicted by controller.abort() in the error handler
      if (error.name === "AbortError") return;
      console.error("Agent SSE error:", error);
      onError?.({ error: error.message || "Connection failed" });
    });

  // Returns true if a persisted answer was recovered and rendered, false
  // otherwise — the caller surfaces a terminal error when recovery fails so
  // the loading state is always torn down (issue #1352).
  async function resyncFromDb(sid) {
    if (!sid) return false;
    try {
      const resp = await fetch(`/api/conversation.php?session_id=${encodeURIComponent(sid)}`);
      if (!resp.ok) return false;
      const conv = await resp.json();
      const messages = Array.isArray(conv?.messages) ? conv.messages : [];
      const lastAssistant = [...messages].reverse().find((m) => m?.role === "assistant");
      if (!lastAssistant?.content) return false;
      console.warn("[SSE resync] Stream ended without 'done'; rendering persisted response.");
      onDelta?.(lastAssistant.content);
      setProgress("complete");
      onFinish?.({
        session_id: sid,
        trace_id: capturedTraceId,
        phase: "complete",
        recovered: true,
      });
      return true;
    } catch (err) {
      console.error("[SSE resync] Failed:", err);
      return false;
    }
  }

  function parseSSE(frame) {
    let event = "message";
    let data = "";

    frame.split("\n").forEach((line) => {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) data += line.slice(5).trim();
    });

    if (!data) return;

    let payload;
    try {
      payload = JSON.parse(data);
    } catch (err) {
      console.error("Failed to parse SSE JSON:", data, err);
      return;
    }

    switch (event) {
      case "trace":
        if (payload.session_id) capturedSessionId = payload.session_id;
        if (payload.trace_id) capturedTraceId = payload.trace_id;
        onTrace?.(payload.trace_id);
        setProgress("pending");
        break;
      case "text":
        setProgress("running");
        onDelta?.(payload.content);
        break;
      case "status":
        setProgress("running");
        onStatus?.(payload.message);
        break;
      case "ephemera":
        // Legacy per-card event — kept for one deploy cycle so a rolled-back
        // backend doesn't leave the UI silent. New backend emits ephemera_batch.
        onEphemera?.(payload);
        break;
      case "ephemera_batch":
        onEphemeraBatch?.(Array.isArray(payload.cards) ? payload.cards : []);
        break;
      case "stage_start":
        // The stage label (payload.message) is surfaced by the activity
        // timeline (ui/activityIndicator.js via app.js), so we deliberately
        // do NOT forward it to onStatus — the old crossfade status text was
        // removed with the phase indicator.
        setProgress("running");
        onStageStart?.(payload);
        break;
      case "stage_end":
        onStageEnd?.(payload);
        break;
      case "critic_verdict":
        onCriticVerdict?.(payload);
        break;
      case "glossary":
        onGlossary?.(payload.terms);
        break;
      case "tool_call_start":
        onToolCallStart?.(payload);
        break;
      case "tool_call_end":
        onToolCallEnd?.(payload);
        break;
      case "chart":
        onChart?.(payload);
        break;
      case "data":
        if (!shouldSuppressDataEvent(payload)) {
          onData?.(payload);
        }
        break;
      case "citations":
        onCitations?.(payload);
        break;
      case "quotes":
        onQuotes?.(payload.quotes);
        break;
      case "options":
        // #1348: structured, clickable clarification choices.
        onOptions?.(payload);
        break;
      case "followups":
        // #1555: suggested follow-up prompts (e.g. "Show me this as a bar
        // chart") offered after the answer. Payload is {"prompts": [...]}.
        if (Array.isArray(payload.prompts) && payload.prompts.length > 0) {
          onFollowups?.(payload.prompts);
        }
        break;
      case "done":
        sawDone = true;
        if (payload?.session_id) capturedSessionId = payload.session_id;
        if (payload?.trace_id) capturedTraceId = payload.trace_id;
        setProgress("complete");
        onFinish?.({
          ...payload,
          trace_id: payload?.trace_id || capturedTraceId,
        });
        break;
      case "error":
        setProgress(null);
        onError?.(payload);
        controller.abort();
        break;
      default:
        break;
    }
  }

  return { stop: () => controller.abort() };
}
