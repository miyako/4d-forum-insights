// Loading and decoding of the published data (see scripts/build_data.py for the format).

const BASE = new URL('../data/', import.meta.url);

async function getJSON(rel) {
  const res = await fetch(new URL(rel, BASE));
  if (!res.ok) throw new Error(`${rel}: HTTP ${res.status}`);
  return res.json();
}

export const dataUrl = (rel) => new URL(rel, BASE).href;

export async function loadManifest() {
  return getJSON('manifest.json?t=' + Math.floor(Date.now() / 60000));
}

/** topics.json → array of plain objects (tags resolved to names). */
export async function loadTopics(manifest) {
  const { fields, rows } = await getJSON(manifest.files.topics);
  return decodeTopics(fields, rows, manifest);
}

export function decodeTopics(fields, rows, manifest) {
  const ix = Object.fromEntries(fields.map((f, i) => [f, i]));
  const tags = manifest.tags;
  return rows.map((r, i) => ({
    i,
    id: r[ix.id],
    slug: r[ix.slug],
    title: r[ix.title],
    title_en: r[ix.title_en],
    cat: r[ix.cat],
    tags: (r[ix.tags] || []).map((t) => tags[t]),
    lang: r[ix.lang],
    created: r[ix.created],
    active: r[ix.active],
    posts: r[ix.posts],
    views: r[ix.views],
    likes: r[ix.likes],
    votes: r[ix.votes],
    flags: r[ix.flags],
    line: r[ix.line],
    para: undefined,
    ans: null,
  }));
}

/** text.json: summary paragraphs aligned with topics.json rows. */
export async function loadText(manifest, topics) {
  const { para } = await getJSON(manifest.files.text);
  if (para.length !== topics.length) throw new Error('text.json does not match topics.json');
  for (let i = 0; i < para.length; i++) topics[i].para = para[i];
}

const runCache = new Map();

/** answers/<run>.json → Map(topicId → {qid: decoded answer}). */
export function loadRun(manifest, runId) {
  if (!runCache.has(runId)) {
    const run = manifest.runs.find((r) => r.id === runId);
    if (!run) return Promise.resolve(new Map());
    runCache.set(runId, getJSON(run.file).then((data) => decodeRun(data, manifest)).catch((e) => {
      runCache.delete(runId);
      throw e;
    }));
  }
  return runCache.get(runId);
}

export function decodeRun(data, manifest) {
  const qs = Object.fromEntries(manifest.questions.map((q) => [q.id, q]));
  const runMeta = manifest.runs?.find((r) => r.id === data.run);
  const out = new Map();
  data.ids.forEach((id) => out.set(id, {}));
  for (const [qid, col] of Object.entries(data.answers)) {
    const q = qs[qid];
    if (!q) continue;
    // the run file lists its own level/choice order; map it onto the manifest's order
    const own = data.keys?.[qid];
    const global = (q.levels || q.choices || []).map((x) => x.key);
    const map = own ? own.map((k) => global.indexOf(k)) : null;
    const cuts = runMeta?.bands?.[qid]?.cuts;
    for (let k = 0; k < data.ids.length; k++) {
      const v = col[k];
      if (v == null) continue;
      out.get(data.ids[k])[qid] = decodeAnswer(q, v, map, cuts);
    }
  }
  return out;
}

function remap(values, map, size) {
  if (!map) return values;
  const out = new Array(size).fill(0);
  values.forEach((x, j) => { if (map[j] >= 0) out[map[j]] = x; });
  return out;
}

/** Band of a score: the first band whose lower cut the score reaches (cuts are high → low). */
export function bandOf(q, s, cuts) {
  if (!q.bands?.length || !cuts?.length) return undefined;
  const i = cuts.findIndex((c) => s >= c - 1e-9);
  return q.bands[i < 0 ? Math.min(cuts.length, q.bands.length - 1) : i].key;
}

export function decodeAnswer(q, v, map = null, cuts = null) {
  if (q.type === 'score') {
    const s = v[0] / 100;
    let best = q.levels[0], bd = Infinity;
    for (const l of q.levels) {
      const d = Math.abs(s - l.value);
      if (d < bd - 1e-9 || (Math.abs(d - bd) < 1e-9 && l.value > best.value)) { best = l; bd = d; }
    }
    return { s, lv: best.key, b: bandOf(q, s, cuts), p: remap(v.slice(1).map((x) => x / 1000), map, q.levels.length) };
  }
  if (q.type === 'choice') {
    const ci = v[0] >= 0 ? (map ? map[v[0]] : v[0]) : -1;
    return { c: ci >= 0 ? q.choices[ci].key : null, p: remap(v.slice(1).map((x) => x / 1000), map, q.choices.length) };
  }
  return { p: v / 1000 };
}
