import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createRequestsLocale, localizeRequestsManifest, requestsMessages } from '../site/js/requests-messages.js';
import { preferredLocale, localeURL, navigationURL, stateURL, installLocale, loadOverlay, sourceHash, applyOverlay } from '../site/js/locale.js';
import * as state from '../site/js/state.js';
import * as filters from '../site/js/filter.js';
import { KeywordIndex, normalize } from '../site/js/search.js';

const config = JSON.parse(readFileSync(new URL('../config/questions.json', import.meta.url)));
const site = JSON.parse(readFileSync(new URL('../config/site.json', import.meta.url)));
const questions = Object.entries(config).filter(([id]) => !id.startsWith('_')).map(([id, q]) => ({
  ...q, id, type: id === 'fr' ? 'score' : id === 'kind' ? 'choice' : 'noul',
  ...(q.levels ? { levels: Object.entries(q.levels).map(([key, label]) => ({ key, label, value: Number(key), long: label })) } : {}),
  ...(q.bands ? { bands: q.bands.bands } : {}),
  ...(q.choices ? { choices: Object.entries(q.choices).map(([key, label]) => ({ key, label, long: label })) } : {}),
}));

test('Requests dictionaries preserve interpolation tokens and translate all configured model and preset labels', () => {
  for (const locale of ['fr', 'ja']) {
    const i18n = createRequestsLocale(locale);
    for (const [key, translated] of Object.entries(requestsMessages[locale])) {
      assert.deepEqual([...translated.matchAll(/\{\w+\}/g)].map(String).sort(), [...key.matchAll(/\{\w+\}/g)].map(String).sort(), key);
    }
    const manifest = { questions, presets: site.presets, categories: [], languages: [] };
    const before = structuredClone(manifest);
    const localized = localizeRequestsManifest(manifest, i18n);
    assert.deepEqual(manifest, before);
    assert.deepEqual(localized.questions.map((q) => [q.id, q.type, q.order, q.primary]), questions.map((q) => [q.id, q.type, q.order, q.primary]));
    for (let i = 0; i < questions.length; i++) {
      const original = questions[i], result = localized.questions[i];
      assert.notEqual(result.label, original.label);
      assert.notEqual(result.short, original.short);
      for (const group of ['levels', 'bands', 'choices']) if (original[group]) {
        assert.deepEqual(result[group].map((row) => row.key), original[group].map((row) => row.key));
        assert.deepEqual(result[group].map((row) => row.value), original[group].map((row) => row.value));
        assert.deepEqual(result[group].map((row) => row.quantile), original[group].map((row) => row.quantile));
      }
    }
    assert.deepEqual(localized.presets.map((p) => [p.id, p.params]), site.presets.map((p) => [p.id, p.params]));
    for (let i = 0; i < site.presets.length; i++) {
      assert.notEqual(localized.presets[i].label, site.presets[i].label);
      assert.notEqual(localized.presets[i].description, site.presets[i].description);
    }
    assert.equal(i18n.t('not a registered label'), 'not a registered label');
    assert.equal(i18n.t('Interface language'), locale === 'fr' ? 'Langue de l’interface' : '表示言語');
  }
});

test('Requests overlays normalize null or empty generated fields without translating the original title', async () => {
  const fields = { title_en: '', line: '', para: '' };
  for (const emptyValue of ['', null]) for (const locale of ['fr', 'ja']) {
    const topic = { id: 1, title: 'Titre original', title_en: emptyValue, line: emptyValue, para: emptyValue, lang: 'fr', tags: [] };
    const [translated] = await applyOverlay([topic], { version: 1, page: 'requests', locale, entries: { 1: {
      translation_version: '1', source_hash: await sourceHash(fields), fields,
    } } }, 'requests', locale);
    assert.equal(translated.translationFallback, false);
    assert.equal(translated.title, topic.title);
    for (const key of ['title_en', 'line', 'para']) assert.equal(translated[key], '');
    assert.deepEqual(translated.canonicalFields, fields);
    const [missing] = await loadOverlay([topic], 'requests', locale, async () => { throw Error('missing'); });
    assert.equal(missing.translationFallback, true);
    assert.equal(missing.title, topic.title);
  }
});

test('older published manifests gain one localized All topics escape hatch without changing existing presets', () => {
  const presets = site.presets.filter((preset) => preset.id !== 'all');
  for (const locale of ['en', 'fr', 'ja']) {
    const i18n = createRequestsLocale(locale);
    const localized = localizeRequestsManifest({ questions, presets, categories: [], languages: [] }, i18n);
    assert.equal(localized.presets[0].id, 'all');
    assert.equal(localized.presets[0].label, i18n.t('All topics'));
    assert.equal(localized.presets[0].params, 'scope=all');
    assert.deepEqual(localized.presets.slice(1).map((preset) => [preset.id, preset.params]), presets.map((preset) => [preset.id, preset.params]));
    assert.equal(localizeRequestsManifest(localized, i18n).presets.filter((preset) => preset.id === 'all').length, 1);
    assert.equal(presets.some((preset) => preset.id === 'all'), false);
  }
});

function element(tagName = 'DIV', attrs = {}, children = []) {
  return {
    nodeType: 1, tagName, attrs: { ...attrs }, childNodes: children, innerHTML: '', textContent: '', value: '', title: '', hidden: false,
    dataset: {}, classList: { add() {}, remove() {}, contains() { return false; } }, addEventListener() {},
    hasAttribute(name) { return Object.hasOwn(this.attrs, name); },
    getAttribute(name) { return this.attrs[name]; },
    setAttribute(name, value) { this.attrs[name] = value; },
  };
}

for (const locale of ['en', 'fr', 'ja']) for (const stale of [false, true]) {
  test(`Requests ${locale}${stale ? ' stale' : ''}: boot, cards, filters, table, details, status and state links`, async () => {
    const topic = { id: 42, i: 0, title: 'Language <original>', title_en: 'English generated title', slug: 'public-source',
      lang: 'fr', tags: ['ORDA'], cat: 9, created: 19000, active: 19001, views: 12345, likes: 7, posts: 8, votes: 5, flags: 4,
      line: 'English one-line summary.', para: undefined };
    const translations = {
      fr: { title_en: 'Titre français', line: 'Résumé français.', para: 'Paragraphe français.' },
      ja: { title_en: '日本語のタイトル', line: '日本語の要約。', para: '日本語の段落。' },
    };
    const run = { id: 'run', label: 'model · version', questions: questions.map((q) => q.id), count: 1, bands: { fr: { cuts: [2.5, 1.75] } } };
    const answers = new Map([[42, {
      fr: { s: 2.8, lv: '3', b: 'high', p: [.01, .04, .2, .65, .1] },
      kind: { c: 'feature_request', p: [.8, .05, .05, .05, .05] }, request: { p: .9 }, gap: { p: .8 },
    }]]);
    const manifest = { ...site, questions, presets: site.presets,
      runs: [run], default_run: 'run', categories: [{ id: 9, name: 'French Community' }, { id: 27, name: 'Feature Request' }],
      languages: [{ code: 'fr', name: 'French' }], tags: ['ORDA'], flags: { accepted: 4, closed: 1, archived: 2, enriched: 8, lang_guessed: 16 },
      stats: { topics: 1, enriched: 1, classified: 1, last_fetch: '2026-10-26T00:00:00Z' },
      generated_at: '2026-10-27T00:00:00Z', embeddings: { count: 1, bytes: 1000000, model: 'model', quant: 'int8', dim: 256 },
      tips: { runs: [run] }, voices: { entries: 1 } };
    const elements = new Map();
    const get = (selector) => {
      if (!elements.has(selector)) {
        const node = element();
        node.querySelector = get;
        node.querySelectorAll = () => [];
        node.closest = () => node;
        elements.set(selector, node);
      }
      return elements.get(selector);
    };
    const modes = ['kw', 'hyb', 'sem'].map((mode) => ({ ...element('BUTTON'), dataset: { mode } }));
    const nav = ['en', 'fr', 'ja'].map((hreflang) => ({ ...element('A'), hreflang }));
    const document = { body: get('body'), documentElement: element('HTML'),
      querySelector: get, getElementById: (id) => get('#' + id),
      querySelectorAll: (selector) => selector === '.modes button' ? modes : selector === '#locale-nav a' ? nav : [], addEventListener() {} };
    const location = { pathname: '/requests/', search: `?ui=${locale}&lang=fr&tracking=keep`, hash: '#entry',
      get href() { return 'https://example.test' + this.pathname + this.search + this.hash; } };
    const calls = [];
    let modelLoads = 0;
    const context = {
      ...state, ...filters, KeywordIndex, normalize, createRequestsLocale, localizeRequestsManifest,
      preferredLocale, localeURL, navigationURL, stateURL, loadOverlay,
      installLocale: (i18n, page) => installLocale(i18n, page, document, location),
      console, document, location, URLSearchParams, Intl, setTimeout, clearTimeout, CSS: { escape: (value) => value },
      IntersectionObserver: class { observe() {} },
      loadManifest: async () => manifest, loadTopics: async () => [topic],
      loadText: async (_manifest, topics) => { calls.push('text'); topics[0].para = 'Full English paragraph.'; },
      loadRun: async () => answers,
      getJSON: async (path) => {
        calls.push(path);
        assert.equal(topic.para, 'Full English paragraph.', 'canonical paragraphs must load before overlay hashing');
        return { version: 1, page: 'requests', locale, entries: { 42: {
          translation_version: '1', source_hash: stale ? 'stale' : await sourceHash({ title_en: topic.title_en, line: topic.line, para: topic.para }),
          fields: translations[locale],
        } } };
      },
      Semantic: class {
        available = true;
        async ready() { modelLoads++; }
        async similar() { return [{ id: 42, score: .91 }]; }
      },
    };
    const navigate = (_state, _title, url) => {
      const parsed = new URL(url, location.href);
      location.search = parsed.search; location.hash = parsed.hash;
    };
    const events = {};
    context.window = { addEventListener(name, callback) { events[name] = callback; }, history: { replaceState: navigate, pushState: navigate } };
    const source = readFileSync(new URL('../site/js/app.js', import.meta.url), 'utf8')
      .replace(/^import[\s\S]*?from '[^']+';\n/gm, '').replace(/init\(\)\.catch\([\s\S]*$/, '');
    vm.runInNewContext(source + `
      globalThis.ready = init();
      globalThis.api = { app, update, renderTable, renderDetail, renderSimilar, onModelProgress,
        clearFilters, openDetail, closeDetail, startSemantic, scoreTitle, answerBlock };
    `, context);
    await context.ready;
    const { api } = context;
    const i18n = createRequestsLocale(locale);
    const fallback = stale && locale !== 'en';
    const expected = locale === 'en' || stale ? topic.title_en : translations[locale].title_en;
    assert.ok(get('#results').innerHTML.includes(expected));
    assert.ok(get('#results').innerHTML.includes('Language &lt;original&gt;'), 'original text is untouched');
    assert.equal(get('#results').innerHTML.includes(i18n.t('English fallback')), fallback);
    assert.ok(get('#results').innerHTML.includes(i18n.date(topic.created)));
    assert.ok(get('#filter-body').innerHTML.includes(i18n.t('Classified by the model run')));
    assert.ok(get('[data-facet="a.fr"]').innerHTML.includes(i18n.t('High')));
    assert.ok(get('[data-facet="a.request"]').innerHTML.includes(i18n.t('Yes (≥ {percent})', { percent: i18n.pct(.5) })));
    assert.ok(!get('[data-facet="a.fr"]').innerHTML.includes('})'), 'score-cut tooltip contains no template residue');
    assert.ok(get('#presets').innerHTML.includes(i18n.t('Hidden feature requests')));
    assert.ok(get('#sort').innerHTML.includes(i18n.t('{label} score', { label: i18n.t('Feature request') })));
    assert.ok(get('#footer').innerHTML.includes(i18n.t('Source')));
    if (locale !== 'en') assert.ok(get('#footer').innerHTML.includes(i18n.t('Translation coverage: {translated} of {total} topics. Missing or stale translations use English.', { translated: fallback ? '0' : '1', total: '1' })));
    assert.equal(modelLoads, 0, 'keyword boot never downloads a semantic model');
    assert.equal(calls.some((call) => call.startsWith('locales/')), locale !== 'en');
    assert.equal(document.documentElement.lang, locale);
    assert.equal(new URLSearchParams(location.search).get('tracking'), 'keep');
    assert.equal(new URLSearchParams(location.search).get('lang'), 'fr');
    api.openDetail(42);
    await Promise.resolve();
    const detail = get('#detail-body').innerHTML;
    assert.ok(detail.includes(i18n.t('Source language')));
    assert.ok(detail.includes('Language &lt;original&gt;'));
    assert.ok(detail.includes('https://discuss.4d.com/t/public-source/42'));
    assert.ok(detail.includes(i18n.t('Model answers')));
    assert.ok(detail.includes(i18n.t('Find similar topics')));
    if (locale !== 'en' && !stale) {
      assert.ok(detail.includes('lang="en">Full English paragraph.'));
      assert.ok(detail.includes(i18n.t('English generated source')));
      assert.equal(api.app.topics[0].canonicalFields.para, 'Full English paragraph.');
      assert.equal(api.app.index.search(locale === 'ja' ? '日本語' : 'français')[0]?.id, 42);
      assert.equal(api.app.index.search('"Full English paragraph"')[0]?.id, 42, 'English canonical text remains searchable');
    }
    assert.ok(nav.every((link) => new URL(link.href, location.href).searchParams.get('t') === '42'));
    const table = api.renderTable(api.app.topics);
    assert.ok(table.includes(expected));
    assert.ok(table.includes(i18n.t('Category')));
    assert.ok(table.includes(i18n.number.format(7)));
    assert.ok(table.includes(new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(2.8)));
    const generatedTitle = api.app.topics[0].title_en;
    api.app.topics[0].title_en = '';
    api.update();
    assert.ok(get('#results').innerHTML.includes(i18n.t('Original title')), 'missing generated titles are explicitly labeled original');
    assert.ok(get('#results').innerHTML.includes('lang="fr"'));
    assert.ok(api.renderTable(api.app.topics).includes(i18n.t('Original title')));
    api.app.topics[0].title_en = generatedTitle;
    api.onModelProgress({ file: 'model', total: 1e6, loaded: .5e6 });
    assert.ok(get('#status').innerHTML.includes(i18n.t('Loading text-only EmbeddingGemma 2: {loaded} / {total} MB…',
      { loaded: i18n.decimal.format(.5), total: i18n.decimal.format(1) })));
    api.clearFilters();
    assert.equal(new URLSearchParams(location.search).get('scope'), 'all');
    assert.ok(!new URLSearchParams(location.search).has('a.fr'));
    assert.ok(nav.every((link) => new URL(link.href, location.href).searchParams.get('scope') === 'all'));
    api.closeDetail();
    assert.ok(nav.every((link) => !new URL(link.href, location.href).searchParams.has('t')));
    await events.popstate();
    assert.equal(api.app.state.scope, 'all');
    assert.deepEqual(Object.keys(api.app.state.ans), []);
    assert.equal(modelLoads, 0);
    api.app.results = [];
    api.app.state.q = 'not-found';
    api.app.index.search = () => [];
    api.update();
    assert.ok(get('#results').innerHTML.includes(i18n.t('No topics match.')));
    api.app.state.q = '';
    api.startSemantic();
    await Promise.resolve();
    assert.equal(modelLoads, 1, 'semantic model loads only after explicitly selecting semantic features');
  });
}
