#!/usr/bin/env node
// Build (or incrementally update) the quantised topic embeddings used by the site's semantic search.
//
//   node scripts/build_embeddings.mjs                 # embed every topic that is new or changed
//   node scripts/build_embeddings.mjs --sample 400    # small deterministic sample (development)
//
// Reads build/embed-input.jsonl (written by scripts/build_data.py), reuses vectors already in
// site/data/embeddings.bin whose text hash is unchanged, embeds the rest with transformers.js (the same
// ONNX model the browser uses for queries), and writes:
//   site/data/embeddings.bin           N x 256 int8 (MRL truncation, L2 normalization, per-vector scaling)
//   site/data/embeddings-ids.json      topic id of each row
//   site/data/embeddings-hashes.json   text hash of each row (cache key for the next run; not loaded by the UI)
// and records the model/quantisation in site/data/manifest.json ("embeddings").
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { parseArgs } from 'node:util';
import { fileURLToPath } from 'node:url';
import { documentText, embeddingIdentity, loadTextEncoder, TRANSFORMERS_VERSION } from '../site/js/embed-worker.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const site = JSON.parse(fs.readFileSync(path.join(ROOT, 'config/site.json'), 'utf8'));
const defaults = site.embeddings || {};

const { values: opt } = parseArgs({
  options: {
    input: { type: 'string', default: path.join(ROOT, 'build/embed-input.jsonl') },
    out: { type: 'string', default: path.join(ROOT, 'site/data') },
    model: { type: 'string', default: defaults.model },
    revision: { type: 'string', default: defaults.revision },
    dim: { type: 'string', default: String(defaults.dim) },
    pooling: { type: 'string', default: defaults.pooling },
    dtype: { type: 'string', default: defaults.doc_dtype },
    device: { type: 'string', default: process.env.EMBED_DEVICE || 'cpu' },
    quant: { type: 'string', default: defaults.quant },
    'max-length': { type: 'string', default: String(defaults.max_length) },
    sample: { type: 'string' },
    limit: { type: 'string' },
    batch: { type: 'string', default: '4' },
    threads: { type: 'string', default: process.env.EMBED_THREADS || '2' },
    'cache-dir': { type: 'string', default: process.env.HF_CACHE_DIR || path.join(ROOT, 'build/hf-cache') },
  },
});
if (opt.quant !== 'int8') throw new Error('--quant must be int8');
if (opt.pooling !== 'sentence_embedding') throw new Error('--pooling must be sentence_embedding');
if (!['cpu', 'webgpu'].includes(opt.device)) throw new Error('--device must be cpu or webgpu');
for (const key of ['dim', 'batch', 'threads', 'max-length', 'sample', 'limit']) {
  if (opt[key] !== undefined && (!Number.isSafeInteger(Number(opt[key])) || Number(opt[key]) <= 0)) {
    throw new Error(`--${key} must be a positive integer`);
  }
}
if (Number(opt.dim) !== 256) throw new Error('--dim must be 256');
if (!/^[a-f0-9]{40}$/.test(opt.revision)) throw new Error('--revision must pin a full model commit SHA');
const config = {
  ...defaults, model: opt.model, revision: opt.revision, dim: Number(opt.dim),
  pooling: opt.pooling, doc_dtype: opt.dtype, quant: opt.quant, max_length: Number(opt['max-length']),
};
const identity = embeddingIdentity(config);

const log = (...a) => console.log('[embed]', ...a);
const readJSON = (f, d) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return d; } };

// ---------------------------------------------------------------- inputs
const docs = fs.readFileSync(opt.input, 'utf8').split('\n').filter(Boolean).map((l) => {
  const doc = JSON.parse(l);
  const text = documentText(doc);
  return { ...doc, text, h: crypto.createHash('sha256').update(identity).update('\0').update(text).digest('hex') };
});
const manifestPath = path.join(opt.out, 'manifest.json');
const manifest = readJSON(manifestPath, null);
if (!manifest) throw new Error(`${manifestPath} missing: run scripts/build_data.py first`);
manifest.embedding_config = config;

// ---------------------------------------------------------------- previous vectors (cache)
const prev = manifest.embeddings;
const cache = new Map(); // id -> {h, row: Int8Array}
const dim = config.dim;
if (prev && prev.transformers_version === TRANSFORMERS_VERSION
    && prev.cache_identity === identity && embeddingIdentity(prev) === identity
    && fs.existsSync(path.join(opt.out, 'embeddings.bin'))) {
  const bin = fs.readFileSync(path.join(opt.out, 'embeddings.bin'));
  const ids = readJSON(path.join(opt.out, 'embeddings-ids.json'), []);
  const hashes = readJSON(path.join(opt.out, 'embeddings-hashes.json'), []);
  const rb = dim;
  if (bin.length === ids.length * rb && hashes.length === ids.length) {
    ids.forEach((id, i) => cache.set(id, {
      h: hashes[i],
      row: new Int8Array(bin.buffer, bin.byteOffset + i * rb, rb).slice(),
    }));
  }
  log(`reusing up to ${cache.size} cached vectors`);
} else if (prev) {
  log('embedding contract changed: recomputing everything (no old-model/dimension reuse)');
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
log(`${docs.length} topics, ${todo.length} to embed with ${opt.model} (${opt.dtype}, ${opt.device}, ${opt.pooling} pooling)`);

// ---------------------------------------------------------------- embed
function quantize(v) {
  let m = 0;
  for (const x of v) m = Math.max(m, Math.abs(x));
  const row = new Int8Array(v.length);
  for (let k = 0; k < v.length; k++) row[k] = m ? Math.round((v[k] / m) * 127) : 0;
  return row;
}

if (todo.length) {
  const transformers = await import('@huggingface/transformers');
  transformers.env.cacheDir = opt['cache-dir'];
  const encoder = await loadTextEncoder(transformers, config, {
    device: opt.device,
    dtype: opt.dtype,
    session_options: { intraOpNumThreads: Number(opt.threads), interOpNumThreads: 1 },
  });
  const B = Number(opt.batch);
  const t0 = Date.now();
  for (let i = 0; i < todo.length; i += B) {
    const batch = todo.slice(i, i + B);
    const rows = await encoder.embed(batch.map((d) => d.text));
    if (rows.length !== batch.length) throw new Error('Model returned incorrect batch size');
    for (let j = 0; j < rows.length; j++) cache.set(batch[j].id, { h: batch[j].h, row: quantize(rows[j]) });
    const done = Math.min(i + B, todo.length);
    if (done % (B * 10) === 0 || done === todo.length) {
      const rate = done / ((Date.now() - t0) / 1000);
      log(`${done}/${todo.length}  ${rate.toFixed(1)}/s  eta ${((todo.length - done) / rate / 60).toFixed(1)} min`);
    }
  }
  await encoder.dispose();
}

// ---------------------------------------------------------------- write (only topics that still exist)
const live = docs.filter((d) => cache.get(d.id)?.h === d.h);
if (!live.length) {
  log('nothing embedded yet');
  manifest.embeddings = null;
  fs.writeFileSync(manifestPath, JSON.stringify(manifest));
  process.exit(0);
}
const rb = dim;
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
  ...config,
  doc_device: opt.device,
  transformers_version: TRANSFORMERS_VERSION,
  cache_identity: identity,
  count: ids.length,
  stale,
  bytes: bin.length,
  built_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
  bin: `embeddings.bin?v=${ver(bin)}`,
  ids: `embeddings-ids.json?v=${ver(idsJson)}`,
};
fs.writeFileSync(manifestPath, JSON.stringify(manifest));
log(`wrote ${ids.length} vectors (${(bin.length / 1024).toFixed(0)} KB, ${opt.quant}, dim ${dim})` + (stale ? `, ${stale} stale` : ''));
