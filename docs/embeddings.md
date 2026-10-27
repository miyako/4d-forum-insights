# Multilingual semantic embeddings

The main index, Tips, and Voices share one topic-ID vector index and the same lazy
query worker. Keyword search never needs the model. “Similar topics” downloads
only the stored vectors, not the inference model.

## Reproducible text-only inference

`config/site.json` pins `onnx-community/embeddinggemma-2-ONNX` at revision
`daa72c51243991dfcaf9f9137d2c573d8f7790c0` and Transformers.js **4.3.1**.
The [upstream config](https://huggingface.co/onnx-community/embeddinggemma-2-ONNX/blob/daa72c51243991dfcaf9f9137d2c573d8f7790c0/config.json)
contains vision and audio encoders. Both are explicitly disabled by loading
`AutoConfig` and setting `vision_config = audio_config = null` **before**
`AutoModel.from_pretrained`. `AutoTokenizer` supplies text tokens directly; no
processor or feature-extraction pipeline loads media encoders.
Transformers.js 4.3.1's tokenizer-discovery probe omits its revision option.
While loading, the shared loader also pins the environment's remote URL
template, then restores it, so even that existence probe cannot consult `main`.

Documents use `title: {title} | text: {content}` (empty title: `none`).
The existing canonical `build_data.py` JSONL format is supported directly:
its first line is the title, and the remaining summary/opening-post and tags
form the content. Explicit `title`/`content` fields are also accepted.
Queries use `task: search result | query: {query}`.
The text graph's `sentence_embedding` is already pooled/projected to 768
dimensions. We take its first **256 MRL dimensions**, then L2-normalize.
The builder subsequently quantizes each document vector to signed int8 using
its maximum absolute component. Search reconstructs cosine direction from each
stored row's norm; its discarded absolute scale is unnecessary.

Document inference and preferred WebGPU query inference use **q4 weights**,
distinct from int8 vector storage. Browser queries fall back to **fp16 WASM**
if an adapter or GPU session cannot be obtained. Upstream q4 and q8 graphs use
`GatherBlockQuantized`, which the WASM CPU backend does not implement; claiming
q4 WASM support would be incorrect. Backend failures are surfaced; keyword
search remains available. WASM is usually slower.

The first semantic query lazily downloads approximately **207 MB** of text model
and tokenizer files (174,028,800-byte q4 external weights, 490,742-byte graph,
32,170,510-byte tokenizer, and small configuration files), plus the inference
runtime. Browser Cache Storage reuses downloaded files where supported.
Neither the vision nor audio graph or weights should appear in network traffic.
**The fp16 WASM fallback is larger: approximately 575 MB**, including its
542,085,120-byte weights, 425,780-byte graph, and the same tokenizer/config.
If a failed GPU initialization has already downloaded q4 weights, fp16 weights
are an additional download; cached tokenizer/config files are shared.

## Build and cache

After building the complete data/input set, run:

```sh
npm ci
node scripts/build_embeddings.mjs
```

Default inference is Node CPU, q4, batches of four, two threads, and a 2,048-token
text limit. `--device webgpu` (or `EMBED_DEVICE=webgpu`) can use a supported native
ONNX Runtime WebGPU device for faster indexing. `--batch`, `--threads`,
`--max-length`, and `--cache-dir` can tune a
build. The model supports a larger context, but this explicit build/query limit
keeps browser memory bounded and is part of the index contract.

`--sample N` and `--limit N` are development/incremental options, **not** evidence
of full coverage. Check `manifest.embeddings.count` against the topic count
before publishing. Updated or removed documents cannot leave stale vectors in
a partial index.

For the 17,017-topic published release used to validate this migration, all
topics were enriched. Its canonical input was therefore recoverable from
`topics.json`, `text.json`, and the manifest's tag dictionary without the raw
database: English title (or original title), summary line, summary paragraph,
and optional `Tags: …`, joined with newlines exactly as in `build_data.py`.
Every reconstructed legacy SHA-1 text hash matched the released
`embeddings-hashes.json` by topic ID (17,017 matches, zero mismatches). Those
verified source hashes are provenance checks, not the new vector-cache hashes.
An unenriched topic's opening post is not published, so the same reconstruction
must stop rather than invent missing text for a future partially enriched
release.

The completed migration index contains **17,017 × 256 int8 values**
(**4,356,352 bytes**, zero stale rows), built with q4 native WebGPU inference.
All new contract/text hashes were verified, and a subsequent unrestricted
builder run reused all 17,017 rows with zero inference work. Coverage checks
included all 10,287 Tips entries and 277 Voices entries in this release.
Japanese and French query smoke tests and stored-vector nearest-neighbor
search ran against the complete index; these are integration checks, not
retrieval-quality benchmarks.

The manifest records model, immutable revision, Transformers.js version,
dimension, pooling/output name, document template, query prefix, document/query
precision, token limit, and storage quantization. All enter the cache identity.
Each row additionally hashes the actual formatted text and identity, not an
untrusted old input hash. An old BGE index, a dimension change, a prefix change,
or a model/precision/revision change forces recomputation. Incompatible
manifests are not offered as semantic search by the shared browser client.

## Release coordination: keep the migration PR draft

The currently published data release contains a legacy BGE index. Pages reads
its release manifest independently of the JavaScript deployment. The new
browser client intentionally rejects that legacy contract **before downloading
vectors or initializing Gemma**; it never scores Gemma queries against BGE
vectors. Keyword search remains usable, but semantic search stays unavailable
until compatible data is installed.
The main index applies that same compatibility guard to its mode indicators,
footer, and Similar topics control. Its tooltips, persistent footer help, and
loading/fallback status disclose text-only Gemma's approximately 207 MB
WebGPU or 575 MB WASM download. Chromium checks verified that main-index keyword
search makes no model/runtime/vector requests, and selecting Semantic against a
legacy release shows an incompatibility warning without initiating inference.

**Keep the migration PR draft until a compatible data artifact can be promoted
before or together with the code merge/deployment.** Completing a local build
does not mean that production has migrated. No production publication is part
of this implementation.

The fully verified, nonproduction artifact is in
`build/embeddinggemma-index/`: `embeddings.bin`, `embeddings-ids.json`,
`embeddings-hashes.json`, and a manifest containing its embedding contract.
For a later explicitly authorized promotion, package those vector files and
merge its `embeddings` and `embedding_config` fields into the **current** release
manifest. Preserve that manifest's topic, Tips, Voices, and localization
metadata rather than replacing it with an older snapshot. Before promotion,
verify corpus IDs/text hashes still match this artifact; rebuild changed rows
if the corpus has advanced. Publish matching files/manifest as one release
artifact, verify full coverage and contract compatibility, and only then
complete the code rollout.

`node --test tests/embeddings.test.mjs` covers the loader contract, truncation
order, backend fallback, cache invalidation, and legacy/stale-index rejection
without downloading a model.

`EMBED_INFERENCE_TEST=1 node --test tests/embeddings.test.mjs` also runs real
pinned q4 CPU inference and a multi-batch int8 build/cache round trip. It
downloads model assets to `build/hf-cache` if absent.

Manual verification used Chrome 156 on macOS, Transformers.js 4.3.1's shipped
browser bundle, and the pinned graphs/tokenizer served locally to avoid
redundant downloads. Real q4 WebGPU and fp16 WASM inference both produced finite,
unit-length 256d vectors for a Japanese search query. The q4 CPU and WebGPU
vectors agreed to approximately 1e-7 per component in that smoke test.
This is a backend smoke test, not a multilingual retrieval-quality benchmark or
a claim that all browsers/devices support the model.

Playwright also exercised the actual, unchanged module worker's `init` and
`embed` messages end-to-end: q4 WebGPU on GPU-enabled headless Chrome 156 and
fp16 WASM on headless Chromium 156 with WebGPU explicitly disabled. Both
returned unit-length 256d vectors, with no JavaScript errors. Download requests
were redirected to the identical cached pinned assets to avoid duplicate
large downloads; assertions required the immutable revision on every Hub
request and rejected all audio/vision requests. The GPU-enabled run loaded
only `model_q4.onnx`/external weights; WASM loaded only the fp16 equivalents.
