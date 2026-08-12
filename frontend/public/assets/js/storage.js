const STORAGE_KEY = "pa-fe_conversations";

/**
 * Load all conversations from localStorage
 * @returns {Array} Array of conversation objects
 */
export function loadConversations() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
  } catch {
    return [];
  }
}

/**
 * Save all conversations to localStorage
 * @param {Array} convs - Array of conversation objects
 */
export function saveConversations(convs) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(convs));
  } catch {
    // Silently ignore storage errors (private browsing, quota exceeded, etc.)
  }
}

/**
 * Add a new conversation
 * @param {Object} conv - Conversation object {id, title, messages, createdAt}
 */
export function addConversation(conv) {
  const convs = loadConversations();
  convs.unshift(conv); // add to the beginning
  saveConversations(convs);
}

/**
 * Update an existing conversation
 * @param {string} id - ID of the conversation to update
 * @param {Object} updatedData - Partial data to update {title?, messages?}
 */
export function updateConversation(id, updatedData) {
  const convs = loadConversations();
  const index = convs.findIndex(c => c.id === id);

  if (index === -1) return false; // not found

  convs[index] = {
    ...convs[index],
    ...updatedData,
  };

  saveConversations(convs);
  return true;
}

/**
 * Delete a conversation by ID
 * @param {string} id - ID of the conversation to delete
 */
export function deleteConversation(id) {
  const convs = loadConversations();
  const filtered = convs.filter(c => c.id !== id);

  saveConversations(filtered);
  return true;
}

/**
 * Get a conversation by ID
 * @param {string} id - ID of the conversation
 * @returns {Object|null} Conversation object or null if not found
 */
export function getConversationById(id) {
  const convs = loadConversations();
  return convs.find(c => c.id === id) || null;
}
