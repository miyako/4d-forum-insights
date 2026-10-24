// Pure logic of the /tips/ knowledge base page (no DOM; unit-tested with node --test).
//   URL state: q, mode (kw|hyb|sem), area (comma list), type (comma list of qa choices; default solved,tip),
//              conf (minimum % probability of the type), since (year), sort (relevance|new|views|likes), all=1

export const TYPES_DEFAULT = ['solved', 'tip'];
export const SORTS = [
  { key: 'relevance', label: 'Relevance', needsQuery: true },
  { key: 'new', label: 'Newest' },
  { key: 'views', label: 'Most viewed' },
  { key: 'likes', label: 'Most liked' },
];

const list = (v) => (v ? v.split(',').map((x) => x.trim()).filter(Boolean) : []);

export function parseTipsState(search) {
  const p = new URLSearchParams(search);
  const type = list(p.get('type'));
  return {
    q: p.get('q') || '',
    mode: ['kw', 'hyb', 'sem'].includes(p.get('mode')) ? p.get('mode') : 'kw',
    area: list(p.get('area')),
    type: type.length ? type : [...TYPES_DEFAULT],
    conf: Math.min(95, Math.max(0, Number(p.get('conf')) || 0)),
    since: Number(p.get('since')) || 0,
    sort: SORTS.some((s) => s.key === p.get('sort')) ? p.get('sort') : '',
    all: p.get('all') === '1',
  };
}

export function serializeTipsState(s) {
  const p = new URLSearchParams();
  if (s.q) p.set('q', s.q);
  if (s.mode !== 'kw') p.set('mode', s.mode);
  if (s.area.length) p.set('area', s.area.join(','));
  if (s.type.join(',') !== TYPES_DEFAULT.join(',')) p.set('type', s.type.join(','));
  if (s.conf) p.set('conf', String(s.conf));
  if (s.since) p.set('since', String(s.since));
  if (s.sort) p.set('sort', s.sort);
  if (s.all) p.set('all', '1');
  return p.toString().replace(/%2C/g, ',');
}

/** Areas of a topic: the model's choice plus every other area with probability >= minP, best first. */
export function areasOf(ans, keys, minP = 0.25) {
  if (!ans?.area) return [];
  const { c, p } = ans.area;
  const out = keys.map((k, i) => [k, p[i] || 0]).filter(([k, v]) => k === c || v >= minP);
  out.sort((a, b) => (b[0] === c) - (a[0] === c) || b[1] - a[1]);
  return out.map(([k, v]) => ({ key: k, p: v }));
}

/** Q&A type of a topic and the model's probability for it. */
export function typeOf(ans, keys) {
  if (!ans?.qa?.c) return { key: null, p: 0 };
  return { key: ans.qa.c, p: ans.qa.p[keys.indexOf(ans.qa.c)] || 0 };
}

/**
 * Build the knowledge-base items from decoded topics.
 * topics: decoded topics; answers: Map(id → decoded tips answers); entries: Map(id → {mode, headline, body, ok});
 * keys: {area: [...], qa: [...]}.
 */
export function buildItems(topics, answers, entries, keys, minP = 0.25) {
  const out = [];
  for (const t of topics) {
    const ans = answers.get(t.id);
    if (!ans) continue;
    const e = entries.get(t.id) || null;
    out.push({
      id: t.id,
      topic: t,
      areas: areasOf(ans, keys.area, minP),
      type: typeOf(ans, keys.qa),
      entry: e,
      headline: e?.headline || t.title_en || t.title,
      body: e?.body || t.line || '',
    });
  }
  return out;
}

const yearOf = (days) => (days == null ? 0 : new Date(days * 86400000).getUTCFullYear());

/** Named predicates (one per facet) so facet counts can leave their own filter out. */
export function tipsPredicates(s) {
  const preds = [];
  if (s.area.length) {
    const set = new Set(s.area);
    preds.push({ key: 'area', fn: (it) => it.areas.some((a) => set.has(a.key)) });
  }
  const types = new Set(s.type);
  const conf = s.conf / 100;
  preds.push({ key: 'type', fn: (it) => types.has(it.type.key) && it.type.p >= conf - 1e-9 });
  if (!s.all) preds.push({ key: 'ok', fn: (it) => !it.entry || it.entry.ok !== 0 });
  if (s.since) preds.push({ key: 'since', fn: (it) => yearOf(it.topic.created) >= s.since });
  return preds;
}

export function applyTips(items, preds, skip = null) {
  return items.filter((it) => preds.every((p) => p.key === skip || p.fn(it)));
}

export function sortTips(items, sort, relevance) {
  const key = {
    relevance: (it) => relevance?.get(it.id) ?? -Infinity,
    new: (it) => it.topic.created ?? 0,
    views: (it) => it.topic.views,
    likes: (it) => it.topic.likes,
  }[sort] || ((it) => it.topic.created ?? 0);
  return [...items].sort((a, b) => key(b) - key(a) || (b.topic.created ?? 0) - (a.topic.created ?? 0));
}
