/**
 * Formerly processed blockquotes — now a no-op since the Writer no longer
 * embeds source text. The citations panel and document viewer provide
 * safe access to source text via DB-sourced excerpts.
 */

/**
 * @param {HTMLElement} _container - The message bubble element (unused)
 * @param {Array} _quotes - Validated quote objects (unused)
 */
export function processQuoteLinks(_container, _quotes) {
  // No-op: Writer no longer produces blockquotes.
  // Source text access is through the citations panel "View full document" button.
}
