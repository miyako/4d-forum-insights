import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';
import {
  documentText, embeddingIdentity, truncateNormalize, loadTextEncoder, loadBrowserEncoder,
  TRANSFORMERS_VERSION,
} from '../site/js/embed-worker.js';
import { Semantic } from '../site/js/semantic.js';

const config = JSON.parse(fs.readFileSync(new URL('../config/site.json', import.meta.url))).embeddings;
const meta = { ...config, transformers_version: TRANSFORMERS_VERSION, cache_identity: embeddingIdentity(config) };

test('embedding contract pins multilingual text-only Gemma, 256d MRL, and task prefixes', () => {
  assert.equal(config.model, 'onnx-community/embeddinggemma-2-ONNX');
  assert.match(config.revision, /^[a-f0-9]{40}$/);
  assert.equal(config.dim, 256);
  assert.equal(config.pooling, 'sentence_embedding');
  assert.equal(config.doc_dtype, 'q4');
  assert.equal(config.query_dtype, 'q4');
  assert.equal(config.query_wasm_dtype, 'fp16');
  assert.equal(config.quant, 'int8');
  assert.equal(config.query_prefix, 'task: search result | query: ');
  assert.equal(documentText({ title: '日本語', content: 'Sauvegarde' }), 'title: 日本語 | text: Sauvegarde');
  assert.equal(documentText({ title: '', content: 'text' }), 'title: none | text: text');
  assert.equal(documentText({ text: 'title: A | text: B' }), 'title: A | text: B');
  assert.equal(documentText({ text: 'Title\nSummary\nTags: a', h: 'abcdef012345', e: 1 }),
    'title: Title | text: Summary\nTags: a');
  assert.equal(documentText({ text: 'Title\nOpening post', h: 'abcdef012345', e: 0 }),
    'title: Title | text: Opening post');
  assert.throws(() => documentText({ text: 'legacy BGE input' }), /rebuild data/);
});

test('MRL truncates before normalization and rejects malformed model output', () => {
  const [a, b] = truncateNormalize({
    dims: [2, 4], data: Float32Array.of(3, 4, 100, 100, 0, 2, 100, 100),
  }, 2);
  assert.ok(Math.abs(a[0] - 0.6) < 1e-6);
  assert.ok(Math.abs(a[1] - 0.8) < 1e-6);
  assert.deepEqual([...b], [0, 1]);
  assert.throws(() => truncateNormalize({ dims: [1, 1], data: Float32Array.of(1) }, 2));
  assert.throws(() => truncateNormalize({ dims: [1, 2], data: Float32Array.of(NaN, 1) }, 2), /Non-finite/);
  assert.throws(() => truncateNormalize({ dims: [1, 2], data: Float32Array.of(0, 0) }, 2), /Zero/);
});

test('cache identity covers model, revision, dimension, pooling, prefixes, precision, and token limit', () => {
  for (const key of ['model', 'revision', 'dim', 'pooling', 'document_template', 'query_prefix',
    'doc_dtype', 'query_dtype', 'query_wasm_dtype', 'quant', 'max_length']) {
    assert.notEqual(embeddingIdentity({ ...config, [key]: 'changed' }), embeddingIdentity(config), key);
  }
  assert.equal(new Semantic(meta).available, true);
  assert.equal(new Semantic({ model: 'Xenova/bge-small-en-v1.5', dim: 384 }).available, false);
  assert.equal(new Semantic({ ...meta, dim: 384 }).available, false);
  const sem = new Semantic(meta);
  sem.vectors = { dim: 256 };
  assert.throws(() => sem.scoreAll(new Float32Array(384)), /dimension/);
});

test('legacy release metadata is rejected before vector downloads or worker initialization', async () => {
  const legacy = new Semantic({
    model: 'Xenova/bge-small-en-v1.5', dim: 384, pooling: 'cls', quant: 'int8',
    bin: 'embeddings.bin', ids: 'embeddings-ids.json',
  });
  assert.equal(legacy.available, false);
  await assert.rejects(legacy.loadVectors(), /Incompatible embedding index/);
  await assert.rejects(legacy.loadModel(), /Incompatible embedding index/);
  await assert.rejects(legacy.embed('database backup'), /Incompatible embedding index/);
  assert.equal(legacy.worker, null);
  assert.equal(legacy.vectors, null);
});

function fakeTransformers({ failGPU = false } = {}) {
  const calls = [];
  const tf = {
    env: { remotePathTemplate: '{model}/resolve/{revision}/' },
    AutoConfig: { async from_pretrained(...args) {
      assert.equal(tf.env.remotePathTemplate, `{model}/resolve/${config.revision}/`);
      calls.push(['config', ...args]);
      return { vision_config: {}, audio_config: {} };
    } },
    AutoTokenizer: { async from_pretrained(...args) {
      calls.push(['tokenizer', ...args]);
      return async (texts, options) => { calls.push(['tokenize', texts, options]); return { input_ids: 'tokens' }; };
    } },
    AutoModel: { async from_pretrained(model, options) {
      calls.push(['model', model, options]);
      assert.equal(options.config.vision_config, null);
      assert.equal(options.config.audio_config, null);
      if (failGPU && options.device === 'webgpu') throw new Error('Unsupported GPU kernel');
      const modelFn = async (inputs) => {
        assert.deepEqual(inputs, { input_ids: 'tokens' });
        return { sentence_embedding: { dims: [1, 768], data: new Float32Array(768).fill(1) } };
      };
      modelFn.dispose = async () => calls.push(['dispose']);
      return modelFn;
    } },
  };
  return { tf, calls };
}

test('AutoConfig disables media before AutoModel and uses sentence_embedding, not hidden states', async () => {
  const { tf, calls } = fakeTransformers();
  const encoder = await loadTextEncoder(tf, meta);
  assert.equal(tf.env.remotePathTemplate, '{model}/resolve/{revision}/');
  const [row] = await encoder.embed(['title: T | text: C']);
  assert.equal(row.length, 256);
  assert.ok(Math.abs(Math.hypot(...row) - 1) < 1e-6);
  for (const type of ['config', 'tokenizer', 'model']) {
    assert.equal(calls.find(c => c[0] === type)[2].revision, config.revision);
  }
  assert.equal(calls.find(c => c[0] === 'tokenize')[2].max_length, config.max_length);
  await encoder.dispose();
});

test('browser prefers q4 WebGPU and retries fp16 WASM when GPU session fails', async () => {
  for (const failGPU of [false, true]) {
    const { tf, calls } = fakeTransformers({ failGPU });
    const progress = [];
    const loaded = await loadBrowserEncoder(tf, meta, p => progress.push(p), { requestAdapter: async () => ({}) });
    assert.equal(loaded.device, failGPU ? 'wasm' : 'webgpu');
    assert.deepEqual(calls.filter(c => c[0] === 'model').map(c => [c[2].device, c[2].dtype]),
      failGPU ? [['webgpu', 'q4'], ['wasm', 'fp16']] : [['webgpu', 'q4']]);
    assert.equal(progress.length, failGPU ? 1 : 0);
    assert.equal(tf.env.remotePathTemplate, '{model}/resolve/{revision}/');
  }
  const { tf, calls } = fakeTransformers();
  assert.equal((await loadBrowserEncoder(tf, meta, () => {}, null)).device, 'wasm');
  assert.deepEqual(calls.filter(c => c[0] === 'model').map(c => c[2].device), ['wasm']);
  assert.equal(calls.find(c => c[0] === 'model')[2].dtype, 'fp16');
});

test('builder reuses only exact contract and text hashes, never legacy dimensions or stale text', () => {
  const dir = path.join('build', `embedding-test-${process.pid}`);
  fs.mkdirSync(dir, { recursive: true });
  try {
    const text = 'title: 日本語 | text: multilingual';
    const hash = crypto.createHash('sha256').update(meta.cache_identity).update('\0').update(text).digest('hex');
    for (const scenario of ['exact', 'legacy', 'revision', 'text']) {
      const previous = scenario === 'legacy' ? { model: 'Xenova/bge-small-en-v1.5', dim: 384 } :
        scenario === 'revision' ? { ...meta, revision: '0'.repeat(40) } : meta;
      fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify({ embeddings: previous }));
      fs.writeFileSync(path.join(dir, 'embeddings.bin'), Buffer.alloc(previous.dim, 42));
      fs.writeFileSync(path.join(dir, 'embeddings-ids.json'), '[1]');
      fs.writeFileSync(path.join(dir, 'embeddings-hashes.json'), JSON.stringify([scenario === 'text' ? 'stale' : hash]));
      fs.writeFileSync(path.join(dir, 'input.jsonl'), JSON.stringify({ id: 1, text, e: 0 }));
      // The un-enriched fixture is deliberately outside --sample: an invalid cache must not survive.
      const result = spawnSync(process.execPath, [
        'scripts/build_embeddings.mjs', '--input', path.join(dir, 'input.jsonl'), '--out', dir, '--sample', '1',
      ], { encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr);
      const actual = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'))).embeddings;
      assert.deepEqual(JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'))).embedding_config, config);
      if (scenario === 'exact') {
        assert.equal(actual.dim, 256);
        assert.equal(actual.count, 1);
        assert.equal(fs.statSync(path.join(dir, 'embeddings.bin')).size, 256);
      } else assert.equal(actual, null, scenario);
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('real pinned q4 text-only CPU inference', { skip: process.env.EMBED_INFERENCE_TEST !== '1' }, async () => {
  const tf = await import('@huggingface/transformers');
  tf.env.cacheDir = process.env.HF_CACHE_DIR || path.resolve('build/hf-cache');
  const encoder = await loadTextEncoder(tf, meta, {
    session_options: { intraOpNumThreads: 2, interOpNumThreads: 1 },
  });

  try {
    const rows = await encoder.embed([
      meta.query_prefix + 'データベースのバックアップ',
      documentText({ title: 'Sauvegarde', content: 'How to back up a 4D database safely.' }),
    ]);
    assert.equal(rows.length, 2);
    for (const row of rows) {
      assert.equal(row.length, 256);
      assert.ok(row.every(Number.isFinite));
      assert.ok(Math.abs(Math.hypot(...row) - 1) < 1e-5);
    }
  } finally {
    await encoder.dispose();
  }
});

test('real builder writes int8 across multiple batches and reuses the result', {
  skip: process.env.EMBED_INFERENCE_TEST !== '1',
}, () => {
  const dir = path.join('build', `embedding-inference-test-${process.pid}`);
  fs.mkdirSync(dir, { recursive: true });
  try {
    fs.writeFileSync(path.join(dir, 'manifest.json'), '{}');
    fs.writeFileSync(path.join(dir, 'input.jsonl'), [
      { id: 1, title: 'Backup', content: 'How to back up a database.' },
      { id: 2, title: 'Sauvegarde', content: 'Sauvegarder une base de données.' },
      { id: 3, title: 'バックアップ', content: 'データベースをバックアップする方法。' },
    ].map(JSON.stringify).join('\n'));
    for (let run = 0; run < 2; run++) {
      const result = spawnSync(process.execPath, [
        'scripts/build_embeddings.mjs', '--input', path.join(dir, 'input.jsonl'), '--out', dir, '--batch', '1',
      ], { encoding: 'utf8' });
      assert.equal(result.status, 0, result.stderr);
      if (run) assert.match(result.stdout, /3 topics, 0 to embed/);
      const actual = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json'))).embeddings;
      assert.equal(actual.count, 3);
      assert.equal(actual.stale, 0);
      assert.equal(actual.cache_identity, meta.cache_identity);
      const bin = fs.readFileSync(path.join(dir, 'embeddings.bin'));
      assert.equal(bin.length, 3 * 256);
      assert.ok(bin.some(x => x !== 0));
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
