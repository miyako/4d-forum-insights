// Keyword search: MiniSearch over titles, English titles, one-line summaries and tags; the summary
// paragraphs are added in a second, background build once text.json has loaded.
import MiniSearch from '../vendor/minisearch.js';

const CJK = /[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]/;
const SPLIT = /[\s\p{P}\p{S}]+/u;

const ASCII = /^[\x00-\x7f]*$/;
const NORM_CACHE_MAX = 50000;
const NORM_TERM_MAX = 64;
const normCache = new Map();

function normalizeUncached(term) {
  // Fold Latin accents, not Japanese dakuten: バ and ハ are different search terms.
  // Unicode normalization is the identity on ASCII, so only lower-casing remains.
  if (ASCII.test(term)) return term.toLowerCase();
  return term.normalize('NFKC').normalize('NFD').replace(/(\p{Script=Latin})\p{M}+/gu, '$1').normalize('NFC').toLowerCase();
}

/** Memoized for short strings: indexing normalizes every token, and tokens repeat heavily. */
export function normalize(term) {
  if (term.length > NORM_TERM_MAX) return normalizeUncached(term);
  let out = normCache.get(term);
  if (out === undefined) {
    if (normCache.size >= NORM_CACHE_MAX) normCache.clear();
    out = normalizeUncached(term);
    normCache.set(term, out);
  }
  return out;
}

/** Resolves after the next paint (or a short timeout where frames are throttled or unavailable). */
export function afterPaint(win = globalThis) {
  return new Promise((resolve) => {
    let done = false;
    const go = () => { if (!done) { done = true; resolve(); } };
    if (typeof win.requestAnimationFrame === 'function') win.requestAnimationFrame(() => setTimeout(go, 0));
    setTimeout(go, 50);
  });
}

/** Words for Latin scripts; overlapping character bigrams for CJK runs (no spaces between words). */
export function tokenize(text) {
  const out = [];
  const str = String(text || '');
  for (const tok of (ASCII.test(str) ? str : str.normalize('NFKC')).split(SPLIT)) {
    if (!tok) continue;
    if (!CJK.test(tok)) { out.push(tok); continue; }
    for (const part of tok.split(/([\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+)/)) {
      if (!part) continue;
      if (!CJK.test(part)) out.push(part);
      else if (part.length === 1) out.push(part);
      else for (let i = 0; i < part.length - 1; i++) out.push(part.slice(i, i + 2));
    }
  }
  return out;
}

const STOP = new Set('a an and are as at be by for from how i in is it of on or the to with 4d'.split(' '));

function processTerm(term, field) {
  const t = normalize(term);
  if (field && STOP.has(t)) return null;  // keep stopwords in queries out of the index only
  return t;
}

function options(fields) {
  return {
    idField: 'id',
    fields,
    tokenize,
    processTerm,
    extractField: (doc, f) => (f === 'tags' ? doc.tags.join(' ') : doc[f] || ''),
    searchOptions: {
      boost: { title_en: 2.2, title: 2, tags: 1.6, line: 1, para: 0.6, fallback: 0.6 },
      prefix: (term) => term.length > 2,
      fuzzy: (term) => (term.length > 5 ? 0.15 : false),
      processTerm: (t) => { const n = normalize(t); return STOP.has(n) ? null : n; },
    },
  };
}

const BASE_FIELDS = ['title', 'title_en', 'line', 'tags', 'fallback'];
const FULL_FIELDS = ['title', 'title_en', 'line', 'tags', 'para', 'fallback'];
const CHUNK = 500;

export class KeywordIndex {
  /** Builds synchronously unless `build: false` (see KeywordIndex.create for the non-blocking build). */
  constructor(topics, { build = true } = {}) {
    this.sourceTopics = new Map(topics.map(topic => [topic.id, topic]));
    this.topics = topics.map(topic => ({ ...topic, fallback: topic.canonicalFields
      ? Object.values(topic.canonicalFields).flat().join(' ') : '' }));
    this.byId = new Map(this.topics.map((t) => [t.id, t]));
    this.index = new MiniSearch(options(BASE_FIELDS));
    if (build) this.index.addAll(this.topics);
    this.full = false;
  }

  /**
   * Builds in chunks that yield to the event loop, so the page stays responsive. With `paragraphs`
   * the summary paragraphs are indexed in the same pass (equivalent to the constructor + addParagraphs()).
   */
  static async create(topics, { paragraphs = false } = {}) {
    const ki = new KeywordIndex(topics, { build: false });
    if (paragraphs) await ki.addParagraphs();
    else await ki.index.addAllAsync(ki.topics, { chunkSize: CHUNK });
    return ki;
  }

  /** Rebuild with the summary paragraphs in the background, then swap. */
  async addParagraphs() {
    const idx = new MiniSearch(options(FULL_FIELDS));
    // Paragraphs may have been attached after the first index build.
    for (const topic of this.topics) topic.para = this.sourceTopics.get(topic.id).para;
    await idx.addAllAsync(this.topics, { chunkSize: CHUNK });
    this.index = idx;
    this.full = true;
  }

  /**
   * Returns ranked [{id, score, terms}]. Words are ANDed (falls back to OR when nothing matches);
   * "-word" excludes, "quoted phrases" must appear verbatim in the title or summary.
   */
  search(query) {
    const phrases = [...query.matchAll(/"([^"]+)"/g)].map((m) => normalize(m[1]));
    const rest = query.replace(/"([^"]+)"/g, ' $1 ');
    const words = rest.split(/\s+/).filter(Boolean);
    const neg = words.filter((w) => w.startsWith('-') && w.length > 1).map((w) => w.slice(1));
    const pos = words.filter((w) => !w.startsWith('-')).join(' ');
    if (!pos.trim()) return [];
    const run = (combineWith) => {
      const q = neg.length
        ? { combineWith: 'AND_NOT', queries: [{ queries: [pos], combineWith }, ...neg.map((n) => ({ queries: [n], prefix: false, fuzzy: false }))] }
        : pos;
      return this.index.search(q, { combineWith });
    };
    let res = run('AND');
    if (!res.length) res = run('OR');
    if (phrases.length) {
      res = res.filter((r) => {
        const t = this.byId.get(r.id);
        const hay = normalize([t.title, t.title_en, t.line, t.para, t.fallback].filter(Boolean).join(' '));
        return phrases.every((ph) => hay.includes(ph));
      });
    }
    return res.map((r) => ({ id: r.id, score: r.score, terms: r.terms }));
  }
}
