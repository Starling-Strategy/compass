import { state } from "../state.js";
import { renderSelectedChat } from "./chat.js";
import { applyFlyoutA11y } from "./flyoutA11y.js";

/**
 * Truncate text for display (keeps full text in storage)
 * @param {string} text - Full text
 * @param {number} maxLength - Maximum length before truncation
 * @returns {string} Truncated text
 */
function truncateTitle(text, maxLength = 30) {
  if (text.length <= maxLength) {
    return text;
  }

  // Cut at max length
  let truncated = text.slice(0, maxLength);

  // Remove partial word at the end
  truncated = truncated.replace(/\s+\S*$/, '');

  return truncated;
}

export function renderConversationList(
  container,
  conversations,
  {
    activeConversationId,
    onRename,
    onDelete
  }
) {
  // In embed mode the left sidebar (and its #conversationList) is gated out of
  // the DOM (index.php `<?php if (!$isEmbed)`), but app.js still calls
  // refreshSidebar() unconditionally. Bail out when the container is absent so
  // this can't throw and abort app.js on the public embed. (Same embed-gated
  // class as the setProgress null-guard in activity.js — audit 2026-06-22.)
  if (!container) return;

  container.innerHTML = "";

  // Bug G (Ashley 2026-04-30): hamburger menu opened but the panel
  // appeared blank because zero conversations rendered to zero DOM
  // elements. Show a friendly empty-state instead.
  if (!conversations || conversations.length === 0) {
    const empty = document.createElement("div");
    empty.className = "text-xs text-on-dark-muted px-3 py-4";
    empty.textContent = "No conversations yet. Start a chat to see it here.";
    container.appendChild(empty);
    return;
  }

  conversations.forEach((conv) => {
    const item = document.createElement("div");

    item.className = `
      group flex items-center justify-between px-3 py-2.5 
      transition-colors cursor-pointer
      ${conv.id === activeConversationId
        ? "bg-[#004a7c]"
        : "hover:bg-[#004a7c]/60"}
    `.trim();

    item.onclick = () => {
      if (state.isRunning) return;
      renderSelectedChat(conv.id);
    };

    /* LEFT: title + date */
    const left = document.createElement("div");
    left.className = "flex flex-col flex-1 min-w-0";

    const title = document.createElement("div");
    title.className = "font-medium text-sm truncate text-white conversation-title";
    title.textContent = truncateTitle(conv.title); // Display truncated version
    
    // Add tooltip data attribute
    title.setAttribute('data-tooltip', conv.title);
    
    // Check if truncated after render and add class for tooltip
    setTimeout(() => {
      // if (isTextTruncated(title) || conv.title.length > 30) {
      //   title.classList.add('has-tooltip');
      // }
       if (conv.title.length > 30) {
        title.classList.add('has-tooltip');
      }
    }, 0);

    const date = document.createElement("div");
    date.className = "text-xs text-on-dark-muted mt-0.5";
    date.textContent = new Date(conv.createdAt).toLocaleDateString();

    left.append(title, date);

    /* RIGHT: actions */
    const actions = document.createElement("div");
    actions.className =
      "flex-shrink-0 flex items-center gap-1 opacity-1 md:opacity-0 group-hover:opacity-100 transition-opacity duration-200";

    // Rename button
    const renameBtn = document.createElement("button");
    renameBtn.className = "p-1 hover:bg-[#006d8f]/40 rounded transition-colors";
    renameBtn.title = "Rename conversation";
    renameBtn.innerHTML = `<img src="/assets/icons/pen.svg" alt="Rename" class="w-4 h-4" />`;

    renameBtn.onclick = (e) => {
      e.stopPropagation();
      const newTitle = prompt("Rename conversation:", conv.title);
      if (newTitle) onRename(conv.id, newTitle);
    };

    // Delete button
    const deleteBtn = document.createElement("button");
    deleteBtn.className = "p-1 hover:bg-red-600/50 rounded transition-colors";
    deleteBtn.title = "Delete conversation";
    deleteBtn.innerHTML = `<img src="/assets/icons/trash.svg" alt="Delete" class="w-4 h-4" />`;

    deleteBtn.onclick = (e) => {
      e.stopPropagation();
      if (confirm("Delete this conversation?")) {
        onDelete(conv.id);
      }
    };

    actions.append(renameBtn, deleteBtn);
    item.append(left, actions);
    container.appendChild(item);
  });
}


export function toggleLeftSidebar(panel, backdrop) {
  if (!panel) return;

  const isOpen = panel.classList.contains("min-w-80");
  const willOpen = !isOpen;

  panel.classList.toggle("min-w-80", willOpen);
  panel.classList.toggle("w-0", isOpen);

  if (backdrop) {
    backdrop.classList.toggle("hidden", isOpen);
  }

  applyFlyoutA11y(panel, willOpen, ".toggleLeftSidebarBtn");
}