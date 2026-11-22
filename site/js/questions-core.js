import { rrf } from './filter.js';

export const COVERAGE = {
  none: 'No useful answer', mostly_unanswered: 'Mostly unanswered', partial: 'Partially answered',
  mostly_answered: 'Mostly answered', full: 'Fully answered',
};
export const COVERAGE_FILL = { none: 0, mostly_unanswered: 25, partial: 50, mostly_answered: 75, full: 100 };
export function coverageIcon(key) {
  const fill = COVERAGE_FILL[key];
  if (fill === undefined) throw new Error(`Unknown answer coverage: ${key}`);
  return `<i class="coverage-icon" style="--coverage-fill:${fill}%" aria-hidden="true"></i>`;
}
export const RATING_TONES = {
  impact: { critical: 'high', significant: 'medium', minor: 'low', unknown: 'unknown' },
  complexity: { complex: 'high', moderate: 'medium', straightforward: 'low', unknown: 'unknown' },
};
export const IMPACT = { critical: 'Critical', significant: 'Significant', minor: 'Minor', unknown: 'Unknown' };
export const COMPLEXITY = { complex: 'Complex', moderate: 'Moderate', straightforward: 'Straightforward', unknown: 'Unknown' };
export const DOMAINS = {
  coding: 'Coding', interface: 'User interface', administration: 'Administration', setup: 'Installation & setup',
  migration: 'Migration', network: 'Web & network', database: 'Database management', integration: 'Integration', other: 'Other',
};
export const STATE_KEYS = ['q', 'mode', 'coverage', 'impact', 'complexity', 'domain', 'solved', 'closed', 'replies', 'clarification', 'sort', 't'];
const selected = (params, key, choices) => [...new Set((params.get(key) || '').split(',').filter((v) => Object.hasOwn(choices, v)))];
const oneOf = (value, choices, fallback) => choices.includes(value) ? value : fallback;

export function parseQuestionsState(search) {
  const p = new URLSearchParams(search);
  const id = Number(p.get('t'));
  return {
    q: p.get('q') || '', mode: oneOf(p.get('mode'), ['kw', 'hyb', 'sem'], 'kw'),
    coverage: selected(p, 'coverage', COVERAGE), impact: selected(p, 'impact', IMPACT),
    complexity: selected(p, 'complexity', COMPLEXITY), domain: selected(p, 'domain', DOMAINS),
    solved: oneOf(p.get('solved'), ['any', 'yes', 'no'], 'any'),
    closed: oneOf(p.get('closed'), ['any', 'yes', 'no'], 'any'),
    replies: oneOf(p.get('replies'), ['any', 'zero', 'some', 'unknown'], 'any'),
    clarification: p.get('clarification') === '1',
    sort: oneOf(p.get('sort'), ['new', 'relevance', 'impact'], ''),
    t: Number.isSafeInteger(id) && id > 0 ? id : 0,
  };
}

export function serializeQuestionsState(s) {
  const p = new URLSearchParams();
  if (s.q) p.set('q', s.q);
  if (s.mode !== 'kw') p.set('mode', s.mode);
  for (const key of ['coverage', 'impact', 'complexity', 'domain']) if (s[key].length) p.set(key, s[key].join(','));
  for (const key of ['solved', 'closed', 'replies']) if (s[key] !== 'any') p.set(key, s[key]);
  if (s.clarification) p.set('clarification', '1');
  if (s.sort) p.set('sort', s.sort);
  if (s.t) p.set('t', String(s.t));
  return p.toString();
}

export const questionsEnabled = (manifest) => manifest?.private_preview !== true && manifest?.publish_allowed !== false
  && manifest?.questions_view?.enabled === true
  && manifest.questions_view.preview !== true && Boolean(manifest.questions_view.files?.entries);

export const questionsPreview = (manifest) => manifest?.private_preview === true
  && manifest.publish_allowed === false && manifest.questions_view?.preview === true
  && Boolean(manifest.questions_view.files?.entries);

export const questionsPublicPreview = (manifest) => manifest?.private_preview !== true
  && manifest?.publish_allowed !== false && manifest?.questions_view?.public_preview === true
  && manifest.questions_view.preview === true && manifest.questions_view.enabled === false
  && Boolean(manifest.questions_view.files?.entries);

export const questionsSemanticMeta = (manifest) => manifest?.questions_view?.semantic_enabled === true
  ? manifest.embeddings : null;

export function installQuestionsLink(manifest, document = globalThis.document) {
  const link = document.getElementById('questions-link');
  if (link) link.hidden = !(questionsEnabled(manifest) || questionsPreview(manifest) || questionsPublicPreview(manifest));
}

const probability = (v) => Number.isFinite(v) && v >= 0 && v <= 1;
function rating(value, labels, id, key) {
  if (!value || !Object.hasOwn(labels, value.choice) || !value.probabilities
    || Object.keys(value.probabilities).length !== Object.keys(labels).length
    || !Object.keys(labels).every((k) => probability(value.probabilities[k]))
    || Math.abs(Object.values(value.probabilities).reduce((a, b) => a + b, 0) - 1) > .02) {
    throw new Error(`Invalid Questions ${key} rating for topic ${id}`);
  }
  return value;
}

export function buildQuestions(data) {
  if (data?.version !== 1 || !Array.isArray(data.entries)) throw new Error('Unsupported Questions dataset');
  const ids = new Set();
  return data.entries.map((row) => {
    if (!Number.isSafeInteger(row.id) || row.id <= 0 || ids.has(row.id)
      || typeof row.title !== 'string' || !row.title.trim() || !probability(row.answers?.has_question?.noul)) {
      throw new Error(`Invalid Questions entry: ${row.id}`);
    }
    ids.add(row.id);
    const a = row.answers;
    const coverage = rating(a.coverage, COVERAGE, row.id, 'coverage');
    const impact = rating(a.impact, IMPACT, row.id, 'impact');
    const complexity = rating(a.technicality, COMPLEXITY, row.id, 'complexity');
    if (!(row.replies === null || Number.isSafeInteger(row.replies) && row.replies >= 0)
      || ![true, false, null].includes(row.accepted) || ![true, false, null].includes(row.closed)
      || !probability(a.clarification?.noul)
      || !Object.keys(DOMAINS).every((k) => probability(a['domain_' + k]?.noul))
      || ![row.headline, row.body].every((v) => v == null || typeof v === 'string')) {
      throw new Error(`Invalid Questions metadata for topic ${row.id}`);
    }
    const active = row.active == null ? null : Date.parse(row.active);
    if (active !== null && !Number.isFinite(active)) throw new Error(`Invalid Questions date for topic ${row.id}`);
    return {
      id: row.id, title: row.title, headline: row.headline || row.title, body: row.body || '',
      active, lang: row.lang || '', tags: Array.isArray(row.tags) ? row.tags.filter((v) => typeof v === 'string') : [],
      replies: row.replies, solved: row.accepted, closed: row.closed,
      coverage, impact, complexity, clarification: a.clarification.noul,
      questionProbability: a.has_question.noul, domainProbabilities: Object.fromEntries(Object.keys(DOMAINS).map((k) => [k, a['domain_' + k].noul])),
      warnings: Array.isArray(row.evidence_warnings) ? row.evidence_warnings.filter((v) => typeof v === 'string') : [],
      decisionModel: row.decision_model || '', decisionVersion: row.decision_version || '',
      hasQuestion: a.has_question.noul >= .5,
      domains: Object.keys(DOMAINS).filter((k) => a['domain_' + k].noul >= .6),
    };
  }).filter((it) => it.hasQuestion);
}

export function filterQuestions(items, s, skip = '') {
  return items.filter((it) => {
    for (const key of ['coverage', 'impact', 'complexity']) {
      if (skip !== key && s[key].length && !s[key].includes(it[key].choice)) return false;
    }
    if (skip !== 'domain' && s.domain.length && !it.domains.some((k) => s.domain.includes(k))) return false;
    for (const key of ['solved', 'closed']) {
      if (s[key] !== 'any' && it[key] !== (s[key] === 'yes')) return false;
    }
    if (s.replies === 'zero' && it.replies !== 0 || s.replies === 'some' && !(it.replies > 0)
      || s.replies === 'unknown' && it.replies !== null) return false;
    return !s.clarification || it.clarification >= .5;
  });
}

export function sortQuestions(items, sort, relevance) {
  const impact = { critical: 3, significant: 2, minor: 1, unknown: 0 };
  const score = (it) => sort === 'relevance' ? relevance?.get(it.id) ?? -Infinity
    : sort === 'impact' ? impact[it.impact.choice] : it.active ?? 0;
  return [...items].sort((a, b) => score(b) - score(a) || (b.active ?? 0) - (a.active ?? 0) || a.id - b.id);
}

export function rankQuestions(items, query, mode, keyword, semantic) {
  if (!query.trim()) return { items, relevance: null };
  const byId = new Map(items.map((it) => [it.id, it]));
  const kw = keyword.filter((r) => byId.has(r.id));
  const sem = semantic?.filter((r) => byId.has(r.id));
  if (mode === 'sem' && sem) return { items: sem.map((r) => byId.get(r.id)), relevance: new Map(sem.map((r) => [r.id, r.score])) };
  if (mode === 'hyb' && sem) {
    const scores = rrf([kw.map((r) => r.id), sem.map((r) => r.id)]);
    return { items: [...scores.keys()].map((id) => byId.get(id)), relevance: scores };
  }
  return { items: kw.map((r) => byId.get(r.id)), relevance: new Map(kw.map((r) => [r.id, r.score])) };
}
