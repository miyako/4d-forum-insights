// Web Worker: embeds search queries with transformers.js (ONNX Runtime Web, WASM), off the main thread.
// The library is loaded from a CDN only when semantic search is first used; the model comes from the
// Hugging Face Hub and is cached by the browser (Cache Storage) after the first download.
const TRANSFORMERS = 'https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.3.1';

let extractor = null;

self.onmessage = async (e) => {
  const m = e.data;
  try {
    if (m.type === 'init') {
      const { pipeline } = await import(TRANSFORMERS);
      extractor = await pipeline('feature-extraction', m.model, {
        dtype: m.dtype,
        progress_callback: (p) => {
          if (p.status === 'progress' || p.status === 'done') {
            self.postMessage({ type: 'progress', file: p.file, loaded: p.loaded, total: p.total, status: p.status });
          }
        },
      });
      self.postMessage({ type: 'ready' });
    } else if (m.type === 'embed') {
      const out = await extractor(m.text, { pooling: m.pooling, normalize: true });
      const vec = Float32Array.from(out.data);
      self.postMessage({ type: 'vec', id: m.id, vec }, [vec.buffer]);
    }
  } catch (err) {
    self.postMessage({ type: 'error', id: m.id ?? null, error: String(err?.message || err) });
  }
};
