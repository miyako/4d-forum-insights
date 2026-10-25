// Pure logic of the /voices/ page (no DOM; unit-tested with node --test).
//   URL state: q, mode (kw|hyb|sem), subject (comma list), settled (comma list), since (year),
//              sort (relevance|lively|new|people|views), all=1 (also threads the writer found no real exchange in)

export const SORTS = [
  { key: 'relevance', label: 'Relevance', needsQuery: true },
  { key: 'lively', label: 'Liveliest' },
  { key: 'new', label: 'Newest' },
  { key: 'people', label: 'Most participants' },
  { key: 'views', label: 'Most viewed' },
];

const list = (v) => (v ? v.split(',').map((x) => x.trim()).filter(Boolean) : []);

export function parseVoicesState(search) {
  const p = new URLSearchParams(search);
  return {
    q: p.get('q') || '',
    mode: ['kw', 'hyb', 'sem'].includes(p.get('mode')) ? p.get('mode') : 'kw',
    subject: list(p.get('subject')),
    settled: list(p.get('settled')),
    since: Number(p.get('since')) || 0,
    sort: SORTS.some((s) => s.key === p.get('sort')) ? p.get('sort') : '',
    all: p.get('all') === '1',
  };
}

export function serializeVoicesState(s) {
  const p = new URLSearchParams();
  if (s.q) p.set('q', s.q);
  if (s.mode !== 'kw') p.set('mode', s.mode);
  if (s.subject.length) p.set('subject', s.subject.join(','));
  if (s.settled.length) p.set('settled', s.settled.join(','));
  if (s.since) p.set('since', String(s.since));
  if (s.sort) p.set('sort', s.sort);
  if (s.all) p.set('all', '1');
  return p.toString().replace(/%2C/g, ',');
}

/**
 * voices/entries.json (columnar, see scripts/build_data.py build_voices) + decoded topics → items.
 * questions: manifest.voices.questions (settled, subject, exchange with their choice keys).
 */
export function buildVoices(entries, topics, questions) {
  const keys = Object.fromEntries(questions.map((q) => [q.id, q.choices.map((c) => c.key)]));
  const byId = new Map(topics.map((t) => [t.id, t]));
  const out = [];
  entries.ids.forEach((id, i) => {
    const topic = byId.get(id);
    if (!topic) return;
    out.push({
      id,
      topic,
      rank: entries.rank[i] / 1000,
      debate: entries.debate[i] / 1000,
      exchange: keys.exchange[entries.exchange[i]],
      settled: { key: keys.settled[entries.settled[i]], p: entries.settled_p[i] / 1000 },
      subject: { key: keys.subject[entries.subject[i]], p: entries.subject_p[i] / 1000 },
      participants: entries.participants[i],
      returning: entries.returning[i],
      days: entries.days[i],
      question: entries.question[i],
      positions: entries.positions[i] || [],
      outcome: entries.outcome[i] || '',
      ok: entries.ok[i],
    });
  });
  return out;
}

/** Debate level label (0-4) from the 0..1 debate value. */
export function debateLevel(debate, labels) {
  const i = Math.max(0, Math.min(labels.length - 1, Math.round(debate * (labels.length - 1))));
  return { i, label: labels[i] };
}

const yearOf = (days) => (days == null ? 0 : new Date(days * 86400000).getUTCFullYear());

/** Named predicates (one per facet) so facet counts can leave their own filter out. */
export function voicesPredicates(s) {
  const preds = [];
  if (s.subject.length) {
    const set = new Set(s.subject);
    preds.push({ key: 'subject', fn: (it) => set.has(it.subject.key) });
  }
  if (s.settled.length) {
    const set = new Set(s.settled);
    preds.push({ key: 'settled', fn: (it) => set.has(it.settled.key) });
  }
  if (!s.all) preds.push({ key: 'ok', fn: (it) => it.ok !== 0 });
  if (s.since) preds.push({ key: 'since', fn: (it) => yearOf(it.topic.created) >= s.since });
  return preds;
}

export function applyVoices(items, preds, skip = null) {
  return items.filter((it) => preds.every((p) => p.key === skip || p.fn(it)));
}

export function sortVoices(items, sort, relevance) {
  const key = {
    relevance: (it) => relevance?.get(it.id) ?? -Infinity,
    lively: (it) => it.rank,
    new: (it) => it.topic.created ?? 0,
    people: (it) => it.participants,
    views: (it) => it.topic.views,
  }[sort] || ((it) => it.rank);
  return [...items].sort((a, b) => key(b) - key(a) || b.rank - a.rank || (b.topic.created ?? 0) - (a.topic.created ?? 0));
}
