// Shared text-only inference contract for the index builder and lazy browser worker.
export const TRANSFORMERS_VERSION = '4.3.1';
const TRANSFORMERS = `https://cdn.jsdelivr.net/npm/@huggingface/transformers@${TRANSFORMERS_VERSION}`;

export function embeddingIdentity(meta) {
  return JSON.stringify([
    TRANSFORMERS_VERSION, meta.model, meta.revision, meta.dim, meta.pooling,
    meta.document_template, meta.query_prefix, meta.doc_dtype, meta.query_dtype,
    meta.query_wasm_dtype, meta.quant, meta.max_length, 'text-only:mrl-truncate-then-l2:v1',
  ]);
}

export function documentText(doc) {
  if (typeof doc.title === 'string' && typeof doc.content === 'string') {
    return `title: ${doc.title || 'none'} | text: ${doc.content}`;
  }
  if (typeof doc.text === 'string' && /^title: .* \| text: /s.test(doc.text)) return doc.text;
  // build_data.py's canonical JSONL puts the document title on its first line.
  if (typeof doc.text === 'string' && /^[a-f0-9]{12}$/.test(doc.h) && [0, 1].includes(doc.e)) {
    const split = doc.text.indexOf('\n');
    const title = split < 0 ? doc.text : doc.text.slice(0, split);
    const content = split < 0 ? '' : doc.text.slice(split + 1);
    return `title: ${title || 'none'} | text: ${content}`;
  }
  throw new Error('Embedding input requires title/content or formatted title: {title} | text: {content}; rebuild data');
}

export function truncateNormalize(output, dim) {
  if (!output || output.dims.length !== 2 || output.dims[1] < dim) {
    throw new Error('Expected sentence_embedding [batch, native dimension]');
  }
  const [n, nativeDim] = output.dims;
  const rows = [];
  for (let r = 0; r < n; r++) {
    const row = Float32Array.from(output.data.subarray(r * nativeDim, r * nativeDim + dim));
    let norm = 0;
    for (const x of row) {
      if (!Number.isFinite(x)) throw new Error('Non-finite sentence_embedding');
      norm += x * x;
    }
    if (!norm) throw new Error('Zero sentence_embedding');
    norm = Math.sqrt(norm);
    for (let k = 0; k < dim; k++) row[k] /= norm;
    rows.push(row);
  }
  return rows;
}

export async function loadTextEncoder(transformers, meta, options = {}) {
  const { AutoConfig, AutoModel, AutoTokenizer } = transformers;
  const { device = 'cpu', dtype = meta.doc_dtype, ...rest } = options;
  const env = transformers.env;
  const previousTemplate = env?.remotePathTemplate;
  // 4.3.1's tokenizer file-discovery probe omits revision; pin its URL as well.
  if (typeof previousTemplate === 'string') {
    env.remotePathTemplate = previousTemplate.replaceAll('{revision}', encodeURIComponent(meta.revision));
  }
  let tokenizer, model;
  try {
    const config = await AutoConfig.from_pretrained(meta.model, { revision: meta.revision });
    config.vision_config = config.audio_config = null;
    tokenizer = await AutoTokenizer.from_pretrained(meta.model, { revision: meta.revision });
    model = await AutoModel.from_pretrained(meta.model, {
      ...rest, revision: meta.revision, config, device, dtype,
    });
  } finally {
    if (typeof previousTemplate === 'string') env.remotePathTemplate = previousTemplate;
  }
  return {
    dispose: () => model.dispose(),
    async embed(texts) {
      const inputs = await tokenizer(texts, { padding: true, truncation: true, max_length: meta.max_length });
      const { sentence_embedding } = await model(inputs);
      return truncateNormalize(sentence_embedding, meta.dim);
    },
  };
}

export async function loadBrowserEncoder(transformers, meta, progress, gpu = globalThis.navigator?.gpu) {
  // Adapter availability alone is not sufficient: session creation can also fail.
  if (gpu) {
    try {
      if (await gpu.requestAdapter()) {
        const encoder = await loadTextEncoder(transformers, meta, {
          device: 'webgpu', dtype: meta.query_dtype, progress_callback: progress,
        });
        return { encoder, device: 'webgpu', dtype: meta.query_dtype };
      }
    } catch (error) {
      progress({ status: 'fallback', device: 'wasm', dtype: meta.query_wasm_dtype,
        total: meta.wasm_model_download_bytes, reason: String(error.message || error) });
    }
  }
  const encoder = await loadTextEncoder(transformers, meta, {
    device: 'wasm', dtype: meta.query_wasm_dtype, progress_callback: progress,
  });
  return { encoder, device: 'wasm', dtype: meta.query_wasm_dtype };
}

if (typeof WorkerGlobalScope !== 'undefined' && self instanceof WorkerGlobalScope) {
  let encoder = null;
  let meta = null;
  self.onmessage = async ({ data: m }) => {
    try {
      if (m.type === 'init') {
        meta = m.meta;
        const transformers = await import(TRANSFORMERS);
        const progress = (p) => self.postMessage({ ...p, type: 'progress' });
        const loaded = await loadBrowserEncoder(transformers, meta, progress);
        encoder = loaded.encoder;
        self.postMessage({ type: 'ready', device: loaded.device, dtype: loaded.dtype });
      } else if (m.type === 'embed') {
        if (!encoder) throw new Error('Embedding model not initialized');
        const [vec] = await encoder.embed([meta.query_prefix + m.text]);
        self.postMessage({ type: 'vec', id: m.id, vec }, [vec.buffer]);
      }
    } catch (err) {
      self.postMessage({ type: 'error', id: m.id ?? null, error: String(err?.message || err) });
    }
  };
}
