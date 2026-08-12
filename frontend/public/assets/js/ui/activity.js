import { applyFlyoutA11y } from "./flyoutA11y.js";

export function addActivity(container, toolName, message, status = "complete") {
  // Embed mode gates out #activityLog (it lives in right-sidebar.php), but
  // app.js's SSE turn handlers call addActivity unconditionally. Bail when the
  // log is absent so a turn can't crash mid-stream on the public embed. (Same
  // embed-gated null class as setProgress / renderConversationList — audit
  // 2026-06-22.)
  if (!container) return;

  // Main wrapper
  const logMainWrapper = document.createElement("div");
  logMainWrapper.className = "flex gap-3 group relative";

  // Timeline connector line (optional, only if needed, you can modify later)
  // For example, you can append a line if this isn't the last element

  // Icon wrapper
  const iconWrapper = document.createElement("div");
  iconWrapper.className = "flex-shrink-0 mt-0.5 relative z-10";

  const iconDiv = document.createElement("div");
  iconDiv.className = "w-5 h-5 rounded-full flex items-center justify-center";

  // Set icon based on status
  if (status === "complete") {
    iconDiv.innerHTML = `
    <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#0089b3" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-circle-check-big text-[#0089b3]" aria-hidden="true"><path d="M21.801 10A10 10 0 1 1 17 3.335"></path><path d="m9 11 3 3L22 4"></path></svg>
    `;
  } else if (status === "running") {
    // running state shows no icon glyph (the row text conveys progress)
  } else if (status === "error") {
    iconDiv.classList.add("bg-red-500");
  } else {
    iconDiv.classList.add("border-2", "border-slate-300", "bg-white");
  }

  iconWrapper.appendChild(iconDiv);
  logMainWrapper.appendChild(iconWrapper);

  // Text wrapper
  const textWrapper = document.createElement("div");
  textWrapper.className = "flex-1 pb-2";

  const toolNameDiv = document.createElement("div");
  toolNameDiv.className = `font-semibold text-sm ${
    status === "running" || status === "complete"
      ? "text-[#003057]"
      : "text-on-light-muted"
  }`;
  toolNameDiv.textContent = toolName;

  textWrapper.appendChild(toolNameDiv);

  if (status === "running" || status === "complete") {
    const toolDescriptionDiv = document.createElement("div");
    toolDescriptionDiv.className = `mt-1 text-xs leading-relaxed ${
      status === "running" ? "text-[#003057]/80" : "text-slate-500"
    }`;
    toolDescriptionDiv.textContent = message;
    textWrapper.appendChild(toolDescriptionDiv);
  }

  logMainWrapper.appendChild(textWrapper);

  // Append to container
  container.appendChild(logMainWrapper);
  container.scrollTop = container.scrollHeight; // auto scroll
}

export function toggleActivityLogSidebar(panel, backdrop) {
  if (!panel) return;

  const isOpen = panel.classList.contains("min-w-80");
  const willOpen = !isOpen;

  panel.classList.toggle("min-w-80", willOpen);
  panel.classList.toggle("w-0", isOpen);

  // Mirror the open state on <body> so the top-corner nav's right edge can
  // slide inboard via CSS — otherwise the 3 right-side icons sit on top of
  // the open sidebar at right:20px. See `.top-corner-nav` rules in input.css.
  document.body.classList.toggle("activity-log-open", willOpen);

  if (backdrop) {
    backdrop.classList.toggle("hidden", isOpen);
  }

  applyFlyoutA11y(panel, willOpen, ".toggleActivityLogBtn");
}

export function resetActivtyLogs(container) {
  if (!container) return; // #activityLog is gated out in embed mode (see addActivity)

  // Remove all child elements
  container.innerHTML = "";

  // Optionally, reset scroll
  container.scrollTop = 0;
}

export function resetProgressBar() {
  setProgress(null);
}

const progressConfig = {
  pending: 20,
  running: 60,
  complete: 100,
};

// Initialize to 0%

export function setProgress(status) {
  const statusLabel = document.getElementById("statusLabel");
  const progressPercent = document.getElementById("progressPercent");
  const progressBar = document.getElementById("progressBar");

  // The progress UI lives in right-sidebar.php, which index.php omits in embed
  // mode (`<?php if (!$isEmbed): ?>`). app.js loads unconditionally, so without
  // this guard the module-level setProgress(null) below dereferences null and
  // throws at import time — aborting the whole app.js module graph and leaving
  // the public ?embed=true iframe as dead JS (renders, but nothing is wired).
  if (!statusLabel || !progressPercent || !progressBar) return;

  const percentage = status ? progressConfig[status] ?? 0 : 0;

  statusLabel.textContent = status
    ? status.charAt(0).toUpperCase() + status.slice(1)
    : "Not started";

  progressPercent.textContent = `${percentage}%`;
  progressBar.style.width = `${percentage}%`;

  // Color handling
  progressBar.classList.toggle("bg-emerald-500", status === "complete");
  progressBar.classList.toggle("bg-[#0089b3]", status !== "complete");
}

setProgress(null);
