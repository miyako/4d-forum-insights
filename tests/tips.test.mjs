import { test } from 'node:test';
import assert from 'node:assert/strict';
import { decodeRun } from '../site/js/data.js';
import {
  parseTipsState, serializeTipsState, areasOf, typeOf, buildItems, tipsPredicates, applyTips, sortTips,
} from '../site/js/tips-core.js';

const AREA = ['language', 'interface', 'database', 'web', 'other'];
const QA = ['solved', 'open', 'tip', 'other'];
const QUESTIONS = [
  { id: 'area', type: 'choice', choices: AREA.map((key) => ({ key, label: key })) },
  { id: 'qa', type: 'choice', choices: QA.map((key) => ({ key, label: key })) },
];
const day = (s) => Math.floor(Date.parse(s + 'T00:00:00Z') / 86400000);

test('tips state round-trips through the query string and defaults to solved+tip', () => {
  const d = parseTipsState('');
  assert.deepEqual(d.type, ['solved', 'tip']);
  assert.equal(d.mode, 'kw');
  assert.equal(serializeTipsState(d), '');
  const s = parseTipsState('?q=orda+query&mode=hyb&area=database,web&type=tip&conf=70&since=2022&sort=views&all=1');
  assert.deepEqual(s, { q: 'orda query', mode: 'hyb', area: ['database', 'web'], type: ['tip'], conf: 70, since: 2022, sort: 'views', all: true });
  assert.deepEqual(parseTipsState('?' + serializeTipsState(s)), s);
  assert.equal(parseTipsState('?mode=x&sort=nope&conf=500').mode, 'kw');
  assert.equal(parseTipsState('?conf=500').conf, 95);
  assert.equal(parseTipsState('?sort=nope').sort, '');
});

test('areasOf: the chosen area first, plus others above the threshold', () => {
  assert.deepEqual(areasOf({ area: { c: 'web', p: [0.3, 0.05, 0.1, 0.45, 0.1] } }, AREA, 0.25),
    [{ key: 'web', p: 0.45 }, { key: 'language', p: 0.3 }]);
  // the choice is kept even below the threshold
  assert.deepEqual(areasOf({ area: { c: 'other', p: [0.2, 0.2, 0.2, 0.2, 0.2] } }, AREA, 0.25).map((a) => a.key), ['other']);
  assert.deepEqual(areasOf({}, AREA), []);
  assert.deepEqual(typeOf({ qa: { c: 'tip', p: [0.1, 0.1, 0.7, 0.1] } }, QA), { key: 'tip', p: 0.7 });
  assert.deepEqual(typeOf(null, QA), { key: null, p: 0 });
});

function fixture() {
  const run = {
    run: 'r', ids: [1, 2, 3, 4],
    answers: {
      area: [[2, 100, 50, 700, 100, 50], [0, 800, 100, 50, 25, 25], [1, 300, 400, 100, 100, 100], [3, 0, 0, 0, 1000, 0]],
      qa: [[0, 900, 50, 25, 25], [2, 100, 100, 600, 200], [1, 100, 800, 50, 50], [0, 450, 400, 100, 50]],
    },
  };
  const answers = decodeRun(run, { questions: QUESTIONS, runs: [{ id: 'r' }] });
  const topics = [1, 2, 3, 4, 5].map((id) => ({
    id, title: `T${id}`, title_en: `Title ${id}`, line: `line ${id}`, tags: [],
    created: day(`${2018 + id}-06-01`), views: id * 10, likes: 5 - id,
  }));
  const entries = new Map([
    [1, { mode: 'solved', headline: 'How do I query?', body: 'Use ds.find.', ok: 1 }],
    [2, { mode: 'tip', headline: 'Speed up loops', body: 'Use a collection.', ok: 0 }],
    [4, { mode: 'solved', headline: 'Why REST 401?', body: 'Check the session.', ok: 2 }],
  ]);
  return buildItems(topics, answers, entries, { area: AREA, qa: QA });
}

test('buildItems: classified topics only, entry text first, summary as fallback', () => {
  const items = fixture();
  assert.deepEqual(items.map((i) => i.id), [1, 2, 3, 4]);
  assert.equal(items[0].headline, 'How do I query?');
  assert.equal(items[2].headline, 'Title 3');
  assert.equal(items[2].body, 'line 3');
  assert.deepEqual(items[0].areas.map((a) => a.key), ['database']);
  assert.deepEqual(items[2].areas.map((a) => a.key), ['interface', 'language']);
});

test('tips filters: type + confidence, areas (any of), hides unanswered entries unless all=1', () => {
  const items = fixture();
  const ids = (q) => applyTips(items, tipsPredicates(parseTipsState(q))).map((i) => i.id).sort();
  assert.deepEqual(ids(''), [1, 4]); // 2 is a tip whose entry says "not answered"
  assert.deepEqual(ids('?all=1'), [1, 2, 4]);
  assert.deepEqual(ids('?conf=50'), [1]);
  assert.deepEqual(ids('?type=open'), [3]);
  assert.deepEqual(ids('?type=solved,open,tip&area=language&all=1'), [2, 3]);
  assert.deepEqual(ids('?type=solved,open,tip,other&since=2022'), [4]);
  // facet counts leave their own filter out
  const preds = tipsPredicates(parseTipsState('?area=web'));
  assert.deepEqual(applyTips(items, preds).map((i) => i.id), [4]);
  assert.deepEqual(applyTips(items, preds, 'area').map((i) => i.id), [1, 4]);
});

test('sortTips: relevance, newest, views, likes', () => {
  const items = fixture();
  assert.deepEqual(sortTips(items, 'new').map((i) => i.id), [4, 3, 2, 1]);
  assert.deepEqual(sortTips(items, 'likes').map((i) => i.id), [1, 2, 3, 4]);
  assert.deepEqual(sortTips(items, 'relevance', new Map([[2, 3], [3, 9]])).map((i) => i.id), [3, 2, 4, 1]);
});
