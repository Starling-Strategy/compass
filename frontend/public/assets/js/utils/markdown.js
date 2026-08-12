const renderer = new marked.Renderer();

renderer.link = function (hrefOrToken, titleArg, textArg) {
  const token = hrefOrToken && typeof hrefOrToken === "object"
    ? hrefOrToken
    : { href: hrefOrToken, title: titleArg, text: textArg };
  const href = token.href || "";
  const text = token.text || href;
  const title = token.title ? ` title="${token.title}"` : "";

  return `<a href="${href}" target="_blank" rel="noopener noreferrer"${title}>${text}</a>`;
};

export function renderMarkdown(md) {
  // Escape single tildes so ~approx values don't trigger GFM strikethrough.
  // Preserves ~~intentional strikethrough~~.
  md = md.replace(/(?<!\~)\~(?!\~)/g, "\\~");

  const html = marked.parse(md, {
    breaks: true,
    gfm: true,
    renderer,
  });
  if (typeof DOMPurify !== "undefined") {
    return DOMPurify.sanitize(html);
  }
  console.warn("DOMPurify not loaded — rendering as plain text");
  return md;
}
