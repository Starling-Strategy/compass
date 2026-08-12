/**
 * Sources and Citations rendering
 * Handles displaying sources panel and making inline citations clickable
 */

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Escape a value for safe interpolation into a double-quoted HTML attribute.
 * escapeHtml (textContent round-trip) encodes < > & but NOT the double-quote
 * that would close an attribute, so attribute-context sinks (e.g. an href built
 * via innerHTML) need this stronger pass. See audit security-citation-href-xss.
 */
function escapeAttr(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

/**
 * Sanitize URL to prevent javascript: and other malicious URLs
 */
function sanitizeUrl(url) {
  if (!url) return '';
  try {
    const parsed = new URL(url);
    if (['http:', 'https:'].includes(parsed.protocol)) {
      // Return the constructor-normalized href, NOT the raw input: the URL
      // constructor percent-encodes " < > and spaces, so a scraped/poisoned
      // citation URL like `https://x/" onmouseover=...` can't break out of the
      // href attribute at the innerHTML sink in createSourceItem.
      return parsed.href;
    }
  } catch (e) {
    // Invalid URL
  }
  return '';
}

export function citationDisplayTitle(citation) {
  if (!citation || typeof citation !== 'object') return 'Source';
  const explicit = citation.title || citation.document_name || citation.document;
  if (explicit && String(explicit).trim()) return String(explicit).trim();
  const safeUrl = sanitizeUrl(citation.url);
  if (safeUrl) {
    try {
      return new URL(safeUrl).hostname.replace(/^www\./, '');
    } catch (e) {
      // sanitizeUrl already validated; fall through to document type.
    }
  }
  if (citation.document_type && String(citation.document_type).trim()) {
    return String(citation.document_type).replace(/_/g, ' ').trim();
  }
  if (citation.source_kind && String(citation.source_kind).trim()) {
    return String(citation.source_kind).replace(/_/g, ' ').trim();
  }
  return 'Source';
}

/**
 * Render the sources panel
 * @param {Array} citations - Array of citation objects from backend
 * @returns {HTMLElement} - Sources panel element
 */
export function renderSourcesPanel(citations) {
  if (!citations || citations.length === 0) return null;

  const panel = document.createElement('div');
  panel.className = 'sources-panel mt-6 pt-4 border-t border-[#DCE2EA]';

  // Header with count
  const header = document.createElement('div');
  header.className = 'sources-header flex items-center justify-between mb-4 cursor-pointer select-none';
  header.innerHTML = `
    <div class="flex items-center gap-2">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
        <polyline points="14 2 14 8 20 8"></polyline>
        <line x1="16" y1="13" x2="8" y2="13"></line>
        <line x1="16" y1="17" x2="8" y2="17"></line>
      </svg>
      <span class="text-sm font-semibold text-white">Sources</span>
      <span class="text-xs text-on-light-secondary bg-[#DCE2EA] px-2 py-0.5 rounded-full">${citations.length}</span>
    </div>
    <svg class="sources-toggle-icon transition-transform duration-200" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#B6C5DD" stroke-width="2">
      <polyline points="6 9 12 15 18 9"></polyline>
    </svg>
  `;
  panel.appendChild(header);

  // Source items container
  const sourcesList = document.createElement('div');
  sourcesList.className = 'sources-list space-y-2';

  citations.forEach((c, index) => {
    const displayIndex = c.id || (index + 1);
    const sourceItem = createSourceItem(c, displayIndex);
    sourcesList.appendChild(sourceItem);
  });

  panel.appendChild(sourcesList);

  // Toggle functionality. #1228: start COLLAPSED to save vertical space — an
  // 80+ source list otherwise dominates the answer. A citation-marker click
  // re-expands the panel via processCitationLinks (it sets display directly).
  // Derive open/closed from the DOM at click time rather than a closure flag,
  // so the header toggle and the citation re-expand path can never disagree
  // (a closure flag would desync after a citation-driven expand, forcing a
  // double-click to collapse).
  sourcesList.style.display = 'none';
  const initialIcon = header.querySelector('.sources-toggle-icon');
  if (initialIcon) {
    initialIcon.style.transform = 'rotate(180deg)';
  }
  header.addEventListener('click', () => {
    const nowVisible = sourcesList.style.display !== 'none';
    sourcesList.style.display = nowVisible ? 'none' : 'block';
    const icon = header.querySelector('.sources-toggle-icon');
    if (icon) {
      icon.style.transform = nowVisible ? 'rotate(180deg)' : 'rotate(0deg)';
    }
  });

  return panel;
}

/**
 * Create a single source item
 */
function createSourceItem(citation, displayIndex) {
  const isPublication = citation.document_type === 'nctq_publication';
  const isBlobPdf = citation.url && citation.url.includes('blob.core.windows.net');
  const isNces = citation.url && citation.url.includes('nces.ed.gov');
  const isWebsite = citation.url && !isBlobPdf && !isPublication && !isNces;

  const item = document.createElement('div');
  item.className = 'source-item source-item-collapsed cursor-pointer flex gap-3 p-3 bg-[#F8F9FB] rounded-lg border border-transparent hover:border-[#4A91D0] hover:bg-white transition-all duration-200';
  item.id = `source-${displayIndex}`;
  item.setAttribute('data-source-id', displayIndex);

  // Source number badge
  const numberEl = document.createElement('div');
  numberEl.className = 'source-number flex-shrink-0 w-7 h-7 rounded-full bg-primary text-white text-xs font-bold flex items-center justify-center shadow-sm';
  numberEl.textContent = displayIndex;
  item.appendChild(numberEl);

  // Source content
  const content = document.createElement('div');
  content.className = 'source-content flex-1 min-w-0';

  // Title row with badge
  const titleWrapper = document.createElement('div');
  titleWrapper.className = 'source-title flex items-start gap-2 mb-1 flex-wrap';

  const documentName = escapeHtml(citationDisplayTitle(citation));
  let safeUrl = sanitizeUrl(citation.url);

  // Append #page=N for PDF deep-linking (FIX-85)
  let linkUrl = safeUrl;
  if (safeUrl && (citation.page_number || citation.page)) {
    try {
      const parsed = new URL(safeUrl);
      if (parsed.pathname.toLowerCase().endsWith('.pdf')) {
        parsed.hash = `page=${citation.page_number || citation.page}`;
        linkUrl = parsed.toString();
      }
    } catch (e) {
      // sanitizeUrl already validated — safe fallback
    }
  }

  const titleContainer = document.createElement('span');
  titleContainer.className = 'flex-1 min-w-0';

  if (safeUrl) {
    const linkClass = isWebsite
      ? 'break-words text-sm font-medium text-[#5A6A7A] hover:text-[#4A91D0] hover:underline inline-flex items-center gap-1'
      : 'break-words text-sm font-medium text-[#1B3862] hover:text-[#4A91D0] hover:underline inline-flex items-center gap-1';
    titleContainer.innerHTML = `
      <a href="${escapeAttr(linkUrl)}" target="_blank" rel="noopener noreferrer" class="${linkClass}">
        ${documentName}
        <svg class="w-3 h-3 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
          <polyline points="15 3 21 3 21 9"></polyline>
          <line x1="10" y1="14" x2="21" y2="3"></line>
        </svg>
      </a>
    `;
  } else {
    titleContainer.innerHTML = `<span class="text-sm font-medium text-[#0F223D]">${documentName}</span>`;
  }
  titleWrapper.appendChild(titleContainer);

  // Type badge — distinguish PDF sources, website references, and publications
  const badge = document.createElement('span');
  let badgeColor, badgeLabel;
  if (isPublication) {
    badgeColor = 'bg-[#475569] text-white';    // slate-600, 7.3:1 on white text
    badgeLabel = 'NCTQ Publication';
  } else if (isWebsite) {
    badgeColor = 'bg-[#E8EDF2] text-[#414754]'; // on-light-secondary, 8.5:1 on chip bg
    badgeLabel = 'District Website';
  } else {
    badgeColor = 'bg-[#1a73e8] text-white';    // primary_container, 4.8:1 white text
    // Use actual document type if available (FIX-45)
    badgeLabel = citation.document_type
      ? citation.document_type.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
      : 'Source Document';
  }
  badge.className = `source-badge flex-shrink-0 px-2 py-0.5 text-xs font-medium rounded-full ${badgeColor}`;
  badge.textContent = badgeLabel;
  titleWrapper.appendChild(badge);

  const pageValue = citation.page_number || citation.page;
  if (pageValue) {
    const pagePill = document.createElement('span');
    pagePill.className = 'source-page-pill flex-shrink-0 px-2 py-0.5 text-xs font-semibold rounded-full bg-[#E8EDF2] text-[#1B3862]';
    pagePill.textContent = `p. ${pageValue}`;
    titleWrapper.appendChild(pagePill);
  }

  content.appendChild(titleWrapper);

  // Body wrapper — toggled by .source-item-collapsed
  const body = document.createElement('div');
  body.className = 'source-item-body';

  // Metadata line
  const metaParts = [];
  if (citation.academic_year) {
    metaParts.push(citation.academic_year);
  }
  if (citation.page_ref && !citation.page_number) {
    metaParts.push(citation.page_ref);
  }
  if (citation.document_type) {
    metaParts.push(citation.document_type.replace(/_/g, ' '));
  }

  if (metaParts.length > 0) {
    const metaEl = document.createElement('div');
    metaEl.className = 'source-meta text-xs text-on-light-muted mb-2';
    metaEl.textContent = metaParts.join(' • ');
    body.appendChild(metaEl);
  }

  // Excerpt — field absent from CitationRef; guard so dead reads are harmless if future schemas add it
  if (citation.excerpt && typeof citation.excerpt === 'string') {
    const excerptEl = document.createElement('div');
    excerptEl.className = 'source-excerpt text-xs text-[#5A6A7A] leading-relaxed line-clamp-3';
    const truncatedExcerpt = citation.excerpt.length > 400
      ? citation.excerpt.substring(0, 400) + '...'
      : citation.excerpt;
    excerptEl.textContent = `"${truncatedExcerpt}"`;
    body.appendChild(excerptEl);
  }

  content.appendChild(body);
  item.appendChild(content);

  // Chevron — sibling of .source-content so it doesn't compete with title flex space at narrow widths.
  // Rotates via .source-item-collapsed in CSS.
  const chevron = document.createElement('span');
  chevron.className = 'source-item-chevron flex-shrink-0 self-start mt-1 text-[#94A3B8]';
  chevron.innerHTML = `
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <polyline points="6 9 12 15 18 9"></polyline>
    </svg>
  `;
  item.appendChild(chevron);

  // Row toggle — click anywhere outside the title link / view-doc button to expand/collapse.
  item.setAttribute('role', 'button');
  item.setAttribute('tabindex', '0');
  item.addEventListener('click', (e) => {
    if (e.target.closest('a, button')) return;
    item.classList.toggle('source-item-collapsed');
  });
  item.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      item.click();
    }
  });

  return item;
}

/**
 * Renumber citations to be sequential [1],[2],[3] with no gaps (FIX-92).
 * Mutates the citations array and rewrites [N] markers in the container's text.
 * Must be called BEFORE processCitationLinks.
 * @param {HTMLElement} container - Container with rendered text
 * @param {Array} citations - Mutable citations array
 */
export function renumberCitations(container, citations) {
  if (!citations || citations.length === 0) return;

  // Sort by original ID
  citations.sort((a, b) => (a.id || 0) - (b.id || 0));

  // Build old→new map
  const remap = new Map();
  citations.forEach((c, i) => {
    const newId = i + 1;
    if (c.id !== newId) {
      remap.set(c.id, newId);
    }
    c.id = newId;
  });

  if (remap.size === 0) return; // Already sequential

  // Rewrite [N] markers in rendered HTML text
  // Use a two-pass approach: first replace with placeholders, then with final IDs
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null, false);
  const textNodes = [];
  let node;
  while (node = walker.nextNode()) {
    if (/\[\d+\]/.test(node.textContent)) {
      textNodes.push(node);
    }
  }

  textNodes.forEach(textNode => {
    // Replace all [oldId] with [newId] using placeholder to avoid collisions
    let text = textNode.textContent;
    for (const [oldId, newId] of remap) {
      text = text.replaceAll(`[${oldId}]`, `[__CIT_${newId}__]`);
    }
    text = text.replace(/\[__CIT_(\d+)__\]/g, '[$1]');
    textNode.textContent = text;
  });
}

/**
 * Process text content to make citation references clickable
 * Converts [1], [2], etc. to clickable links that scroll to the source
 * @param {HTMLElement} container - Container with the text content
 * @param {Array} citations - Array of citations to map IDs correctly
 */
export function processCitationLinks(container, citations = []) {
  // Build a map of citation IDs for proper linking
  const citationMap = new Map();
  citations.forEach((c, index) => {
    const id = c.id || (index + 1);
    citationMap.set(id, id);
    citationMap.set(index + 1, id); // Also map by position
  });

  // Get all text nodes and process them
  const walker = document.createTreeWalker(
    container,
    NodeFilter.SHOW_TEXT,
    null,
    false
  );

  const nodesToReplace = [];
  let node;
  
  while (node = walker.nextNode()) {
    // Match citation patterns like [1], [2], [1,2], [1-3], etc.
    if (/\[\d+(?:[,\-]\d+)*\]/.test(node.textContent)) {
      nodesToReplace.push(node);
    }
  }

  nodesToReplace.forEach(textNode => {
    const text = textNode.textContent;
    const fragment = document.createDocumentFragment();
    let lastIndex = 0;
    
    // Match all citation patterns
    const regex = /\[(\d+(?:[,\-]\d+)*)\]/g;
    let match;

    while ((match = regex.exec(text)) !== null) {
      // Add text before the match
      if (match.index > lastIndex) {
        fragment.appendChild(document.createTextNode(text.substring(lastIndex, match.index)));
      }

      // Parse citation numbers
      const citationNums = match[1].split(/[,\-]/).map(n => parseInt(n.trim(), 10));
      const firstNum = citationNums[0];
      const targetId = citationMap.get(firstNum) || firstNum;

      // Create clickable citation link (displays as [1] style)
      const citationLink = document.createElement('a');
      citationLink.href = `#source-${targetId}`;
      citationLink.className = 'citation-link';
      citationLink.textContent = `[${match[1]}]`;
      citationLink.setAttribute('data-citation-id', targetId);
      citationLink.title = `View source ${match[1]}`;
      
      // Handle click - scroll to source
      citationLink.addEventListener('click', (e) => {
        e.preventDefault();
        const sourceEl =
          container.querySelector(`[data-source-id="${targetId}"]`) ||
          document.getElementById(`source-${targetId}`);
        if (sourceEl) {
          // Ensure sources panel is expanded
          const sourcesList = sourceEl.closest('.sources-list');
          if (sourcesList && sourcesList.style.display === 'none') {
            sourcesList.style.display = 'block';
            const panel = sourcesList.closest('.sources-panel');
            if (panel) {
              const icon = panel.querySelector('.sources-toggle-icon');
              if (icon) icon.style.transform = 'rotate(0deg)';
            }
          }
          
          // Expand the target row so the user lands on the cited excerpt, not a closed pill
          sourceEl.classList.remove('source-item-collapsed');

          sourceEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
          // Highlight animation
          sourceEl.classList.add('ring-2', 'ring-[#4A91D0]', 'ring-offset-2', 'bg-white');
          setTimeout(() => {
            sourceEl.classList.remove('ring-2', 'ring-[#4A91D0]', 'ring-offset-2', 'bg-white');
          }, 2000);
        }
      });

      fragment.appendChild(citationLink);
      lastIndex = regex.lastIndex;
    }

    // Add remaining text
    if (lastIndex < text.length) {
      fragment.appendChild(document.createTextNode(text.substring(lastIndex)));
    }

    // Replace the text node with our fragment
    if (fragment.childNodes.length > 0) {
      textNode.parentNode.replaceChild(fragment, textNode);
    }
  });
}

/**
 * Restore sources panel when loading conversation from history
 */
export function restoreSourcesPanel(container, citations) {
  if (!citations || citations.length === 0) return;
  
  // Process citation links in the container first (passing citations for ID mapping)
  processCitationLinks(container, citations);
  
  // Then render sources panel at the end
  const panel = renderSourcesPanel(citations);
  if (panel) {
    container.appendChild(panel);
  }
}

// Citation/source styles live in src/compass_frontend/src/input.css (not module-injected).
// (Including the .source-item-collapsed / .source-item-chevron / focus-visible
// rules added for this feature — pre-staged in #424's migration.)
