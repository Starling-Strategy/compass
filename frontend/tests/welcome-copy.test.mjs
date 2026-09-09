import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const source = (name) => readFile(new URL(`../public/${name}`, import.meta.url), "utf8");

test("approved welcome ends with one inline disclaimer-colored video demonstration link", async () => {
  const page = (await source("index.php")).replace(/\s+/g, " ");
  const headline = page.match(/<section[^>]+id="headline">(.*?)<\/section>/)[1];
  assert.match(headline, /Meet Compass/);
  assert.match(headline, /<p class="[^"]*max-w-\[656px\][^"]*text-balance[^"]*"> Ask Compass for trends or guidance on teacher policy challenges, dig into a specific district, or compare peers\. Compass tracks 100\+ data points across more than 130 districts\. <a id="demoLink" href="https:\/\/www\.youtube\.com\/watch\?v=J8KU6_e70mk&amp;feature=youtu\.be" class="text-on-dark-subtle underline hover:text-on-dark-strong">Watch a video demonstration\.<\/a> <\/p>/);
  assert.doesNotMatch(page, /Need guidance\?/);
  assert.equal((page.match(/Watch a video demonstration\./g) || []).length, 1);
  for (const id of ["demoLink", "demoDialog", "demoPlayer", "demoTitle"]) {
    assert.equal((page.match(new RegExp(`id="${id}"`, "g")) || []).length, 1, `${id} is unique`);
  }
  assert.match(page, /<dialog[^>]+id="demoDialog"[^>]+aria-labelledby="demoTitle"/);
  assert.doesNotMatch(page, /<iframe[^>]+src=/);
});

test("welcome balances naturally without forced line breaks or clipping", async () => {
  const page = (await source("index.php")).replace(/\s+/g, " ");
  const headline = page.match(/<section[^>]+id="headline">(.*?)<\/section>/)[1];
  assert.match(headline, /<span class="[^"]*block[^"]*text-balance[^"]*">your AI-powered teacher policy research assistant<\/span>/);
  assert.match(headline, /text-headline-sm md:text-headline/);
  assert.doesNotMatch(headline, /<br|whitespace-nowrap|(?:max-)?h-\[|overflow-hidden|text-xs/);
});

test("landing prompt uses the approved placeholder and first starter question", async () => {
  const [page, cleanup, questions] = await Promise.all([
    source("index.php"), source("assets/js/ui/cleanup.js"), source("sample-questions.php"),
  ]);
  assert.match(page, /placeholder="Ask your question here"/);
  assert.match(cleanup, /desktop: "Ask your question here"/);
  assert.match(questions, /Show me the 5 districts that offer the most elementary teacher planning time/);
});
