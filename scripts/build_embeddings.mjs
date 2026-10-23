#!/usr/bin/env node
// Build (or incrementally update) the quantised topic embeddings used by the site's semantic search.
//
//   node scripts/build_embeddings.mjs                 # embed every topic that is new or changed
//   node scripts/build_embeddings.mjs --sample 400    # small deterministic sample (development)
//
// Reads build/embed-input.jsonl (written by scripts/build_data.py), reuses vectors already in
// site/data/embeddings.bin whose text hash is unchanged, embeds the rest with transformers.js (the same
// ONNX model the browser uses for queries), and writes:
//   site/data/embeddings.bin           N x dim int8 (per-vector scaled) or N x dim/8 sign bits (--quant binary)
//   site/data/embeddings-ids.json      topic id of each row
//   site/data/embeddings-hashes.json   text hash of each row (cache key for the next run; not loaded by the UI)
// and records the model/quantisation in site/data/manifest.json ("embeddings").
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const site = JSON.parse(fs.readFileSync(path.join(ROOT, 'config/site.json'), 'utf8'));
const defaults = site.embeddings || {};

const { values: opt } = parseArgs({
  options: {
    input: { type: 'string', default: path.join(ROOT, 'build/embed-input.jsonl') },
    out: { type: 'string', default: path.join(ROOT, 'site/data') },
    model: { type: 'string', default: defaults.model || 'Xenova/bge-small-en-v1.5' },
    pooling: { type: 'string', default: defaults.pooling || 'cls' },
    dtype: { type: 'string', default: defaults.doc_dtype || 'fp32' },
    quant: { type: 'string', default: defaults.quant || 'int8' },
    sample: { type: 'string' },
    limit: { type: 'string' },
    batch: { type: 'string', default: '16' },
    threads: { type: 'string', default: process.env.EMBED_THREADS || '2' },
    'cache-dir': { type: 'string', default: process.env.HF_CACHE_DIR || path.join(ROOT, 'build/hf-cache') },
  },
});
if (!['int8', 'binary'].includes(opt.quant)) throw new Error('--quant must be int8 or binary');

const log = (...a) => console.log('[embed]', ...a);
const readJSON = (f, d) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return d; } };

// ---------------------------------------------------------------- inputs
const docs = fs.readFileSync(opt.input, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
const manifestPath = path.join(opt.out, 'manifest.json');
const manifest = readJSON(manifestPath, null);
if (!manifest) throw new Error(`${manifestPath} missing: run scripts/build_data.py first`);

// ---------------------------------------------------------------- previous vectors (cache)
const prev = manifest.embeddings;
const cache = new Map(); // id -> {h, row: Int8Array|Uint8Array}
let dim = prev?.dim;
if (prev && prev.model === opt.model && prev.pooling === opt.pooling && prev.quant === opt.quant
    && fs.existsSync(path.join(opt.out, 'embeddings.bin'))) {
  const bin = fs.readFileSync(path.join(opt.out, 'embeddings.bin'));
  const ids = readJSON(path.join(opt.out, 'embeddings-ids.json'), []);
  const hashes = readJSON(path.join(opt.out, 'embeddings-hashes.json'), []);
  const rb = opt.quant === 'binary' ? dim / 8 : dim;
  if (bin.length === ids.length * rb && hashes.length === ids.length) {
    ids.forEach((id, i) => cache.set(id, {
      h: hashes[i],
      row: opt.quant === 'binary' ? new Uint8Array(bin.subarray(i * rb, (i + 1) * rb)) : new Int8Array(bin.buffer, bin.byteOffset + i * rb, rb).slice(),
    }));
  }
  log(`reusing up to ${cache.size} cached vectors`);
} else if (prev) {
  log('model/quantisation changed: recomputing everything');
}

// ---------------------------------------------------------------- what to embed
let todo = docs.filter((d) => cache.get(d.id)?.h !== d.h);
if (opt.sample) {
  // deterministic sample, enriched + classified topics first (they carry English summaries)
  const n = Number(opt.sample);
  const rank = (d) => crypto.createHash('sha1').update(String(d.id)).digest().readUInt32BE(0);
  const pool = docs.filter((d) => d.e).sort((a, b) => (b.c || 0) - (a.c || 0) || rank(a) - rank(b));
  const chosen = new Set(pool.slice(0, n).map((d) => d.id));
  todo = todo.filter((d) => chosen.has(d.id));
}
if (opt.limit) todo = todo.slice(0, Number(opt.limit));
log(`${docs.length} topics, ${todo.length} to embed with ${opt.model} (${opt.dtype}, ${opt.pooling} pooling)`);

// ---------------------------------------------------------------- embed
function quantize(v) {
  if (opt.quant === 'binary') {
    const row = new Uint8Array(v.length / 8);
    for (let k = 0; k < v.length; k++) if (v[k] > 0) row[k >> 3] |= 1 << (7 - (k & 7));
    return row;
  }
  let m = 0;
  for (const x of v) m = Math.max(m, Math.abs(x));
  const row = new Int8Array(v.length);
  for (let k = 0; k < v.length; k++) row[k] = m ? Math.round((v[k] / m) * 127) : 0;
  return row;
}

if (todo.length) {
  const { pipeline, env } = await import('@huggingface/transformers');
  env.cacheDir = opt['cache-dir'];
  const extractor = await pipeline('feature-extraction', opt.model, {
    dtype: opt.dtype,
    session_options: { intraOpNumThreads: Number(opt.threads), interOpNumThreads: 1 },
  });
  const B = Number(opt.batch);
  const t0 = Date.now();
  for (let i = 0; i < todo.length; i += B) {
    const batch = todo.slice(i, i + B);
    const out = await extractor(batch.map((d) => d.text), { pooling: opt.pooling, normalize: true });
    const [n, d] = out.dims;
    if (dim && dim !== d) throw new Error(`dimension changed ${dim} -> ${d}`);
    dim = d;
    for (let j = 0; j < n; j++) cache.set(batch[j].id, { h: batch[j].h, row: quantize(out.data.subarray(j * d, (j + 1) * d)) });
    const done = Math.min(i + B, todo.length);
    if (done % (B * 10) === 0 || done === todo.length) {
      const rate = done / ((Date.now() - t0) / 1000);
      log(`${done}/${todo.length}  ${rate.toFixed(1)}/s  eta ${((todo.length - done) / rate / 60).toFixed(1)} min`);
    }
  }
}

// ---------------------------------------------------------------- write (only topics that still exist)
const live = docs.filter((d) => cache.has(d.id));
if (!live.length) {
  log('nothing embedded yet');
  process.exit(0);
}
const rb = opt.quant === 'binary' ? dim / 8 : dim;
const bin = Buffer.alloc(live.length * rb);
live.forEach((d, i) => Buffer.from(cache.get(d.id).row.buffer, cache.get(d.id).row.byteOffset, rb).copy(bin, i * rb));
const ids = live.map((d) => d.id);
const hashes = live.map((d) => cache.get(d.id).h);
const stale = live.filter((d) => cache.get(d.id).h !== d.h).length;
const ver = (buf) => crypto.createHash('sha1').update(buf).digest('hex').slice(0, 10);
fs.writeFileSync(path.join(opt.out, 'embeddings.bin'), bin);
const idsJson = JSON.stringify(ids);
fs.writeFileSync(path.join(opt.out, 'embeddings-ids.json'), idsJson);
fs.writeFileSync(path.join(opt.out, 'embeddings-hashes.json'), JSON.stringify(hashes));

manifest.embeddings = {
  model: opt.model,
  pooling: opt.pooling,
  query_prefix: defaults.query_prefix ?? '',
  query_dtype: defaults.query_dtype || 'q8',
  doc_dtype: opt.dtype,
  quant: opt.quant,
  dim,
  count: ids.length,
  stale,
  bytes: bin.length,
  built_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
  bin: `embeddings.bin?v=${ver(bin)}`,
  ids: `embeddings-ids.json?v=${ver(idsJson)}`,
};
fs.writeFileSync(manifestPath, JSON.stringify(manifest));
log(`wrote ${ids.length} vectors (${(bin.length / 1024).toFixed(0)} KB, ${opt.quant}, dim ${dim})` + (stale ? `, ${stale} stale` : ''));
