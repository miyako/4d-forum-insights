import { test } from 'node:test';
import assert from 'node:assert/strict';
import { emptyState, parseState, serializeState, hasFilters } from '../site/js/state.js';
import { buildPredicates, applyFilters, facetCounts, sortTopics, rrf, dateToDays, daysToDate } from '../site/js/filter.js';
import { tokenize, normalize, KeywordIndex } from '../site/js/search.js';
import { decodeAnswer, decodeRun, bandOf } from '../site/js/data.js';
import { topK } from '../site/js/semantic.js';

const FR = {
  id: 'fr', type: 'score',
  levels: [0, 1, 2, 3, 4].map((v) => ({ key: String(v), value: v, label: `L${v}` })),
};
const KIND = { id: 'kind', type: 'choice', short: 'Kind', choices: ['feature_request', 'bug_report', 'question'].map((key) => ({ key, label: key })) };
const ASKS = { id: 'request', type: 'noul' };
const QUESTIONS = [FR, KIND, ASKS];
const FLAGS = { enriched: 8 };

test('state round-trips through the query string', () => {
  const qs = 'q=web+area&mode=hyb&cat=27&xcat=3,4&from=2023-01-01&votes=2&cls=1&a.fr=3,4&a.request=strong&sort=a.fr&view=table&t=42';
  const s = parseState(qs, QUESTIONS);
  assert.equal(s.q, 'web area');
  assert.equal(s.mode, 'hyb');
  assert.deepEqual(s.xcat, ['3', '4']);
  assert.deepEqual(s.ans.fr, ['3', '4']);
  assert.equal(s.ans.request, 'strong');
  assert.equal(s.t, 42);
  assert.equal(s.votes, 2);
  assert.ok(hasFilters(s));
  assert.deepEqual(parseState(serializeState(s), QUESTIONS), s);
  assert.equal(serializeState(emptyState()), '');
  assert.equal(parseState('mode=bogus').mode, 'kw');
  assert.ok(!hasFilters(parseState('q=x&sort=votes')));
});

test('unknown questions in the URL are kept (manifest-driven, no code changes)', () => {
  const s = parseState('a.impact=high,medium', QUESTIONS);
  assert.deepEqual(s.ans.impact, ['high', 'medium']);
});

const T = (id, o = {}) => ({ id, cat: 1, lang: 'en', tags: [], created: 19000, active: 19000, votes: 0, likes: 0, posts: 1, views: 1, flags: 0, ans: null, ...o });
const topics = [
  T(1, { cat: 27, votes: 5, ans: { fr: decodeAnswer(FR, [380, 0, 0, 50, 100, 850]), kind: decodeAnswer(KIND, [0, 900, 50, 50]), request: decodeAnswer(ASKS, 950) } }),
  T(2, { cat: 5, lang: 'fr', tags: ['orda'], ans: { fr: decodeAnswer(FR, [310, 0, 0, 100, 700, 200]), kind: decodeAnswer(KIND, [0, 600, 100, 300]), request: decodeAnswer(ASKS, 600) } }),
  T(3, { cat: 5, created: 18000, flags: 8, ans: { fr: decodeAnswer(FR, [40, 900, 100, 0, 0, 0]), kind: decodeAnswer(KIND, [2, 0, 100, 900]), request: decodeAnswer(ASKS, 20) } }),
  T(4, { cat: 6, tags: ['orda', 'web'] }),
];

test('decodeAnswer buckets scores to the nearest level and scales probabilities', () => {
  assert.equal(topics[0].ans.fr.lv, '4');
  assert.equal(topics[1].ans.fr.lv, '3');
  assert.equal(topics[2].ans.fr.lv, '0');
  assert.equal(decodeAnswer(FR, [250, 0, 0, 0, 0, 0]).lv, '3', 'ties go to the higher level');
  assert.equal(topics[0].ans.kind.c, 'feature_request');
  assert.equal(topics[0].ans.request.p, 0.95);
  assert.equal(decodeAnswer(KIND, [-1, 0, 0, 0]).c, null);
});

const FRB = { ...FR, bands: [{ key: 'high', label: 'High', quantile: 0.25 }, { key: 'medium', label: 'Medium', quantile: 0.05 }, { key: 'low', label: 'Low' }] };
const CUTS = [2.5, 1.68];

test('score bands come from the run\'s cut-offs (high → low), independent of the nearest level', () => {
  assert.equal(bandOf(FRB, 2.88, CUTS), 'high');
  assert.equal(bandOf(FRB, 2.5, CUTS), 'high', 'a cut-off is inclusive');
  assert.equal(bandOf(FRB, 2.49, CUTS), 'medium');
  assert.equal(bandOf(FRB, 1.68, CUTS), 'medium');
  assert.equal(bandOf(FRB, 0.7, CUTS), 'low');
  assert.equal(bandOf(FR, 3, CUTS), undefined, 'questions without bands');
  assert.equal(bandOf(FRB, 3, undefined), undefined, 'runs without cut-offs');
  const a = decodeAnswer(FRB, [262, 0, 0, 300, 400, 300], null, CUTS);
  assert.equal(a.lv, '3');
  assert.equal(a.b, 'high');
  const run = decodeRun({ run: 'r1', ids: [5, 6], answers: { fr: [[262, 0, 0, 300, 400, 300], [190, 0, 200, 700, 100, 0]] } },
    { questions: [FRB], runs: [{ id: 'r1', bands: { fr: { cuts: CUTS } } }] });
  assert.equal(run.get(5).fr.b, 'high');
  assert.equal(run.get(6).fr.b, 'medium');
});

test('score filters accept band keys and (for older links) level keys', () => {
  const list = [
    T(1, { ans: { fr: decodeAnswer(FRB, [262, 0, 0, 300, 400, 300], null, CUTS) } }),
    T(2, { ans: { fr: decodeAnswer(FRB, [190, 0, 200, 700, 100, 0], null, CUTS) } }),
    T(3, { ans: { fr: decodeAnswer(FRB, [80, 900, 100, 0, 0, 0], null, CUTS) } }),
  ];
  const run = (qs) => applyFilters(list, buildPredicates(parseState(qs, [FRB]), { questions: [FRB], flags: FLAGS })).map((t) => t.id);
  assert.deepEqual(run('a.fr=high'), [1]);
  assert.deepEqual(run('a.fr=high,medium'), [1, 2]);
  assert.deepEqual(run('a.fr=low'), [3]);
  assert.deepEqual(run('a.fr=3'), [1], 'legacy level link');
  const byBand = facetCounts(list, buildPredicates(parseState('a.fr=high', [FRB]), { questions: [FRB], flags: FLAGS }), 'a.fr', (t) => t.ans?.fr?.b);
  assert.deepEqual(Object.fromEntries(byBand), { high: 1, medium: 1, low: 1 });
});

test('decodeRun maps a run\'s own choice order onto the manifest order', () => {
  const manifest = { questions: [KIND] };
  const run = decodeRun({ ids: [7], keys: { kind: ['question', 'feature_request', 'bug_report'] }, answers: { kind: [[0, 800, 150, 50]] } }, manifest);
  const a = run.get(7).kind;
  assert.equal(a.c, 'question');
  assert.deepEqual(a.p, [0.15, 0.05, 0.8]);
});

test('hidden feature-request preset: likely FR outside category 27', () => {
  const s = parseState('a.fr=3,4&xcat=27', QUESTIONS);
  const out = applyFilters(topics, buildPredicates(s, { questions: QUESTIONS, flags: FLAGS }));
  assert.deepEqual(out.map((t) => t.id), [2]);
});

test('filters: tags, language, dates, votes, classified, enriched, noul thresholds', () => {
  const run = (qs) => applyFilters(topics, buildPredicates(parseState(qs, QUESTIONS), { questions: QUESTIONS, flags: FLAGS })).map((t) => t.id);
  assert.deepEqual(run('tag=orda'), [2, 4]);
  assert.deepEqual(run('lang=fr'), [2]);
  assert.deepEqual(run(`to=${daysToDate(18500)}`), [3]);
  assert.deepEqual(run('votes=1'), [1]);
  assert.deepEqual(run('cls=1'), [1, 2, 3]);
  assert.deepEqual(run('enriched=1'), [3]);
  assert.deepEqual(run('a.request=strong'), [1]);
  assert.deepEqual(run('a.request=yes'), [1, 2]);
  assert.deepEqual(run('a.request=no'), [3]);
  assert.deepEqual(run('a.kind=question'), [3]);
  assert.equal(dateToDays(daysToDate(19000)), 19000);
});

test('facet counts ignore the facet\'s own filter', () => {
  const preds = buildPredicates(parseState('cat=5&a.fr=3', QUESTIONS), { questions: QUESTIONS, flags: FLAGS });
  const byCat = facetCounts(topics, preds, 'cat', (t) => t.cat);
  assert.deepEqual([...byCat], [[5, 1]]);
  const byFr = facetCounts(topics, preds, 'a.fr', (t) => t.ans?.fr?.lv);
  assert.deepEqual(Object.fromEntries(byFr), { 3: 1, 0: 1 });
});

test('sorting by score, choice probability and metadata; missing values last', () => {
  const ids = (sort) => sortTopics(topics, sort, QUESTIONS, null).map((t) => t.id);
  assert.deepEqual(ids('a.fr'), [1, 2, 3, 4]);
  assert.deepEqual(ids('a.kind:question'), [3, 2, 1, 4]);
  assert.deepEqual(ids('votes')[0], 1);
  assert.deepEqual(ids('old')[0], 3);
  const rel = new Map([[4, 9], [2, 3]]);
  assert.deepEqual(sortTopics(topics, 'relevance', QUESTIONS, rel).map((t) => t.id).slice(0, 2), [4, 2]);
});

test('reciprocal rank fusion rewards agreement', () => {
  const f = rrf([[1, 2, 3], [3, 1]]);
  const order = [...f].sort((a, b) => b[1] - a[1]).map((x) => x[0]);
  assert.deepEqual(order, [1, 3, 2]);
});

test('tokenizer: words, diacritics, CJK bigrams', () => {
  assert.deepEqual(tokenize('Web-Area: PDF export!'), ['Web', 'Area', 'PDF', 'export']);
  assert.equal(normalize('Données'), 'donnees');
  assert.deepEqual(tokenize('印刷プレビュー'), ['印刷', '刷プ', 'プレ', 'レビ', 'ビュ', 'ュー']);
  assert.deepEqual(tokenize('4D の印刷'), ['4D', 'の印', '印刷']);
});

test('keyword index: AND, OR fallback, exclusion and phrases', () => {
  const docs = [
    { id: 1, title: 'Print a web area to PDF', title_en: null, line: 'printing web pages', tags: [] },
    { id: 2, title: 'Web server sessions', title_en: null, line: 'sessions in the web server', tags: ['web'] },
    { id: 3, title: 'Exporter en PDF', title_en: 'Export to PDF', line: 'export a document', tags: [] },
  ];
  const idx = new KeywordIndex(docs);
  const ids = (q) => idx.search(q).map((r) => r.id).sort();
  assert.deepEqual(ids('web pdf'), [1]);
  assert.deepEqual(ids('pdf'), [1, 3]);
  assert.deepEqual(ids('pdf -print'), [3]);
  assert.deepEqual(ids('"web server"'), [2]);
  assert.deepEqual(ids('sessions zzzunknown'), [2], 'falls back to OR');
  assert.deepEqual(ids('exporter'), [3]);
});

test('topK returns the best scores first', () => {
  assert.deepEqual(topK(new Float32Array([0.1, 0.9, 0.5]), [10, 11, 12], 2), [{ id: 11, score: Float32Array.from([0.9])[0] }, { id: 12, score: 0.5 }]);
});
