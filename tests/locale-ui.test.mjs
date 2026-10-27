import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import {
  localeOf, preferredLocale, localeURL, stateURL, createLocale, canonicalJSON, sourceHash,
  applyOverlay, loadOverlay, translatedFields, localizeDOM, installLocale, modelDisclosure,
} from '../site/js/locale.js';
import { messages } from '../site/js/locale-messages.js';
import { canonical, sourceHash as buildHash } from '../scripts/localization.mjs';
import { parseState } from '../site/js/state.js';
import * as tipsCore from '../site/js/tips-core.js';
import * as voicesCore from '../site/js/voices-core.js';
import { KeywordIndex, normalize, tokenize } from '../site/js/search.js';
import { rrf } from '../site/js/filter.js';

const tip = { id: 42, headline: 'How to query ORDA?', body: 'Use an entity selection.', type: { key: 'solved', p: .9 } };
const voice = { id: 42, question: 'Which direction?', positions: ['One view.', 'Another view.'], outcome: 'Still open.', rank: .7 };
const translations = {
  fr: { tips: { headline: 'Comment interroger ORDA ?', body: 'Utilisez une sélection d’entités.' },
    voices: { question: 'Quelle direction ?', positions: ['Un point de vue.', 'Un autre avis.'], outcome: 'Toujours ouverte.' } },
  ja: { tips: { headline: 'ORDAで検索するには？', body: 'エンティティセレクションを使用します。' },
    voices: { question: 'どの方向へ？', positions: ['一つの見方。', '別の見方。'], outcome: '未解決です。' } },
};
const overlay = (page, locale, item) => ({ version: 1, page, locale, entries: {
  [item.id]: { source_hash: buildHash(translatedFields(page, item)), translation_version: '1', fields: translations[locale][page] },
} });

test('ui never reinterprets source-thread lang and switching keeps complete state and anchors', () => {
  assert.equal(localeOf('?lang=fr'), 'en');
  assert.equal(localeOf('?lang=ja&ui=fr'), 'fr');
  assert.equal(localeOf('?ui=unknown'), 'en');
  const href = '/tips/?q=ORDA&mode=hyb&area=database&type=tip&conf=70&since=2020&all=1&lang=ja&t=42#entry';
  const fr = localeURL(href, 'fr');
  assert.equal(fr, href.replace('#entry', '&ui=fr#entry'));
  assert.equal(localeURL(fr, 'en'), href.replace('#entry', '&ui=en#entry'));
  assert.equal(localeURL('../voices/', 'ja', 'https://example.test/tips/?ui=ja'), '/voices/?ui=ja');
  const keys = ['q', 'mode', 'area', 'type', 'conf', 'since', 'sort', 'all'];
  const state = stateURL('?lang=fr&ui=ja&area=web&t=42', 'q=ORDA&area=database&mode=hyb', keys, 'ja');
  const params = new URLSearchParams(state);
  assert.equal(params.get('lang'), 'fr');
  assert.equal(params.get('ui'), 'ja');
  assert.equal(params.get('t'), '42');
  assert.equal(params.get('area'), 'database');
  assert.deepEqual(parseState(state).lang, ['fr']);
  assert.equal(new URLSearchParams(stateURL(state, '', keys, 'en')).get('ui'), 'en');
});

test('browser hash exactly matches publication canonical JSON and UTF-8 SHA256', async () => {
  for (const fields of [translatedFields('tips', tip), translatedFields('voices', voice),
    { z: ['é', 'ガ', { b: false, a: null }], a: { x: 1, w: '日本語' } }]) {
    assert.equal(canonicalJSON(fields), canonical(fields));
    assert.equal(await sourceHash(fields), buildHash(fields));
  }
  assert.notEqual(await sourceHash({ text: 'ガ' }), await sourceHash({ text: 'カ' }));
});

test('overlays localize only generated fields, preserving identities, classification, source, and rank', async () => {
  for (const page of ['tips', 'voices']) for (const locale of ['fr', 'ja']) {
    const item = page === 'tips' ? tip : voice;
    const before = structuredClone(item);
    const [result] = await applyOverlay([item], overlay(page, locale, item), page, locale);
    assert.deepEqual(translatedFields(page, result), translations[locale][page]);
    assert.deepEqual(result.canonicalFields, translatedFields(page, item));
    assert.equal(result.translationFallback, false);
    assert.equal(result.contentLocale, locale);
    assert.equal(result.id, item.id);
    if (page === 'tips') assert.equal(result.type, item.type);
    else assert.equal(result.rank, item.rank);
    assert.deepEqual(item, before);
  }
});

test('missing, stale, malformed, wrong-version and failed-hash overlays explicitly fall back per entry', async () => {
  for (const page of ['tips', 'voices']) {
    const item = page === 'tips' ? tip : voice;
    const good = overlay(page, 'ja', item);
    const stale = structuredClone(good); stale.entries[42].source_hash = '0'.repeat(64);
    const partial = structuredClone(good); delete partial.entries[42].fields[page === 'tips' ? 'body' : 'outcome'];
    const version = structuredClone(good); version.entries[42].translation_version = '0';
    const extra = structuredClone(good); extra.entries[42].fields.rank = 100;
    const variants = [null, { ...good, entries: {} }, stale, partial, version, extra,
      { ...good, locale: 'fr' }, { ...good, page: 'wrong' }, { ...good, version: 2 }];
    if (page === 'voices') {
      const positions = structuredClone(good); positions.entries[42].fields.positions.pop(); variants.push(positions);
    }
    for (const data of variants) {
      const [result] = await applyOverlay([item], data, page, 'ja');
      assert.deepEqual(translatedFields(page, result), translatedFields(page, item));
      assert.equal(result.translationFallback, true);
      assert.equal(result.contentLocale, 'en');
    }
    const [noCrypto] = await applyOverlay([item], good, page, 'ja', async () => { throw Error('unavailable'); });
    assert.equal(noCrypto.translationFallback, true);
    const other = { ...item, id: 43 };
    const mixed = await applyOverlay([item, other], good, page, 'ja');
    assert.equal(mixed[0].translationFallback, false);
    assert.equal(mixed[1].translationFallback, true);
    const changed = { ...item, [page === 'tips' ? 'body' : 'outcome']: 'Changed canonical English' };
    assert.equal((await applyOverlay([changed], good, page, 'ja'))[0].translationFallback, true);
  }
});

test('English does not fetch overlays; network failures retain English with explicit fallback', async () => {
  let requests = 0;
  const fetchJSON = async () => { requests++; throw Error('404'); };
  const items = [tip];
  assert.equal(await loadOverlay(items, 'tips', 'en', fetchJSON), items);
  assert.equal(requests, 0);
  assert.equal((await loadOverlay(items, 'tips', 'fr', fetchJSON))[0].translationFallback, true);
  assert.equal(requests, 1);
});

test('French accent folding and Japanese width/voicing normalization work in real keyword search', () => {
  assert.equal(normalize('ÉLÈVE'), 'eleve');
  assert.equal(normalize('ｅｎｔｉｔé'), 'entite');
  assert.equal(normalize('ｶﾞ'), 'ガ');
  assert.equal(normalize('カ\u3099'), 'ガ');
  assert.notEqual(normalize('バ'), normalize('ハ'));
  assert.deepEqual(tokenize('ﾃﾞｰﾀﾍﾞｰｽ'), tokenize('データベース'));
  const index = new KeywordIndex([
    { id: 1, title: 'Une sélection d’entités', line: '', tags: [] },
    { id: 2, title: 'データベース検索', line: '', tags: [] },
    { id: 3, title: 'ガイド', line: '', tags: [] },
  ]);
  assert.equal(index.search('selection entites')[0].id, 1);
  assert.equal(index.search('ﾃﾞｰﾀﾍﾞｰｽ')[0].id, 2);
  assert.equal(index.search('カ\u3099イド')[0].id, 3);
  assert.equal(index.search('"ガイド"')[0].id, 3);
  assert.equal(index.search('"カイド"').length, 0);
});

test('Japanese dakuten and handakuten stay distinct while halfwidth and composed forms match', () => {
  assert.equal(normalize('ｶﾞ'), normalize('ガ'));
  assert.equal(normalize('カ\u3099'), normalize('ガ'));
  assert.equal(normalize('ﾊﾟ'), normalize('パ'));
  assert.equal(normalize('ハ\u309a'), normalize('パ'));
  assert.notEqual(normalize('が'), normalize('か'));
  assert.notEqual(normalize('パ'), normalize('ハ'));
  const index = new KeywordIndex(['が', 'か', 'パ', 'ハ', 'ガ'].map((title, index) => ({ id: index + 1, title, line: '', tags: [] })));
  assert.deepEqual(index.search('が').map((result) => result.id), [1]);
  assert.deepEqual(index.search('か').map((result) => result.id), [2]);
  assert.deepEqual(index.search('パ').map((result) => result.id), [3]);
  assert.deepEqual(index.search('ハ').map((result) => result.id), [4]);
  assert.deepEqual(index.search('ｶﾞ').map((result) => result.id), [5]);
  assert.deepEqual(index.search('ガ').map((result) => result.id), [5]);
});

test('locale formatting and known manifest vocabulary cover French and Japanese', () => {
  for (const locale of ['en', 'fr', 'ja']) {
    const i18n = createLocale(locale);
    assert.equal(i18n.number.format(12345), new Intl.NumberFormat(locale).format(12345));
    assert.equal(i18n.pct(.7), new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 0 }).format(.7));
    assert.equal(i18n.date(null), '');
    assert.match(i18n.date(0), /1970/);
    assert.equal(i18n.t('unknown future label'), 'unknown future label');
  }
  for (const page of ['tips', 'voices']) {
    const config = JSON.parse(readFileSync(new URL(`../config/${page}.json`, import.meta.url)));
    const labels = [config.title, config.description,
      ...Object.values(config.questions).flatMap((q) => [q.label, ...Object.values(q.choices)]),
      ...config.presets.map((p) => p.label), ...(config.debate || [])];
    for (const locale of ['fr', 'ja']) for (const label of labels) {
      if (/^(?:4D |ORDA|REST & HTTP)/.test(label) && !/Forum/.test(label)) continue;
      assert.ok(messages[locale][label], `${locale}: ${label}`);
    }
  }
});

test('lazy model disclosure reflects WebGPU and larger working WASM fallback sizes', () => {
  const embeddings = { model_download_bytes: 206696682, wasm_model_download_bytes: 574688040 };
  for (const locale of ['en', 'fr', 'ja']) {
    const disclosure = modelDisclosure(embeddings, createLocale(locale));
    assert.match(disclosure, /207/);
    assert.match(disclosure, /575/);
    assert.match(disclosure, /WebGPU/);
    assert.match(disclosure, /WASM/);
    assert.doesNotMatch(disclosure, /q4 WASM|q8 WASM/);
    assert.ok(!modelDisclosure(null, createLocale(locale)).includes('undefined'));
    if (locale !== 'en') assert.ok(!disclosure.startsWith('Semantic search'));
  }
});

function element(tagName = 'DIV', attrs = {}, children = []) {
  return {
    nodeType: 1, tagName, attrs: { ...attrs }, childNodes: children, innerHTML: '', textContent: '', value: '', hidden: false,
    dataset: {}, classList: { toggle() {} }, addEventListener() {},
    hasAttribute(name) { return Object.hasOwn(this.attrs, name); },
    getAttribute(name) { return this.attrs[name]; },
    setAttribute(name, value) { this.attrs[name] = value; },
  };
}
const text = (value) => ({ nodeType: 3, textContent: value });

test('static DOM localization retains controls and never rewrites generated English or original titles', () => {
  const input = element('INPUT', { placeholder: 'Search discussions…  ( / )', 'aria-label': 'Search the discussions' });
  const french = text(' Outcome ');
  const original = text('Language');
  const english = text('Tip');
  const root = element('DIV', {}, [input, french, element('SPAN', { lang: 'ja' }, [original]),
    element('P', { lang: 'en' }, [english])]);
  localizeDOM(root, createLocale('fr'));
  assert.equal(input.attrs['aria-label'], 'Rechercher dans les discussions');
  assert.equal(french.textContent, ' Conclusion ');
  assert.equal(original.textContent, 'Language');
  assert.equal(english.textContent, 'Tip');
  assert.equal(root.childNodes[0], input);
});

// Execute the actual page modules against a small DOM adapter, without network, model, or extra dependencies.
for (const page of ['tips', 'voices']) for (const scenario of ['en', 'fr', 'ja', 'fr-stale', ...(page === 'tips' ? ['fr-no-entry'] : [])]) {
  const locale = scenario.split('-')[0];
  const stale = scenario.endsWith('-stale');
  const noEntry = scenario.endsWith('-no-entry');
  test(`${page} renders ${scenario} cards, details, locale links, and optional English fallback`, async () => {
    const topic = { id: 42, title: 'Titre original <4D>', title_en: 'Original English title', slug: 'original',
      lang: 'fr', tags: [], created: 19000, views: 12345, likes: 7, posts: 8, flags: 4, para: 'English thread summary.', line: 'English one-line summary.' };
    const config = JSON.parse(readFileSync(new URL(`../config/${page}.json`, import.meta.url)));
    const questions = Object.entries(config.questions).map(([id, q]) => ({
      id, choices: Object.entries(q.choices).map(([key, label]) => ({ key, label, long: label })),
    }));
    const run = { id: 'run', file: 'tips/run.json', count: 1, model: 'model', prompt_version: '1', write_version: '1' };
    const manifest = { forum_url: 'https://example.test', flags: { accepted: 4 }, stats: { enriched: 1 },
      generated_at: '2026-10-27T00:00:00Z', embeddings: null,
      tips: { ...config, questions, runs: [run], default_run: 'run', files: { entries: 'tips/entries.json' }, entries: 1 },
      voices: { ...config, questions, run, files: { entries: 'voices/entries.json' }, entries: 1 } };
    const entries = page === 'tips'
      ? { ids: noEntry ? [] : [42], modes: ['solved'], mode: [0], headline: [tip.headline], body: [tip.body], ok: [1] }
      : { ids: [42], rank: [700], debate: [500], exchange: [0], settled: [2], settled_p: [900], subject: [0],
        subject_p: [900], participants: [5], returning: [2], days: [3], question: [voice.question],
        positions: [voice.positions], outcome: [voice.outcome], ok: [1] };
    const elements = new Map();
    const get = (selector) => {
      if (!elements.has(selector)) elements.set(selector, element());
      return elements.get(selector);
    };
    const modes = ['kw', 'hyb', 'sem'].map((mode) => ({ ...element('BUTTON'), dataset: { mode } }));
    const confidence = ['0', '50', '70'].map((value) => ({ ...element('OPTION'), value }));
    const document = { body: element('BODY'), documentElement: element('HTML'),
      querySelector: get, getElementById: (id) => get('#' + id),
      querySelectorAll: (selector) => selector === '.modes button' ? modes : selector === '#conf option' ? confidence : [], addEventListener() {} };
    const location = { pathname: `/${page}/`, search: locale === 'en' ? '?lang=fr' : `?lang=fr&ui=${locale}`,
      hash: '#entry', get href() { return 'https://example.test' + this.pathname + this.search + this.hash; } };
    const requests = [];
    const context = {
      ...tipsCore, ...voicesCore, SORTS: page === 'tips' ? tipsCore.SORTS : voicesCore.SORTS,
      createLocale, localeOf, preferredLocale, localeURL, stateURL, loadOverlay, installLocale: (i18n, page) => installLocale(i18n, page, document, location), localizeDOM, modelDisclosure, KeywordIndex, rrf,
      console, document, location, URLSearchParams, Intl, setTimeout, clearTimeout,
      loadManifest: async () => manifest, loadTopics: async () => [topic], loadText: async () => {},
      decodeRun: () => new Map([[42, { area: { c: 'database', p: [0, 0, 1] }, qa: { c: 'solved', p: [.9, 0, 0, .1] } }]]),
      dataUrl: (path) => path,
      Semantic: class { available = false; },
      fetch: async (path) => {
        requests.push(path);
        let data;
        if (path.includes('locales/')) {
          data = overlay(page, locale, page === 'tips' ? tip : voice);
          if (stale) data.entries[42].source_hash = '0'.repeat(64);
        } else data = path.endsWith('entries.json') ? entries : {};
        return { ok: true, json: async () => data };
      },
    };
    context.window = { addEventListener() {}, history: {
      replaceState(_state, _title, url) {
        const parsed = new URL(url, location.href); location.search = parsed.search; location.hash = parsed.hash;
      }, pushState() {},
    } };
    const source = readFileSync(new URL(`../site/js/${page}.js`, import.meta.url), 'utf8')
      .replace(/^import[\s\S]*?from '[^']+';\n/gm, '').replace(/init\(\)\.catch\([\s\S]*$/, '');
    vm.runInNewContext(source + `
      globalThis.ready = init(); globalThis.detail = renderWhy; globalThis.query = setQuery;
      globalThis.progress = onModelProgress;
      globalThis.unavailable = () => { app.sem.error = 'offline'; app.state.mode = 'hyb'; update({ history: false }); };
    `, context);
    await context.ready;
    const html = get('#results').innerHTML;
    assert.match(html, /Titre original &lt;4D&gt;/);
    const expected = noEntry ? topic.title_en : locale === 'en' || stale ? page === 'tips' ? tip.headline : voice.question
      : translations[locale][page][page === 'tips' ? 'headline' : 'question'];
    assert.ok(html.includes(expected), html);
    assert.ok(html.includes(`lang="${stale || noEntry ? 'en' : locale}"`));
    assert.equal(html.includes('class="translation-fallback"'), stale || noEntry);
    if (stale || noEntry) assert.ok(html.includes(createLocale(locale).t('English fallback')));
    if (noEntry) assert.ok(html.includes(createLocale(locale).t('(thread summary; no knowledge-base entry yet)')));
    assert.ok(html.includes(createLocale(locale).t(page === 'tips' ? 'Why is this here?' : 'About this summary')));
    assert.equal(confidence[1].textContent, '≥ ' + createLocale(locale).pct(.5));
    assert.equal(requests.some((url) => url.includes('locales/')), locale !== 'en');
    assert.match(get('#locale-nav').innerHTML, /lang=fr/);
    assert.match(get('#locale-nav').innerHTML, /#entry/);
    assert.equal(new URLSearchParams(location.search).get('lang'), 'fr');
    assert.equal(document.documentElement.lang, locale);
    const detail = element(); detail.dataset.id = '42'; detail.querySelector = () => detail;
    context.detail(detail);
    const status = noEntry ? 'No knowledge-base entry yet: the text above is the thread summary.'
      : page === 'tips' ? 'Entry passed the quality checks.' : 'The summary passed the checks (including: no names).';
    assert.ok(detail.innerHTML.includes(createLocale(locale).t(status)));
    if (page === 'tips') {
      assert.ok(detail.innerHTML.includes(createLocale(locale).t('English source summary')));
      assert.ok(detail.innerHTML.includes('<span lang="en">English thread summary.</span>'));
    }
    assert.ok(detail.innerHTML.includes(`/requests/?t=42&amp;ui=${locale}`));
    context.query('this-word-never-appears');
    const empty = page === 'tips' ? 'Nothing found. Try other words, Semantic mode, more areas or types, or a lower confidence.'
      : 'Nothing found. Try other words, Semantic mode, or fewer filters.';
    assert.ok(get('#results').innerHTML.includes(createLocale(locale).t(empty)));
    context.progress({ file: 'model', total: 1e6, loaded: .5e6 });
    assert.ok(get('#status').innerHTML.includes(createLocale(locale).t('Loading the semantic model (first time only): {loaded} / {total} MB…',
      { loaded: createLocale(locale).decimal.format(.5), total: createLocale(locale).decimal.format(1) })));
    context.unavailable();
    assert.ok(get('#status').innerHTML.includes(createLocale(locale).t('Semantic search unavailable: {error}', { error: 'offline' })));
  });
}
