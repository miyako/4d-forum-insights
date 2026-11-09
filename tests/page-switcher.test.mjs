import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { installPageSwitcher } from '../site/js/page-switcher.js';
import { installLocale, createLocale } from '../site/js/locale.js';

test('all four pages use the same native popup navigation with the current page identified', () => {
  for (const page of ['requests', 'tips', 'voices', 'questions']) {
    const html = readFileSync(new URL(`../site/${page}/index.html`, import.meta.url), 'utf8');
    assert.match(html, /<details class="page-switcher">/);
    assert.match(html, /<summary class="btn" aria-label="Switch forum page">/);
    assert.match(html, /<nav aria-label="Forum pages">/);
    assert.match(html, new RegExp(`<span aria-current="page">${page[0].toUpperCase() + page.slice(1)}</span>`));
    assert.match(html, /src="\.\.\/js\/page-switcher.js"/);
    assert.doesNotMatch(html, /class="btn switch"/);
  }
});

test('popup supports keyboard movement, native Tab order, Escape and outside click/focus dismissal', () => {
  const handlers = {};
  const outside = {};
  const summary = { focus() { document.activeElement = this; } };
  const links = [0, 1, 2].map((id) => ({ id, hidden: id === 1, focus() { document.activeElement = this; } }));
  const switcher = {
    open: false, querySelector: () => summary, querySelectorAll: () => links,
    contains: (target) => target === summary || links.includes(target),
    addEventListener: (name, fn) => { handlers[name] = fn; },
  };
  const document = {
    activeElement: summary, querySelector: () => switcher,
    addEventListener: (name, fn) => { handlers['document-' + name] = fn; },
  };
  installPageSwitcher(document);
  const key = (value) => {
    let prevented = false;
    handlers.keydown({ key: value, preventDefault() { prevented = true; } });
    return prevented;
  };
  assert.equal(key('ArrowDown'), true);
  assert.equal(switcher.open, true);
  assert.equal(document.activeElement, links[0]);
  key('ArrowDown'); assert.equal(document.activeElement, links[2], 'hidden Questions/Tips links are skipped');
  key('ArrowDown'); assert.equal(document.activeElement, links[0]);
  key('End'); assert.equal(document.activeElement, links[2]);
  key('Home'); assert.equal(document.activeElement, links[0]);
  key('ArrowUp'); assert.equal(document.activeElement, links[2]);
  assert.equal(key('Tab'), false, 'Tab remains native');
  key('Escape'); assert.equal(switcher.open, false); assert.equal(document.activeElement, summary);
  key('ArrowDown');
  handlers['document-click']({ target: links[0] }); assert.equal(switcher.open, true);
  handlers['document-click']({ target: outside }); assert.equal(switcher.open, false);
  key('ArrowDown');
  handlers['document-focusin']({ target: outside }); assert.equal(switcher.open, false);
});

test('popup links use shared locale navigation without losing query or search mode', () => {
  const links = ['requests', 'tips', 'voices'].map((page) => ({
    dataset: {}, getAttribute: () => `../${page}/`, closest: () => false,
  }));
  const document = {
    documentElement: { removeAttribute() {} }, body: { nodeType: 1, tagName: 'BODY', childNodes: [], hasAttribute: () => false },
    getElementById: () => ({ setAttribute() {} }),
    querySelectorAll: (selector) => selector === 'a[href]' ? links : [],
  };
  const location = { href: 'http://localhost/questions/?q=ORDA&mode=kw&ui=fr' };
  installLocale(createLocale('fr'), 'questions', document, location);
  for (let i = 0; i < links.length; i++) {
    assert.equal(links[i].href, `/${['requests', 'tips', 'voices'][i]}/?ui=fr&q=ORDA&mode=kw`);
  }
});
