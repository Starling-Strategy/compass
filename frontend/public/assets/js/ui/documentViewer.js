/**
 * Document Viewer Modal
 * Shows full document text with the quoted passage highlighted.
 * Supports character-level highlighting via char_start/char_end positions.
 */

const MODAL_ID = "doc-viewer-modal";

/**
 * Open the document viewer modal.
 * @param {string} docId - UUID of the document
 * @param {string} excerptText - The excerpt to highlight
 * @param {string|null} documentUrl - URL to source PDF for download
 * @param {number|null} charStart - Character start position in full text
 * @param {number|null} charEnd - Character end position in full text
 */
export async function openDocumentViewer(docId, excerptText, documentUrl, charStart, charEnd) {
  // Remove any existing modal
  closeModal();

  const overlay = createOverlay();
  const modal = createModalShell();
  overlay.appendChild(modal);
  document.body.appendChild(overlay);

  // Show loading state
  const content = modal.querySelector(".doc-viewer-body");
  content.innerHTML = '<div class="doc-viewer-loading">Loading document...</div>';

  try {
    const response = await fetch(`/api/document.php?id=${encodeURIComponent(docId)}`);
    if (!response.ok) throw new Error(`Document not found (${response.status})`);
    const doc = await response.json();

    renderHeader(modal, doc, documentUrl);
    renderContent(content, doc.full_text, excerptText, charStart, charEnd);
  } catch (error) {
    content.innerHTML = `
      <div class="doc-viewer-error">
        <p>Could not load document text.</p>
        ${documentUrl ? `<a href="${escapeAttr(documentUrl)}" target="_blank" rel="noopener noreferrer" class="doc-download-btn">Download source PDF instead</a>` : ""}
      </div>
    `;
  }
}

function createOverlay() {
  const overlay = document.createElement("div");
  overlay.id = MODAL_ID;
  overlay.className = "doc-viewer-overlay";
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeModal();
  });
  // Close on Escape
  const handler = (e) => {
    if (e.key === "Escape") {
      closeModal();
      document.removeEventListener("keydown", handler);
    }
  };
  document.addEventListener("keydown", handler);
  return overlay;
}

function createModalShell() {
  const modal = document.createElement("div");
  modal.className = "doc-viewer-modal";
  modal.innerHTML = `
    <div class="doc-viewer-header">
      <div class="doc-viewer-title-area">
        <h3 class="doc-viewer-title">Loading...</h3>
        <span class="doc-viewer-badge"></span>
      </div>
      <div class="doc-viewer-actions">
        <button class="doc-viewer-close" title="Close">&times;</button>
      </div>
    </div>
    <div class="doc-viewer-body"></div>
  `;
  modal.querySelector(".doc-viewer-close").addEventListener("click", closeModal);
  return modal;
}

function renderHeader(modal, doc, documentUrl) {
  const title = modal.querySelector(".doc-viewer-title");
  const badge = modal.querySelector(".doc-viewer-badge");
  const actions = modal.querySelector(".doc-viewer-actions");

  title.textContent = doc.title || "Document";

  if (doc.document_type) {
    badge.textContent = doc.document_type.replace(/_/g, " ");
    badge.style.display = "inline-block";
  }

  // Add PDF download button
  const pdfUrl = documentUrl || doc.src_link;
  if (pdfUrl) {
    const downloadBtn = document.createElement("a");
    downloadBtn.href = pdfUrl;
    downloadBtn.target = "_blank";
    downloadBtn.rel = "noopener noreferrer";
    downloadBtn.className = "doc-download-btn";
    downloadBtn.innerHTML = `
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="7 10 12 15 17 10"></polyline>
        <line x1="12" y1="15" x2="12" y2="3"></line>
      </svg>
      Download PDF
    `;
    actions.insertBefore(downloadBtn, actions.firstChild);
  }
}

function renderContent(container, fullText, excerptText, charStart, charEnd) {
  if (!fullText) {
    container.innerHTML = '<div class="doc-viewer-error">No document text available.</div>';
    return;
  }

  // Try character-position highlighting first (most precise)
  if (charStart != null && charEnd != null && charStart >= 0 && charEnd > charStart) {
    const before = escapeHtml(fullText.substring(0, charStart));
    const highlighted = escapeHtml(fullText.substring(charStart, charEnd));
    const after = escapeHtml(fullText.substring(charEnd));
    container.innerHTML = `<div class="doc-viewer-text">${before}<mark class="doc-highlight">${highlighted}</mark>${after}</div>`;
    scrollToHighlight(container);
    return;
  }

  // Fallback: string search for excerpt in full text
  if (excerptText) {
    const idx = fullText.indexOf(excerptText);
    if (idx >= 0) {
      const before = escapeHtml(fullText.substring(0, idx));
      const highlighted = escapeHtml(fullText.substring(idx, idx + excerptText.length));
      const after = escapeHtml(fullText.substring(idx + excerptText.length));
      container.innerHTML = `<div class="doc-viewer-text">${before}<mark class="doc-highlight">${highlighted}</mark>${after}</div>`;
      scrollToHighlight(container);
      return;
    }

    // Last fallback: show excerpt callout above full text
    container.innerHTML = `
      <div class="doc-viewer-excerpt-callout">
        <strong>Referenced passage:</strong>
        <blockquote>"${escapeHtml(excerptText)}"</blockquote>
      </div>
      <div class="doc-viewer-text">${escapeHtml(fullText)}</div>
    `;
    return;
  }

  // No excerpt to highlight — just show full text
  container.innerHTML = `<div class="doc-viewer-text">${escapeHtml(fullText)}</div>`;
}

function scrollToHighlight(container) {
  setTimeout(() => {
    const mark = container.querySelector(".doc-highlight");
    if (mark) mark.scrollIntoView({ behavior: "smooth", block: "center" });
  }, 100);
}

function closeModal() {
  const existing = document.getElementById(MODAL_ID);
  if (existing) existing.remove();
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function escapeAttr(str) {
  return str.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
