import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

async function setup(supported = true) {
  const events = {};
  const link = { addEventListener: (name, fn) => { events[name] = fn; }, setAttribute() {}, focus() { this.focused = true; } };
  const player = { children: [], replaceChildren(...nodes) { this.children = nodes; } };
  const dialog = { addEventListener: (name, fn) => { events[name] = fn; } };
  if (supported) dialog.showModal = () => { dialog.open = true; };
  const document = {
    getElementById: (id) => ({ demoLink: link, demoDialog: dialog, demoPlayer: player })[id],
    createElement: (tagName) => ({ tagName }),
  };
  const code = await readFile(new URL('../public/assets/js/demo.js', import.meta.url), 'utf8').catch(error => {
    if (error.code === 'ENOENT') return '';
    throw error;
  });
  vm.runInNewContext(code, { document });
  return { events, link, player, dialog };
}

test('demo activation creates one privacy-enhanced player; close removes it and restores focus', async () => {
  const { events, link, player, dialog } = await setup();
  assert.equal(player.children.length, 0);
  assert.equal(typeof events.click, 'function', 'demo activation handler exists');
  let prevented = false;
  events.click({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(dialog.open, true);
  assert.equal(player.children.length, 1);
  assert.equal(player.children[0].src, 'https://www.youtube-nocookie.com/embed/J8KU6_e70mk?autoplay=1');
  assert.equal(player.children[0].title, 'Compass demo video');
  events.close();
  assert.equal(player.children.length, 0);
  assert.equal(link.focused, true);
  events.click({ preventDefault() {} });
  assert.equal(player.children.length, 1);
});

test('browsers without native dialog retain normal YouTube navigation', async () => {
  const { events, player } = await setup(false);
  assert.equal(events.click, undefined);
  assert.equal(player.children.length, 0);
});
