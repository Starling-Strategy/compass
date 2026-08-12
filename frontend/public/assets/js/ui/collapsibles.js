/**
 * Fold renderer-emitted secondary sections into collapsibles (#1228).
 *
 * The backend renderer leads with the answer (lead prose + table), then emits
 * each secondary section — coverage disclosure, methodology, "Not included in
 * ranking", "Larger districts not ranked" — as an H4 heading (`#### …`, the
 * renderer's only use of H4). This turns each such H4 and the elements that
 * follow it (up to the next H4) into a collapsed <details>, so the answer stays
 * in view and the caveats no longer bury it.
 *
 * The contract is purely structural: "a top-level <h4> begins a foldable
 * section." The renderer is the sole emitter of H4 (verified by a backend
 * invariant test) and the answer-layer seals those headings as immutable
 * blocks, so this fold is correct by construction — it never folds primary
 * content, and the section it expects is always present.
 *
 * A section absorbs only the *markdown flow* elements after its heading. The
 * chart, sources panel, feedback, and CSV buttons are appended to the same
 * container as plain <div>/<button> nodes (marked never emits those at the top
 * level), so a section stops at the first such widget — the last section can't
 * swallow the chart/sources that land after it during streaming. This is why
 * the fold can run in onFinish (chart already present) without hiding it.
 *
 * Pure DOM transform: mutates `wrap` in place, returns nothing. Idempotent —
 * once an H4 is replaced by its <details>, a re-run finds no top-level H4 and
 * is a no-op. Kept in its own module so it can be unit-tested without the
 * chat.js import chain.
 *
 * @param {HTMLElement} wrap - The agent message container (post-markdown-render).
 */

// Block tags marked.js emits at the top level — the only nodes a folded section
// may absorb. Anything else following a heading (a <div> chart, a <button>, the
// sources panel) is an appended widget and ends the section.
const MARKDOWN_FLOW_TAGS = new Set([
  "P", "UL", "OL", "BLOCKQUOTE", "PRE", "TABLE", "HR", "DL", "H5", "H6",
]);

export function collapsifyAgentSections(wrap) {
  const headings = Array.from(wrap.querySelectorAll(":scope > h4"));
  headings.forEach((heading) => {
    const moved = [];
    let next = heading.nextElementSibling;
    while (next && MARKDOWN_FLOW_TAGS.has(next.tagName)) {
      moved.push(next);
      next = next.nextElementSibling;
    }
    const details = document.createElement("details");
    details.className = "pa-collapse";
    const summary = document.createElement("summary");
    const chev = document.createElement("span");
    chev.className = "pa-collapse-chev";
    chev.textContent = "▸";
    const title = document.createElement("span");
    title.className = "pa-collapse-title";
    title.textContent = heading.textContent;
    summary.append(chev, title);
    details.appendChild(summary);
    heading.replaceWith(details);
    moved.forEach((node) => details.appendChild(node));
  });
}
