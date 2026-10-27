// Search state ⇄ query string, so every view is shareable.
//   q, mode (kw|hyb|sem), sort, run, view (list|table), t (open topic),
//   cat, xcat, lang, tag (comma lists), from, to (YYYY-MM-DD), votes, cls=1, enriched=1,
//   a.<question>=… (score bands or levels / choices as comma list; noul: yes|strong|no)

const LISTS = ['cat', 'xcat', 'lang', 'tag'];

export function emptyState() {
  return {
    q: '', mode: 'kw', sort: '', run: '', view: 'list', t: null,
    cat: [], xcat: [], lang: [], tag: [], from: '', to: '', votes: 0, cls: false, enriched: false,
    ans: {}, scope: '',
  };
}

const splitList = (v) => (v ? v.split(',').map((x) => x.trim()).filter(Boolean) : []);

export function parseState(search, questions = []) {
  const p = new URLSearchParams(search);
  const s = emptyState();
  s.scope = p.get('scope') === 'all' ? 'all' : '';
  s.q = p.get('q') || '';
  s.mode = ['kw', 'hyb', 'sem'].includes(p.get('mode')) ? p.get('mode') : 'kw';
  s.sort = p.get('sort') || '';
  s.run = p.get('run') || '';
  s.view = p.get('view') === 'table' ? 'table' : 'list';
  s.t = p.get('t') ? Number(p.get('t')) : null;
  for (const k of LISTS) s[k] = splitList(p.get(k));
  s.from = p.get('from') || '';
  s.to = p.get('to') || '';
  s.votes = Number(p.get('votes')) || 0;
  s.cls = p.get('cls') === '1';
  s.enriched = p.get('enriched') === '1';
  const types = Object.fromEntries(questions.map((q) => [q.id, q.type]));
  for (const [k, v] of p) {
    if (!k.startsWith('a.')) continue;
    const qid = k.slice(2);
    s.ans[qid] = types[qid] === 'noul' ? v : splitList(v);
  }
  return s;
}

export function serializeState(s) {
  const p = new URLSearchParams();
  if (s.scope === 'all') p.set('scope', 'all');
  if (s.q) p.set('q', s.q);
  if (s.mode !== 'kw') p.set('mode', s.mode);
  for (const k of LISTS) if (s[k].length) p.set(k, s[k].join(','));
  if (s.from) p.set('from', s.from);
  if (s.to) p.set('to', s.to);
  if (s.votes) p.set('votes', String(s.votes));
  if (s.cls) p.set('cls', '1');
  if (s.enriched) p.set('enriched', '1');
  for (const [qid, v] of Object.entries(s.ans)) {
    if (Array.isArray(v) ? v.length : v) p.set('a.' + qid, Array.isArray(v) ? v.join(',') : v);
  }
  if (s.sort) p.set('sort', s.sort);
  if (s.run) p.set('run', s.run);
  if (s.view !== 'list') p.set('view', s.view);
  if (s.t) p.set('t', String(s.t));
  return p.toString().replace(/%2C/g, ',');
}

export const REQUEST_STATE_KEYS = ['q', 'mode', 'sort', 'run', 'view', 't', ...LISTS,
  'from', 'to', 'votes', 'cls', 'enriched', 'scope'];

/** Only an unfiltered new visit gets the Requests preset; old/shared intent wins. */
export function parseRequestsState(search, questions = [], presets = []) {
  const params = new URLSearchParams(search);
  const explicit = [...params.keys()].some(key => REQUEST_STATE_KEYS.includes(key) || key.startsWith('a.'));
  if (explicit) return parseState(search, questions);
  const preset = presets.find(preset => preset.id === 'hidden-fr');
  return parseState(preset?.params || 'a.fr=high&xcat=27&sort=a.fr', questions);
}
/** True when any filter (not query/sort/view) is set. */
export function hasFilters(s) {
  return LISTS.some((k) => s[k].length) || s.from || s.to || s.votes || s.cls || s.enriched
    || Object.values(s.ans).some((v) => (Array.isArray(v) ? v.length : v));
}
