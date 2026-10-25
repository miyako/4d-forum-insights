import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  parseVoicesState, serializeVoicesState, buildVoices, debateLevel, voicesPredicates, applyVoices, sortVoices,
} from '../site/js/voices-core.js';

const QUESTIONS = [
  { id: 'settled', choices: ['consensus', 'decided', 'unresolved', 'none'].map((key) => ({ key, label: key })) },
  { id: 'subject', choices: ['technical', 'product', 'business', 'community'].map((key) => ({ key, label: key })) },
  { id: 'exchange', choices: ['help', 'views', 'news', 'social'].map((key) => ({ key, label: key })) },
];
const day = (s) => Math.floor(Date.parse(s + 'T00:00:00Z') / 86400000);

test('voices state round-trips through the query string', () => {
  const d = parseVoicesState('');
  assert.deepEqual(d, { q: '', mode: 'kw', subject: [], settled: [], since: 0, sort: '', all: false });
  assert.equal(serializeVoicesState(d), '');
  const s = parseVoicesState('?q=linux&mode=sem&subject=product,business&settled=unresolved&since=2023&sort=people&all=1');
  assert.deepEqual(s, { q: 'linux', mode: 'sem', subject: ['product', 'business'], settled: ['unresolved'], since: 2023, sort: 'people', all: true });
  assert.deepEqual(parseVoicesState('?' + serializeVoicesState(s)), s);
  assert.equal(parseVoicesState('?sort=nope&mode=x').sort, '');
  assert.equal(parseVoicesState('?mode=x').mode, 'kw');
});

function fixture() {
  const topics = [
    { id: 1, title: 'Linux', created: day('2026-03-10'), views: 900 },
    { id: 2, title: 'Arrays', created: day('2025-12-09'), views: 800 },
    { id: 3, title: 'Volaris', created: day('2025-01-23'), views: 950 },
  ];
  const entries = {
    ids: [1, 2, 3, 99],                   // 99: not in topics (deleted) → skipped
    rank: [400, 300, 500, 100],
    debate: [500, 750, 600, 250],
    exchange: [1, 1, 2, 1],
    settled: [2, 0, 2, 3],
    settled_p: [690, 800, 550, 500],
    subject: [1, 0, 2, 0],
    subject_p: [720, 900, 610, 500],
    participants: [8, 13, 12, 5],
    returning: [2, 3, 4, 2],
    days: [41, 8, 5, 1],
    question: ['Should 4D run on Linux?', 'Do arrays have a future?', 'Why Volaris?', 'x?'],
    positions: [['Some want it.', 'Others doubt it.'], ['Keep them.', 'Use collections.'], ['A', 'B'], []],
    outcome: ['Open.', 'Consensus.', 'Divided.', ''],
    ok: [1, 1, 0, 1],
  };
  return buildVoices(entries, topics, QUESTIONS);
}

test('buildVoices decodes the columnar entries', () => {
  const items = fixture();
  assert.equal(items.length, 3);
  const [linux] = items;
  assert.equal(linux.rank, 0.4);
  assert.equal(linux.exchange, 'views');
  assert.deepEqual(linux.settled, { key: 'unresolved', p: 0.69 });
  assert.deepEqual(linux.subject, { key: 'product', p: 0.72 });
  assert.deepEqual(linux.positions, ['Some want it.', 'Others doubt it.']);
  assert.equal(items[2].exchange, 'news');
});

test('debateLevel maps 0..1 to the five labels', () => {
  const labels = ['none', 'minor', 'some', 'clear', 'strong'];
  assert.deepEqual(debateLevel(0, labels), { i: 0, label: 'none' });
  assert.deepEqual(debateLevel(0.5, labels), { i: 2, label: 'some' });
  assert.deepEqual(debateLevel(0.6, labels), { i: 2, label: 'some' });
  assert.deepEqual(debateLevel(1.2, labels), { i: 4, label: 'strong' });
});

test('filters, facets and sorting', () => {
  const items = fixture();
  let preds = voicesPredicates(parseVoicesState(''));
  // the writer found no real exchange in #3: hidden by default, shown with all=1
  assert.deepEqual(sortVoices(applyVoices(items, preds), 'lively').map((it) => it.id), [1, 2]);
  preds = voicesPredicates(parseVoicesState('?all=1'));
  assert.deepEqual(sortVoices(applyVoices(items, preds), 'lively').map((it) => it.id), [3, 1, 2]);
  assert.deepEqual(sortVoices(applyVoices(items, preds), 'people').map((it) => it.id), [2, 3, 1]);
  assert.deepEqual(sortVoices(applyVoices(items, preds), 'new').map((it) => it.id), [1, 2, 3]);
  preds = voicesPredicates(parseVoicesState('?all=1&settled=unresolved&subject=product'));
  assert.deepEqual(applyVoices(items, preds).map((it) => it.id), [1]);
  // facet counts leave their own filter out
  assert.deepEqual(applyVoices(items, preds, 'subject').map((it) => it.id), [1, 3]);
  preds = voicesPredicates(parseVoicesState('?since=2026'));
  assert.deepEqual(applyVoices(items, preds).map((it) => it.id), [1]);
  const rel = new Map([[2, 0.9], [1, 0.1]]);
  assert.deepEqual(sortVoices(items.slice(0, 2), 'relevance', rel).map((it) => it.id), [2, 1]);
});
