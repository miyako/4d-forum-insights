import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { resolve } from 'node:path';
import vm from 'node:vm';
import * as core from '../site/js/questions-core.js';
import { adaptPreview, preparePreview } from '../scripts/questions_preview.mjs';
import { sourceHash } from '../scripts/localization.mjs';
import { validatePublicPreview, installPublicPreview, gateQuestionsRoute } from '../scripts/questions_public_preview.mjs';
import { createLocale, preferredLocale, installLocale, localeURL, navigationURL, stateURL, modelDisclosure, loadOverlay } from '../site/js/locale.js';
import { KeywordIndex } from '../site/js/search.js';

const rating = (labels, choice) => ({ choice, probabilities: Object.fromEntries(Object.keys(labels).map((k) => [k, k === choice ? 1 : 0])) });
const row = (id = 1, overrides = {}) => ({
  id, title: 'How to query ORDA <safely>?', body: 'Thread <summary>.', active: '2026-01-01T00:00:00Z', lang: 'fr',
  accepted: false, closed: false, replies: 2,
  answers: {
    has_question: { noul: .5 }, coverage: rating(core.COVERAGE, 'partial'), impact: rating(core.IMPACT, 'significant'),
    technicality: rating(core.COMPLEXITY, 'moderate'), clarification: { noul: .5 },
    ...Object.fromEntries(Object.keys(core.DOMAINS).map((k) => ['domain_' + k, { noul: k === 'coding' ? .6 : .59 }])),
  }, ...overrides,
});
const data = (...entries) => ({ version: 1, entries });
const snapshot = (rows) => ({
  schema_version: 'questions-ui-private-preview-v1', private_preview: true, publish_allowed: false,
  items: rows.map((r) => ({ ...r, summary: r.body, solved: r.accepted, source_language: r.lang, raw_posts: 'DO NOT COPY', user: 'DO NOT COPY' })),
});

test('Questions decisions preserve scale, metadata independence, uncertainty and exact thresholds', () => {
  const r = row();
  r.answers.coverage = { choice: 'none', probabilities: { none: .4, mostly_unanswered: .3, partial: .1, mostly_answered: .1, full: .1 } };
  const [item] = core.buildQuestions(data({ ...r, accepted: true, closed: true, replies: 0 }));
  assert.equal(item.coverage.choice, 'none', 'solved does not promote model coverage');
  assert.deepEqual(item.coverage.probabilities, r.answers.coverage.probabilities);
  assert.equal(item.solved, true);
  assert.equal(item.closed, true);
  assert.equal(item.replies, 0);
  assert.deepEqual(item.domains, ['coding'], 'domain threshold inclusive, independent');
  assert.equal(item.questionProbability, .5);
  assert.equal(item.clarification, .5);
  const nonQuestion = row(2);
  nonQuestion.answers.has_question.noul = .499;
  assert.deepEqual(core.buildQuestions(data(nonQuestion)), []);
  for (const choice of Object.keys(core.COVERAGE)) {
    const r = row(); r.answers.coverage = rating(core.COVERAGE, choice);
    assert.equal(core.buildQuestions(data(r))[0].coverage.choice, choice);
  }
});

test('coverage icons use the exact five ordinal fills and unknown ratings stay neutral', () => {
  assert.deepEqual(Object.values(core.COVERAGE_FILL), [0, 25, 50, 75, 100]);
  for (const [key, fill] of Object.entries(core.COVERAGE_FILL)) {
    assert.match(core.coverageIcon(key), new RegExp(`--coverage-fill:${fill}%`));
    assert.match(core.coverageIcon(key), /aria-hidden="true"/);
  }
  assert.throws(() => core.coverageIcon('unclear'), /Unknown answer coverage/);
  for (const key of ['impact', 'complexity']) {
    assert.equal(core.RATING_TONES[key].unknown, 'unknown');
    assert.equal(new Set(Object.values(core.RATING_TONES[key])).size, 4);
  }
  assert.equal(core.RATING_TONES.impact.critical, 'high');
  assert.equal(core.RATING_TONES.complexity.complex, 'high');
});

test('malformed or duplicate datasets fail explicitly instead of hiding errors or inventing metadata', () => {
  for (const change of [
    (r) => { r.id = -1; }, (r) => { r.replies = undefined; }, (r) => { r.replies = -1; },
    (r) => { r.closed = undefined; }, (r) => { r.active = 'invalid'; },
    (r) => { r.answers.coverage.choice = 'unclear'; }, (r) => { r.answers.coverage.probabilities.none = 10; },
    (r) => { delete r.answers.coverage.probabilities.full; }, (r) => { r.answers.domain_coding.noul = NaN; },
  ]) {
    const r = row(); change(r);
    assert.throws(() => core.buildQuestions(data(r)), /Invalid Questions/);
  }
  assert.throws(() => core.buildQuestions(data(row(), row())), /Invalid Questions/);
  assert.throws(() => core.buildQuestions({ entries: [] }), /Unsupported/);
  const [unknown] = core.buildQuestions(data(row(1, { replies: null, accepted: null, closed: null, active: null })));
  assert.equal(unknown.replies, null);
  assert.equal(unknown.solved, null);
});

test('URL state round-trips, rejects unknown values and preserves explicit newest sort', () => {
  const state = core.parseQuestionsState('?q=ORDA&mode=hyb&coverage=none,partial&impact=critical&complexity=complex&domain=coding,network&solved=no&closed=yes&replies=zero&clarification=1&sort=new&t=42');
  assert.deepEqual(core.parseQuestionsState(core.serializeQuestionsState(state)), state);
  assert.equal(core.parseQuestionsState('').sort, '');
  assert.equal(state.sort, 'new');
  assert.deepEqual(core.parseQuestionsState('?coverage=unclear,full,full').coverage, ['full']);
  assert.equal(core.parseQuestionsState('?replies=bad&mode=bad&t=-1').t, 0);
});

test('filters distinguish missing reply/solved/closed metadata and omit their own facet', () => {
  const items = core.buildQuestions(data(row(1, { replies: 0, accepted: true }), row(2, { replies: null, accepted: null, closed: null }), row(3)));
  assert.deepEqual(core.filterQuestions(items, core.parseQuestionsState('?replies=zero')).map((it) => it.id), [1]);
  assert.deepEqual(core.filterQuestions(items, core.parseQuestionsState('?replies=unknown')).map((it) => it.id), [2]);
  assert.deepEqual(core.filterQuestions(items, core.parseQuestionsState('?replies=some&solved=no')).map((it) => it.id), [3]);
  assert.deepEqual(core.filterQuestions(items, core.parseQuestionsState('?closed=no')).map((it) => it.id), [1, 3]);
  assert.equal(core.filterQuestions(items, core.parseQuestionsState('?coverage=none')).length, 0);
  assert.equal(core.filterQuestions(items, core.parseQuestionsState('?coverage=none'), 'coverage').length, 3);
  assert.equal(core.filterQuestions(items, core.parseQuestionsState('?domain=coding&clarification=1')).length, 3);
});

test('semantic candidates are restricted by ID after global ranking and hybrid reuses RRF', () => {
  const items = core.buildQuestions(data(row(1), row(2)));
  const keyword = [{ id: 2, score: 3 }];
  const semantic = [{ id: 999, score: 10 }, { id: 1, score: .9 }, { id: 2, score: .8 }];
  assert.deepEqual(core.rankQuestions(items, 'orda', 'sem', keyword, semantic).items.map((it) => it.id), [1, 2]);
  const hybrid = core.rankQuestions(items, 'orda', 'hyb', keyword, semantic);
  assert.deepEqual(core.sortQuestions(hybrid.items, 'relevance', hybrid.relevance).map((it) => it.id), [2, 1]);
  assert.deepEqual(core.rankQuestions(items, 'orda', 'sem', keyword, null).items.map((it) => it.id), [2]);
});

test('rollout and private preview gates never collide with Requests manifest.questions', () => {
  const manifest = { questions: [{ id: 'fr' }], questions_view: { enabled: true, files: { entries: 'questions/entries.json' } } };
  assert.equal(core.questionsEnabled(manifest), true);
  assert.equal(core.questionsEnabled({ questions: manifest.questions }), false);
  assert.equal(core.questionsEnabled({ ...manifest, publish_allowed: false }), false);
  const preview = { ...manifest, private_preview: true, publish_allowed: false, questions_view: { ...manifest.questions_view, enabled: false, preview: true } };
  assert.equal(core.questionsPreview(preview), true);
  assert.equal(core.questionsEnabled(preview), false);
  assert.equal(core.questionsPreview({ ...preview, private_preview: false }), false);
  const link = {};
  core.installQuestionsLink({}, { getElementById: () => link }); assert.equal(link.hidden, true);
  core.installQuestionsLink(preview, { getElementById: () => link }); assert.equal(link.hidden, false);
  for (const page of ['requests', 'tips', 'voices']) {
    const html = readFileSync(new URL(`../site/${page}/index.html`, import.meta.url), 'utf8');
    assert.match(html, /id="questions-link" href="\.\.\/questions\/" hidden/);
    assert.equal(navigationURL('../questions/', 'ja', `https://example.test/${page}/?q=ORDA&mode=hyb&ui=ja`), '/questions/?ui=ja&q=ORDA&mode=hyb');
  }
});

test('Questions semantic infrastructure stays dormant without separate explicit authorization', () => {
  const embeddings = { count: 17042 };
  for (const questions_view of [undefined, {}, { semantic_enabled: false }, { semantic_enabled: 'true' }]) {
    assert.equal(core.questionsSemanticMeta({ embeddings, questions_view }), null);
  }
  assert.equal(core.questionsSemanticMeta({ embeddings, questions_view: { semantic_enabled: true } }), embeddings);
});

const publicData = () => ({
  schema_version: 'questions-ui-public-preview-v1', version: 1, public_preview: true, private_preview: false,
  publish_allowed: true, representative: false, generated_at: '2026-11-09T00:00:00Z', entries: [row()],
});

test('public preview requires separate approval flags and strips fields not used by the public UI', () => {
  const source = publicData();
  source.private_approval = 'NOT PUBLIC';
  source.entries[0].user = 'NOT PUBLIC';
  source.entries[0].answers.private_evidence = 'NOT PUBLIC';
  const validated = validatePublicPreview(source);
  assert.equal(JSON.stringify(validated).includes('NOT PUBLIC'), false);
  assert.deepEqual(validated.entries[0].answers.coverage, source.entries[0].answers.coverage);
  for (const flags of [{ private_preview: true }, { public_preview: false }, { publish_allowed: false },
    { representative: true }, { schema_version: 'questions-ui-private-preview-v1' }, { entries: [] }]) {
    assert.throws(() => validatePublicPreview({ ...source, ...flags }), /publication-approved/);
  }
  assert.throws(() => validatePublicPreview({ ...source, entries: Array.from({ length: 61 }, (_, i) => row(i + 1)) }), /small public/);
});

test('public sample install preserves Requests and independent preview/full/semantic rollout gates', () => {
  mkdirSync('build', { recursive: true });
  const temp = mkdtempSync(resolve('build/questions-public-test-'));
  try {
    const source = resolve(temp, 'source.json');
    const manifestFile = resolve(temp, 'manifest.json');
    writeFileSync(source, JSON.stringify(publicData()));
    writeFileSync(manifestFile, JSON.stringify({ questions: [{ id: 'fr' }] }));
    assert.equal(gateQuestionsRoute(temp), false);
    installPublicPreview(source, temp);
    const manifest = JSON.parse(readFileSync(manifestFile));
    assert.deepEqual(manifest.questions, [{ id: 'fr' }]);
    assert.equal(manifest.questions_view.enabled, false);
    assert.equal(core.questionsPublicPreview(manifest), true);
    assert.equal(core.questionsEnabled(manifest), false);
    assert.equal(core.questionsPreview(manifest), false);
    assert.equal(core.questionsSemanticMeta(manifest), null);
    assert.equal(gateQuestionsRoute(temp), true);
    assert.equal(core.questionsPublicPreview({ ...manifest, private_preview: true }), false);
    writeFileSync(manifestFile, JSON.stringify({ ...manifest, private_preview: true }));
    assert.throws(() => installPublicPreview(source, temp), /private site/);
    assert.throws(() => gateQuestionsRoute(temp), /cannot be deployed/);
    writeFileSync(manifestFile, JSON.stringify({ questions: [], questions_view: { enabled: true, files: { entries: 'questions/full.json' } } }));
    assert.throws(() => installPublicPreview(source, temp), /enabled production/);
  } finally { rmSync(temp, { recursive: true }); }
});

test('private adapter and staging retain Requests schema and never write private data into site/', () => {
  const preview = adaptPreview(snapshot([row()]));
  assert.equal(preview.entries[0].body, 'Thread <summary>.');
  assert.equal(preview.entries[0].lang, 'fr');
  assert.equal(Object.hasOwn(preview.entries[0], 'raw_posts'), false);
  assert.equal(Object.hasOwn(preview.entries[0], 'user'), false);
  assert.throws(() => adaptPreview({ ...snapshot([]), publish_allowed: true }), /explicitly private/);
  mkdirSync('build', { recursive: true });
  const temp = mkdtempSync(resolve('build/questions-ui-test-'));
  try {
    mkdirSync(resolve(temp, 'input'));
    writeFileSync(resolve(temp, 'snapshot.json'), JSON.stringify(snapshot([row()])));
    writeFileSync(resolve(temp, 'input/manifest.json'), JSON.stringify({ questions: [{ id: 'fr' }] }));
    const options = { snapshot: resolve(temp, 'snapshot.json'), data: resolve(temp, 'input'), output: resolve(temp, 'output') };
    preparePreview(options);
    const manifest = JSON.parse(readFileSync(resolve(temp, 'output/data/manifest.json')));
    assert.deepEqual(manifest.questions, [{ id: 'fr' }]);
    assert.equal(manifest.questions_view.enabled, false);
    assert.equal(manifest.questions_view.semantic_enabled, false);
    assert.equal(manifest.publish_allowed, false);
    assert.equal(manifest.private_preview, true);
    assert.throws(() => preparePreview({ ...options, output: 'site' }), /subdirectory/);
    assert.throws(() => preparePreview({ ...options, output: 'build' }), /subdirectory/);
  } finally { rmSync(temp, { recursive: true }); }
});

for (const locale of ['en', 'fr', 'ja']) for (const publication of ['private', 'public'])
  for (const translation of locale === 'en' ? ['missing'] : ['missing', 'current', 'stale'])
    test(`actual Questions module renders ${locale} ${publication} ${translation} with escaped summaries, filters, metadata, locale and state links`, async () => {
  const elements = new Map();
  const events = new Map();
  const element = () => ({
    nodeType: 1, tagName: 'DIV', childNodes: [], dataset: {}, innerHTML: '', textContent: '', value: '',
    hidden: false, classList: { add() {} }, setAttribute() {}, hasAttribute() { return false; },
    addEventListener(name, callback) { this['on' + name] = callback; },
    insertAdjacentHTML(_position, html) { this.innerHTML = html + this.innerHTML; },
  });
  const get = (selector) => {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  };
  const modes = ['kw', 'hyb', 'sem'].map((mode) => ({ ...element(), dataset: { mode } }));
  const document = {
    body: element(), documentElement: element(), querySelector: get,
    getElementById(id) { return id.startsWith('question-') ? null : get('#' + id); },
    querySelectorAll: (selector) => selector === '[data-mode]' ? modes : [],
    addEventListener: (name, callback) => events.set(name, callback),
  };
  const location = { pathname: '/questions/', search: `?ui=${locale}&tracking=keep`, hash: '',
    get href() { return 'https://example.test' + this.pathname + this.search; } };
  const navigate = (_state, _title, url) => { location.search = new URL(url, location.href).search; };
  const context = {
    ...core, KeywordIndex, createLocale, preferredLocale, localeURL, stateURL, modelDisclosure, loadOverlay,
    installLocale: (i18n, page) => installLocale(i18n, page, document, location),
    loadManifest: async () => ({ private_preview: publication === 'private', publish_allowed: publication === 'public', questions_view: { preview: true, public_preview: publication === 'public', enabled: false, files: { entries: 'questions/entries.json' } } }),
    getJSON: async (file) => file.startsWith('locales/') ? {
      version: 1, page: 'questions', locale, entries: translation === 'missing' ? {} : {
        1: { source_hash: translation === 'stale' ? '0'.repeat(64) : sourceHash({ title: row().title, body: row().body }),
          translation_version: '1', fields: {
            title: locale === 'fr' ? 'Question ORDA <traduite>' : 'ORDAの質問 <翻訳>',
            body: locale === 'fr' ? 'Résumé <traduit>.' : '要約 <翻訳>。',
          } },
      },
    } : ({ ...data(row(1, { accepted: true, closed: true, replies: 0 })), public_preview: publication === 'public', private_preview: publication === 'private', publish_allowed: publication === 'public' }),
    Semantic: class { available = false; },
    document, location, console, URLSearchParams, setTimeout,
    window: { history: { pushState: navigate, replaceState: navigate }, addEventListener: (name, callback) => events.set(name, callback) },
  };
  const source = readFileSync(new URL('../site/js/questions.js', import.meta.url), 'utf8')
    .replace(/^import[\s\S]*?from '[^']+';\n/gm, '').replace(/void init\(\);/, '');
  vm.runInNewContext(source + '\nglobalThis.ready = init(); globalThis.api = { app, update, controls, searchSemantic };', context);
  await context.ready;
  assert.equal(context.api.app.ready, true);
  const banner = publication === 'public'
    ? 'Public preview: a frozen sample of {count} threads, not the full dataset or a representative sample. Model assessments are unreviewed. Full processing continues; semantic search is not enabled.'
    : 'Private frozen sample: {count} threads, not representative. Full processing continues; nothing here is published.';
  assert.equal(get('#preview').textContent, createLocale(locale).t(banner, { count: '1' }));
  const expectedBody = translation === 'current' ? locale === 'fr' ? 'Résumé &lt;traduit&gt;.' : '要約 &lt;翻訳&gt;。' : 'Thread &lt;summary&gt;.';
  assert.ok(get('#results').innerHTML.includes(expectedBody));
  assert.ok(get('#results').innerHTML.includes(`lang="${translation === 'current' ? locale : 'en'}"`));
  if (locale !== 'en') assert.equal(get('#results').innerHTML.includes('translation-fallback'), translation !== 'current');
  assert.equal(context.api.app.items[0].lang, 'fr', 'source language is not changed by UI locale');
  assert.equal(context.api.app.items[0].solved, true);
  assert.equal(context.api.app.items[0].replies, 0);
  assert.match(get('#results').innerHTML, /requests\/\?t=1&amp;ui=/);
  assert.match(get('#results').innerHTML, /coverage-partial/);
  assert.match(get('#results').innerHTML, /--coverage-fill:50%/);
  assert.match(get('#results').innerHTML, /rating-tone tone-medium/);
  assert.ok(get('#results').innerHTML.includes(createLocale(locale).t('Unanswered (zero replies)')));
  assert.equal(modes[1].disabled, true);
  assert.equal(new URLSearchParams(location.search).get('tracking'), 'keep');
  context.api.app.state.replies = 'some'; context.api.update();
  assert.ok(get('#results').innerHTML.includes(createLocale(locale).t('No questions match. Try removing a filter.')));
  context.api.app.state.t = 1; context.api.update();
  assert.ok(get('#results').innerHTML.includes(createLocale(locale).t('Linked question shown separately from current filters.')));
  assert.ok(get('#results').innerHTML.includes(expectedBody));
  context.api.app.state.q = 'ORDA'; context.api.app.state.mode = 'sem';
  context.api.app.state.replies = 'any'; context.api.app.state.t = 0;
  context.api.update();
  assert.ok(get('#status').textContent.includes(createLocale(locale).t('Semantic search is unavailable in this preview; keyword search is ready.')));
  context.api.app.semantic = { available: true, meta: { count: 100 }, search: async () => { throw new Error('offline'); } };
  await context.api.searchSemantic();
  assert.ok(get('#status').textContent.includes('offline'));
  assert.match(get('#results').innerHTML, /coverage-partial/);
});
