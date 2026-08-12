import { loadConversations, saveConversations } from "./storage.js";

export function createConversation() {
  return {
    id: `conv_${Date.now()}`,
    title: "New conversation",
    session_id: null,
    messages: [],
    createdAt: Date.now(),
  };
}

export function persistConversation(conv) {
  const convs = loadConversations();
  const idx = convs.findIndex(c => c.id === conv.id);

  if (idx === -1) convs.unshift(conv);
  else convs[idx] = conv;
  saveConversations(convs);
}

export function setTitleIfEmpty(conv, text) {
  if (conv.messages.length !== 0) return;

  // Save the FULL title (no truncation)
  // Truncation will happen in UI display, not in storage
  conv.title = text.trim();
}

/**
 * Update the browser URL to reflect the current conversation.
 * Uses ?session=UUID so the URL is shareable/bookmarkable.
 */
export function updateUrl(sessionId) {
  if (sessionId) {
    const url = new URL(window.location);
    url.searchParams.set("session", sessionId);
    window.history.replaceState({}, "", url);
  } else {
    // Clear session from URL (new conversation or no session yet)
    const url = new URL(window.location);
    url.searchParams.delete("session");
    window.history.replaceState({}, "", url);
  }
}

/**
 * Get session ID from the current URL, if present.
 */
export function getSessionFromUrl() {
  const url = new URL(window.location);
  return url.searchParams.get("session");
}
