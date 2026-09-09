import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const publicFile = (name) =>
  new URL(`../public/${name}`, import.meta.url);

async function source(name) {
  return readFile(publicFile(name), "utf8");
}

test("landing page presents Ashley's approved Compass welcome copy", async () => {
  const page = (await source("index.php")).replace(/\s+/g, " ");

  assert.match(page, /Meet Compass/);
  assert.match(page, /your AI-powered teacher policy research assistant/);
  assert.match(
    page,
    /Ask Compass for trends or guidance on teacher policy challenges, dig into a specific district, or compare peers\./,
  );
  assert.match(page, /Compass tracks 100\+ data points across more than 130 districts\./);
  assert.match(page, /<p[^>]*>\s*Need guidance\? <a[^>]+href="https:\/\/www\.youtube\.com\/watch\?v=J8KU6_e70mk&amp;feature=youtu\.be"[^>]+class="[^"]*text-text-accent[^"]*underline[^"]*">Watch a video\.<\/a>\s*<\/p>/);
  assert.match(page, /<dialog[^>]+id="demoDialog"[^>]+aria-labelledby="demoTitle"/);
  assert.doesNotMatch(page, /<iframe[^>]+src=/);
});

test("welcome subheading and bounded body balance naturally without forced line breaks", async () => {
  const page = (await source("index.php")).replace(/\s+/g, " ");
  assert.match(page, /<span class="[^"]*block[^"]*text-balance[^"]*">your AI-powered teacher policy research assistant<\/span>/);
  assert.match(page, /<p class="[^"]*max-w-\[656px\][^"]*text-balance[^"]*"> Ask Compass[^<]+130 districts\. <\/p>/);
});

test("landing prompt uses the approved placeholder and first starter question", async () => {
  const [page, cleanup, questions] = await Promise.all([
    source("index.php"),
    source("assets/js/ui/cleanup.js"),
    source("sample-questions.php"),
  ]);

  assert.match(page, /placeholder="Ask your question here"/);
  assert.match(cleanup, /desktop: "Ask your question here"/);
  assert.match(questions, /Show me the 5 districts that offer the most elementary teacher planning time/);
});
