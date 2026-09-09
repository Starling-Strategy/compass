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

test("standalone video row is funded by whitespace, not smaller text or clipping", async () => {
  const page = (await source("index.php")).replace(/\s+/g, " ");
  // Save 12px above the heading, 4px in each of two gaps, and 16px
  // before the form: the added 20px CTA + old 16px gap costs 36px.
  assert.match(page, /id="chatWrapper" class="[^"]*mt-\[86px\]/);
  assert.match(page, /<section class="[^"]*space-y-3 mb-lg" id="headline">/);
  assert.match(page, /text-headline-sm md:text-headline/);
  const headline = page.match(/<section[^>]+id="headline">(.*?)<\/section>/)[1];
  assert.doesNotMatch(headline, /(?:max-)?h-\[|overflow-hidden|text-xs/);
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
