// Pure filtering / faceting / sorting over decoded topics. No DOM here (unit-tested with node --test).

export const DAY_MS = 86400000;
export const dateToDays = (s) => (s ? Math.floor(Date.parse(s + 'T00:00:00Z') / DAY_MS) : null);
export const daysToDate = (d) => (d == null ? '' : new Date(d * DAY_MS).toISOString().slice(0, 10));

export const NOUL_FILTERS = [
  { key: 'yes', label: 'Yes (≥ 50%)', test: (p) => p >= 0.5 },
  { key: 'strong', label: 'Strong yes (≥ 80%)', test: (p) => p >= 0.8 },
  { key: 'no', label: 'No (< 50%)', test: (p) => p < 0.5 },
];

/**
 * Build named predicates from the state. Each facet key maps to one predicate so facet counts can be
 * computed "with every filter except this one".
 * ctx: { questions: [question meta for the current run], flags }
 */
export function buildPredicates(state, ctx) {
  const preds = [];
  const add = (key, fn) => preds.push({ key, fn });
  if (state.cat.length) {
    const s = new Set(state.cat.map(Number));
    add('cat', (t) => s.has(t.cat));
  }
  if (state.xcat.length) {
    const s = new Set(state.xcat.map(Number));
    add('xcat', (t) => !s.has(t.cat));
  }
  if (state.lang.length) {
    const s = new Set(state.lang);
    add('lang', (t) => s.has(t.lang));
  }
  if (state.tag.length) {
    const s = state.tag;
    add('tag', (t) => t.tags.some((x) => s.includes(x)));
  }
  const from = dateToDays(state.from), to = dateToDays(state.to);
  if (from != null || to != null) {
    add('date', (t) => t.created != null && (from == null || t.created >= from) && (to == null || t.created <= to));
  }
  if (state.votes > 0) add('votes', (t) => t.votes >= state.votes);
  if (state.cls) add('cls', (t) => t.ans != null);
  if (state.enriched) add('enriched', (t) => (t.flags & ctx.flags.enriched) !== 0);
  for (const q of ctx.questions) {
    const v = state.ans[q.id];
    if (!v || (Array.isArray(v) && !v.length)) continue;
    if (q.type === 'score') {
      const s = new Set(v);
      add('a.' + q.id, (t) => { const a = t.ans?.[q.id]; return !!a && s.has(a.lv); });
    } else if (q.type === 'choice') {
      const s = new Set(v);
      add('a.' + q.id, (t) => { const a = t.ans?.[q.id]; return !!a && s.has(a.c); });
    } else {
      const f = NOUL_FILTERS.find((x) => x.key === v);
      if (f) add('a.' + q.id, (t) => { const a = t.ans?.[q.id]; return !!a && f.test(a.p); });
    }
  }
  return preds;
}

export function applyFilters(topics, preds, skipKey = null) {
  const active = skipKey ? preds.filter((p) => p.key !== skipKey) : preds;
  if (!active.length) return topics;
  return topics.filter((t) => active.every((p) => p.fn(t)));
}

/** Count values for one facet over the set filtered by every other predicate. */
export function facetCounts(base, preds, key, values) {
  const set = applyFilters(base, preds, key);
  const counts = new Map();
  for (const t of set) {
    const v = values(t);
    if (v == null) continue;
    if (Array.isArray(v)) for (const x of v) counts.set(x, (counts.get(x) || 0) + 1);
    else counts.set(v, (counts.get(v) || 0) + 1);
  }
  return counts;
}

/** Sort options for the current run: metadata + one per score/noul question + one per choice. */
export function sortOptions(questions, hasQuery) {
  const opts = [];
  if (hasQuery) opts.push({ key: 'relevance', label: 'Relevance' });
  for (const q of questions) {
    if (q.type === 'score') opts.push({ key: 'a.' + q.id, label: `${q.label} score`, group: 'Model' });
    else if (q.type === 'noul') opts.push({ key: 'a.' + q.id, label: `${q.label} (p)`, group: 'Model' });
    else for (const c of q.choices) opts.push({ key: `a.${q.id}:${c.key}`, label: `${q.short}: P(${c.label})`, group: 'Model' });
  }
  opts.push(
    { key: 'active', label: 'Latest activity', group: 'Topic' },
    { key: 'new', label: 'Newest', group: 'Topic' },
    { key: 'old', label: 'Oldest', group: 'Topic' },
    { key: 'votes', label: 'Votes', group: 'Topic' },
    { key: 'likes', label: 'Likes', group: 'Topic' },
    { key: 'posts', label: 'Replies', group: 'Topic' },
    { key: 'views', label: 'Views', group: 'Topic' },
  );
  return opts;
}

/** Numeric sort key (higher first) or null when the topic has no value (sorted last). */
export function sortKeyFn(sort, questions, relevance) {
  const meta = { active: 'active', new: 'created', votes: 'votes', likes: 'likes', posts: 'posts', views: 'views' };
  if (sort === 'relevance') return (t) => relevance?.get(t.id) ?? null;
  if (sort === 'old') return (t) => (t.created == null ? null : -t.created);
  if (meta[sort]) return (t) => t[meta[sort]] ?? null;
  if (sort.startsWith('a.')) {
    const [qid, choice] = sort.slice(2).split(':');
    const q = questions.find((x) => x.id === qid);
    if (!q) return () => null;
    if (q.type === 'score') return (t) => t.ans?.[qid]?.s ?? null;
    if (q.type === 'noul') return (t) => t.ans?.[qid]?.p ?? null;
    const idx = q.choices.findIndex((c) => c.key === choice);
    return (t) => t.ans?.[qid]?.p[idx] ?? null;
  }
  return () => null;
}

export function sortTopics(list, sort, questions, relevance) {
  const key = sortKeyFn(sort, questions, relevance);
  const keyed = list.map((t) => [key(t), t]);
  keyed.sort((a, b) => {
    if (a[0] == null && b[0] == null) return b[1].active - a[1].active;
    if (a[0] == null) return 1;
    if (b[0] == null) return -1;
    return b[0] - a[0] || (relevance ? (relevance.get(b[1].id) ?? 0) - (relevance.get(a[1].id) ?? 0) : 0) || b[1].active - a[1].active;
  });
  return keyed.map((x) => x[1]);
}

/** Reciprocal-rank fusion of ranked id lists → Map(id → fused score). */
export function rrf(lists, k = 60, weights = null) {
  const out = new Map();
  lists.forEach((ids, li) => {
    const w = weights ? weights[li] : 1;
    ids.forEach((id, rank) => out.set(id, (out.get(id) || 0) + w / (k + rank + 1)));
  });
  return out;
}
