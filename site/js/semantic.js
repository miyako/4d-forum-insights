// Semantic search: precomputed, quantised topic embeddings + a query embedded in the browser by
// transformers.js (in a Web Worker). Everything is loaded lazily, only when semantic search is used.
import { dataUrl } from './data.js';
import { embeddingIdentity, TRANSFORMERS_VERSION } from './embed-worker.js';

export class Semantic {
  constructor(meta) {
    this.meta = meta; // manifest.embeddings
    this.vectors = null;
    this.worker = null;
    this.modelReady = null;
    this.vectorsReady = null;
    this.onProgress = () => {};
    this.pending = new Map();
    this.seq = 0;
  }

  get available() {
    const m = this.meta;
    return !!m && m.dim === 256 && m.quant === 'int8' && m.pooling === 'sentence_embedding'
      && m.transformers_version === TRANSFORMERS_VERSION && m.cache_identity === embeddingIdentity(m);
  }

  /** Load the topic vectors (no model needed: enough for "similar topics"). */
  loadVectors() {
    if (!this.available) return Promise.reject(new Error('Incompatible embedding index; rebuild embeddings'));
    if (!this.vectorsReady) {
      this.vectorsReady = (async () => {
        const [buf, ids] = await Promise.all([
          fetch(dataUrl(this.meta.bin)).then((r) => { if (!r.ok) throw new Error('embeddings: HTTP ' + r.status); return r.arrayBuffer(); }),
          fetch(dataUrl(this.meta.ids)).then((r) => { if (!r.ok) throw new Error('embedding IDs: HTTP ' + r.status); return r.json(); }),
        ]);
        const { dim, quant } = this.meta;
        const n = ids.length;
        const rowBytes = quant === 'binary' ? dim / 8 : dim;
        if (buf.byteLength !== n * rowBytes) throw new Error('embeddings.bin size mismatch');
        const data = quant === 'binary' ? new Uint8Array(buf) : new Int8Array(buf);
        const inv = new Float32Array(n);
        if (quant !== 'binary') {
          for (let r = 0; r < n; r++) {
            let s = 0;
            for (let k = r * dim, e = k + dim; k < e; k++) s += data[k] * data[k];
            inv[r] = s ? 1 / Math.sqrt(s) : 0;
          }
        } else inv.fill(1 / Math.sqrt(dim));
        const row = new Map(ids.map((id, i) => [id, i]));
        this.vectors = { data, inv, ids, row, dim, quant, rowBytes };
        return this.vectors;
      })().catch((e) => { this.vectorsReady = null; throw e; });
    }
    return this.vectorsReady;
  }

  /** Start the embedding model in a worker (downloads it on first use; cached by the browser). */
  loadModel() {
    if (!this.available) return Promise.reject(new Error('Incompatible embedding index; rebuild embeddings'));
    if (!this.modelReady) {
      this.modelReady = new Promise((resolve, reject) => {
        this.worker = new Worker(new URL('./embed-worker.js', import.meta.url), { type: 'module' });
        this.worker.onmessage = (e) => {
          const m = e.data;
          if (m.type === 'progress') this.onProgress(m);
          else if (m.type === 'ready') { this.device = m.device; this.dtype = m.dtype; resolve(); }
          else if (m.type === 'error' && m.id == null) reject(new Error(m.error));
          else if (m.id != null && this.pending.has(m.id)) {
            const { res, rej } = this.pending.get(m.id);
            this.pending.delete(m.id);
            m.type === 'error' ? rej(new Error(m.error)) : res(m.vec);
          }
        };
        this.worker.onerror = (e) => reject(new Error(e.message || 'embedding worker failed'));
        this.worker.postMessage({ type: 'init', meta: this.meta });
      }).catch((e) => { this.modelReady = null; this.worker?.terminate(); throw e; });
    }
    return this.modelReady;
  }

  async ready() {
    await Promise.all([this.loadVectors(), this.loadModel()]);
  }

  async embed(text) {
    await this.loadModel();
    const id = ++this.seq;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.worker.postMessage({ type: 'embed', id, text });
    });
  }

  /** Cosine similarity of a (normalised) float query vector with every topic vector. */
  scoreAll(q) {
    const { data, inv, ids, dim, quant, rowBytes } = this.vectors;
    if (q.length !== dim) throw new Error(`Query dimension ${q.length} does not match index dimension ${dim}`);
    const out = new Float32Array(ids.length);
    if (quant === 'binary') {
      for (let r = 0; r < ids.length; r++) {
        let s = 0;
        const off = r * rowBytes;
        for (let k = 0; k < dim; k++) s += (data[off + (k >> 3)] >> (7 - (k & 7))) & 1 ? q[k] : -q[k];
        out[r] = s * inv[r];
      }
    } else {
      for (let r = 0; r < ids.length; r++) {
        let s = 0;
        for (let k = 0, off = r * dim; k < dim; k++) s += data[off + k] * q[k];
        out[r] = s * inv[r];
      }
    }
    return out;
  }

  /** Ranked [{id, score}] for a query string, best first. */
  async search(text, limit = 300) {
    const [q] = await Promise.all([this.embed(text), this.loadVectors()]);
    return topK(this.scoreAll(q), this.vectors.ids, limit);
  }

  /** Nearest neighbours of a topic (uses its stored vector as the query). */
  async similar(topicId, limit = 8) {
    const v = await this.loadVectors();
    const r = v.row.get(topicId);
    if (r == null) return [];
    const q = new Float32Array(v.dim);
    if (v.quant === 'binary') for (let k = 0; k < v.dim; k++) q[k] = (v.data[r * v.rowBytes + (k >> 3)] >> (7 - (k & 7))) & 1 ? 1 : -1;
    else for (let k = 0; k < v.dim; k++) q[k] = v.data[r * v.dim + k];
    let n = 0;
    for (const x of q) n += x * x;
    n = Math.sqrt(n) || 1;
    for (let k = 0; k < v.dim; k++) q[k] /= n;
    return topK(this.scoreAll(q), v.ids, limit + 1).filter((x) => x.id !== topicId).slice(0, limit);
  }
}

export function topK(scores, ids, k) {
  const idx = Array.from(scores.keys());
  idx.sort((a, b) => scores[b] - scores[a]);
  return idx.slice(0, k).map((i) => ({ id: ids[i], score: scores[i] }));
}
