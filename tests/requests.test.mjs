import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { landingURL } from '../site/js/redirect.js';
import { parseRequestsState, parseState, serializeState } from '../site/js/state.js';
import { preferredLocale, navigationURL, applyOverlay, translatedFields } from '../site/js/locale.js';
import { sourceHash } from '../scripts/localization.mjs';
import { KeywordIndex } from '../site/js/search.js';

test('root redirects only meaningful legacy state to Requests, preserving project base and fragment', () => {
  for (const base of ['https://example.test/', 'https://example.test/4d-forum-insights/',
    'https://example.test/4d-forum-insights', 'https://example.test/4d-forum-insights/index.html']) {
    const root = base.includes('4d-forum-insights') ? '/4d-forum-insights/' : '/';
    for (const query of ['', '?ui=ja', '?ui=fr&unknown=https://evil.test']) {
      const target = new URL(landingURL(base + query + '#section', 'ja'), base);
      assert.equal(target.pathname, root + 'voices/');
      assert.equal(target.searchParams.get('ui'), 'ja');
      assert.equal(target.hash, '#section');
      assert.equal(target.origin, new URL(base).origin);
    }
    for (const state of ['q=ORDA', 't=42', 'a.kind=bug_report&sort=new', 'lang=fr',
      'scope=all', 'view=table', 'cat=27', 'q=&sort=active', 'a.future=value']) {
      const target = new URL(landingURL(base + '?' + state + '#detail', 'fr'), base);
      assert.equal(target.pathname, root + 'requests/');
      for (const [key, value] of new URLSearchParams(state)) assert.equal(target.searchParams.get(key), value);
      assert.equal(target.hash, '#detail');
    }
  }
});

test('Requests default is high-band hidden requests, not a smaller dataset or override of explicit intent', () => {
  const config = JSON.parse(readFileSync(new URL('../config/site.json', import.meta.url)));
  const initial = parseRequestsState('?ui=ja', [], config.presets);
  assert.deepEqual(initial.ans.fr, ['high']);
  assert.deepEqual(initial.xcat, ['27']);
  assert.equal(initial.sort, 'a.fr');
  for (const query of ['?scope=all', '?q=bug', '?t=42', '?sort=active', '?a.kind=question', '?q=']) {
    assert.deepEqual(parseRequestsState(query, [], config.presets), parseState(query));
  }
  const all = parseState('?scope=all');
  assert.equal(serializeState(all), 'scope=all');
  assert.equal(parseRequestsState('?' + serializeState(all)).scope, 'all');
});

test('locale preference precedence is explicit UI, saved preference, browser, then English', () => {
  const storage = { getItem: () => 'ja' };
  assert.equal(preferredLocale('?ui=en&lang=fr', storage, ['fr']), 'en');
  assert.equal(preferredLocale('?lang=fr', storage, ['fr']), 'ja');
  assert.equal(preferredLocale('?ui=invalid', { getItem: () => null }, ['de', 'fr-CA']), 'fr');
  assert.equal(preferredLocale('', { getItem() { throw Error('denied'); } }, ['ja-JP']), 'ja');
  assert.equal(preferredLocale('', null, ['de-DE']), 'en');
});

test('cross-view navigation shares query and explicit search mode, not incompatible facet state', () => {
  assert.equal(navigationURL('../requests/', 'ja',
    'https://example.test/4d-forum-insights/tips/?ui=ja&q=ORDA&mode=hyb&area=database#entry'),
  '/4d-forum-insights/requests/?ui=ja&q=ORDA&mode=hyb');
  assert.equal(navigationURL('./', 'fr', 'https://example.test/tips/?q=ORDA&area=database'),
    '/tips/?ui=fr');
  assert.equal(navigationURL('../tips/', 'en', 'https://example.test/voices/?ui=ja&subject=technical'),
    '/tips/?ui=en');
});

test('pre-paint locale agrees with runtime, including unavailable storage', () => {
  const boot = readFileSync(new URL('../site/js/locale-boot.js', import.meta.url), 'utf8');
  for (const search of ['', '?ui=en', '?ui=fr&lang=ja', '?ui=invalid']) {
    for (const saved of ['ja', null, 'blocked']) {
      const storage = { getItem() { if (saved === 'blocked') throw Error('denied'); return saved; } };
      const root = { setAttribute() {}, removeAttribute() {} };
      vm.runInNewContext(boot, { URLSearchParams, location: { search }, localStorage: storage,
        navigator: { languages: ['de-DE', 'fr-CA'] }, document: { documentElement: root }, setTimeout() {} });
      assert.equal(root.lang, preferredLocale(search, storage, ['de-DE', 'fr-CA']));
    }
  }
});

test('Requests overlays bind all generated fields while preserving original titles and empty fields', async () => {
  const item = { id: 42, title: 'Source title', title_en: null, line: 'Entity selection', para: 'Use ORDA.',
    lang: 'en', tags: [], ans: { fr: { s: .8 } } };
  const fields = { title_en: '', line: 'Sélection d’entités', para: 'Utilisez ORDA.' };
  const entry = { source_hash: sourceHash(translatedFields('requests', item)), translation_version: '1', fields };
  const overlay = { version: 1, page: 'requests', locale: 'fr', entries: { 42: entry } };
  const [translated] = await applyOverlay([item], overlay, 'requests', 'fr');
  assert.deepEqual(translatedFields('requests', translated), fields);
  assert.equal(translated.title, item.title);
  assert.equal(translated.ans, item.ans);
  assert.equal(translated.lang, 'en');
  const index = new KeywordIndex([translated]);
  await index.addParagraphs();
  for (const query of ['selection entites', '"Entity selection"', 'ORDA']) {
    assert.equal(index.search(query)[0]?.id, 42, query);
  }
  assert.equal(index.search('selection -ORDA').length, 0);
  for (const invalid of [{ ...item, para: 'Changed English' }, { ...item, line: 'Changed line' }]) {
    const [fallback] = await applyOverlay([invalid], overlay, 'requests', 'fr');
    assert.equal(fallback.translationFallback, true);
    assert.equal(fallback.line, invalid.line);
  }
  const malformed = structuredClone(overlay);
  malformed.entries[42].fields.title_en = 'Invented generated title';
  assert.equal((await applyOverlay([item], malformed, 'requests', 'fr'))[0].translationFallback, true);
});

test('static Requests resources resolve under the project prefix; root never preloads corpus', () => {
  const html = readFileSync(new URL('../site/requests/index.html', import.meta.url), 'utf8');
  const base = 'https://example.test/4d-forum-insights/requests/';
  for (const [, resource] of html.matchAll(/(?:src|href)="([^"]+\.(?:js|css))"/g)) {
    assert.match(new URL(resource, base).pathname, /^\/4d-forum-insights\/(?:js|css)\//);
  }
  const root = readFileSync(new URL('../site/index.html', import.meta.url), 'utf8');
  assert.doesNotMatch(root, /app\.js|manifest\.json|topics\.json|preload/);
});
