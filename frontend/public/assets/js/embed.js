/**
 * Compass Embed Communication Layer
 *
 * postMessage-based communication between the Compass iframe
 * and the parent nctq.org page.
 *
 * Events sent TO parent:
 *   compass:ready        — iframe loaded and ready
 *   compass:resize       — content height changed (for auto-sizing)
 *
 * Events received FROM parent:
 *   compass:prompt       — inject and optionally submit a question
 *   compass:visitor_id   — a pseudonymous, first-party visitor id from nctq.org
 *                          (WS-5 / G5). Preferred over the iframe-local fallback.
 */

const ALLOWED_ORIGIN_SUFFIXES = [".nctq.org", ".nctq.ai"];
const ALLOWED_ORIGINS_EXACT = [
  "https://nctq.org",
  "https://nctq.ai",
];

function isAllowedOrigin(origin) {
  if (ALLOWED_ORIGINS_EXACT.includes(origin)) return true;
  return ALLOWED_ORIGIN_SUFFIXES.some((suffix) => origin.endsWith(suffix));
}

// ── Visitor identity (WS-5 / G5) ───────────────────────────────────────────
// Chat is anonymous, so to count repeat users we attach a pseudonymous (no-PII)
// visitor id to each chat request. Two sources, parent preferred:
//   1) PARENT-ISSUED (robust): nctq.org generates a UUID in its OWN first-party
//      localStorage and posts it via `compass:visitor_id`. This survives
//      third-party-cookie blocking AND iframe storage-partitioning because it's
//      read in the parent's context — the correct mechanism for a cross-origin
//      iframe. Requires a small snippet on nctq.org (handed off separately).
//   2) FALLBACK (no dependency): an id minted here in the iframe's own
//      (partitioned) localStorage. Best-effort — Safari ITP, partitioning, and
//      storage-clearing make it a rate, not a precise count — but it works with
//      zero parent changes and direct (non-embedded) visits.
// Precedence: prefer the parent id once it arrives. The parent posts it after
// `compass:ready`, so an eager first turn may persist the fallback first; the
// backend reconciles (a later turn's parent id supersedes it) — see
// PostgresSessionStore._ensure_session (ON CONFLICT DO UPDATE).
const VISITOR_KEY = "pa-fe_visitor_id";
let parentVisitorId = null;

function randomId() {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
  } catch (_) {
    /* fall through to the manual generator below */
  }
  // RFC4122-ish fallback for older browsers without crypto.randomUUID.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

function ensureLocalVisitorId() {
  try {
    let id = window.localStorage.getItem(VISITOR_KEY);
    if (!id) {
      id = randomId();
      window.localStorage.setItem(VISITOR_KEY, id);
    }
    return id;
  } catch (_) {
    // localStorage can throw (privacy mode, blocked storage). Degrade to a
    // per-page-load id rather than failing the chat request.
    return randomId();
  }
}

/** The visitor id to attach to a chat request: parent-issued if present, else
 *  the iframe-local fallback. Returns a string (never null). */
export function getVisitorId() {
  return parentVisitorId || ensureLocalVisitorId();
}

let resizeObserver = null;
const RESIZE_DEBOUNCE_MS = 100;

export function initEmbedMessaging() {
  // Notify parent that Compass is ready
  postToParent("compass:ready", { version: "1.0" });

  // Listen for messages from parent
  window.addEventListener("message", handleParentMessage);

  // Notify parent of height changes so it can auto-size the iframe.
  // Debounced because Chart.js animations and streaming reflow fire dozens
  // of ResizeObserver events per second; the parent only needs the final
  // height after a settle.
  let resizeTimer = null;
  resizeObserver = new ResizeObserver(() => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      resizeTimer = null;
      postToParent("compass:resize", {
        height: document.documentElement.scrollHeight,
      });
    }, RESIZE_DEBOUNCE_MS);
  });
  resizeObserver.observe(document.body);
}

function handleParentMessage(event) {
  if (!isAllowedOrigin(event.origin)) return;

  const { type, payload } = event.data || {};

  switch (type) {
    case "compass:prompt":
      injectPrompt(payload?.text, payload?.autoSubmit);
      break;
    case "compass:visitor_id":
      // Parent-issued first-party id wins over the iframe-local fallback.
      if (payload?.visitor_id) {
        parentVisitorId = String(payload.visitor_id).slice(0, 128);
      }
      break;
  }
}

function injectPrompt(text, autoSubmit = false) {
  if (!text) return;
  const input = document.getElementById("input");
  const form = document.getElementById("form");
  if (!input || !form) return;

  input.value = text;
  input.dispatchEvent(new Event("input", { bubbles: true }));

  if (autoSubmit) {
    setTimeout(() => {
      form.dispatchEvent(
        new Event("submit", { bubbles: true, cancelable: true })
      );
    }, 100);
  }
}

function postToParent(type, payload) {
  if (window.parent === window) return; // not in an iframe
  window.parent.postMessage({ type, payload }, "*");
}
