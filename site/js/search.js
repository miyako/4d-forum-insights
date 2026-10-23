// Keyword search: MiniSearch over titles, English titles, one-line summaries and tags; the summary
// paragraphs are added in a second, background build once text.json has loaded.
import MiniSearch from '../vendor/minisearch.js';

const CJK = /[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]/;
const SPLIT = /[\s\p{P}\p{S}]+/u;

export function normalize(term) {
  return term.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();
}

/** Words for Latin scripts; overlapping character bigrams for CJK runs (no spaces between words). */
export function tokenize(text) {
  const out = [];
  for (const tok of String(text || '').split(SPLIT)) {
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
      boost: { title_en: 2.2, title: 2, tags: 1.6, line: 1, para: 0.6 },
      prefix: (term) => term.length > 2,
      fuzzy: (term) => (term.length > 5 ? 0.15 : false),
      processTerm: (t) => { const n = normalize(t); return STOP.has(n) ? null : n; },
    },
  };
}

export class KeywordIndex {
  constructor(topics) {
    this.topics = topics;
    this.byId = new Map(topics.map((t) => [t.id, t]));
    this.index = new MiniSearch(options(['title', 'title_en', 'line', 'tags']));
    this.index.addAll(topics);
    this.full = false;
  }

  /** Rebuild with the summary paragraphs in the background, then swap. */
  async addParagraphs() {
    const idx = new MiniSearch(options(['title', 'title_en', 'line', 'tags', 'para']));
    await idx.addAllAsync(this.topics, { chunkSize: 500 });
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
        const hay = normalize([t.title, t.title_en, t.line, t.para].filter(Boolean).join(' '));
        return phrases.every((ph) => hay.includes(ph));
      });
    }
    return res.map((r) => ({ id: r.id, score: r.score, terms: r.terms }));
  }
}
