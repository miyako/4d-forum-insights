// /tips/: a knowledge base mined from the forum. Topics are classified by area and Q&A type (the "tips-…" model
// run, manifest.tips) and the answered questions and tips are rewritten into short entries (tips/entries.json).
import { loadManifest, loadTopics, loadText, dataUrl, decodeRun } from './data.js';
import { KeywordIndex } from './search.js';
import { Semantic } from './semantic.js';
import { rrf } from './filter.js';
import {
  parseTipsState, serializeTipsState, buildItems, tipsPredicates, applyTips, sortTips, SORTS,
} from './tips-core.js';

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const num = new Intl.NumberFormat('en');
const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
const pct = (p) => `${Math.round(p * 100)}%`;
const PAGE = 30;
const SEM_CANDIDATES = 3000;
const TYPE_BADGE = { solved: 'Q&A', tip: 'Tip', open: 'Open', other: 'Other' };

const app = {
  manifest: null, tips: null, items: [], byId: new Map(), index: null, semantic: null, state: null,
  areaQ: null, typeQ: null, shown: PAGE, terms: [], textLoaded: false,
  sem: { q: null, list: null, busy: false, error: null, progress: null },
};

async function getJSON(rel) {
  const res = await fetch(dataUrl(rel));
  if (!res.ok) throw new Error(`${rel}: HTTP ${res.status}`);
  return res.json();
}

// ------------------------------------------------------------------------------------------- boot

async function init() {
  try {
    app.manifest = await loadManifest();
  } catch (e) {
    return fail(`Could not load data/manifest.json (${esc(e.message)}).`);
  }
  const m = app.manifest;
  app.tips = m.tips;
  if (!app.tips?.runs?.length) return fail('No knowledge-base data yet: run <code>fminer kb-classify</code> and <code>make data</code>.');
  document.title = app.tips.title;
  app.state = parseTipsState(location.search);

  const run = app.tips.runs.find((r) => r.id === app.tips.default_run) || app.tips.runs[0];
  const [topics, runData, entries] = await Promise.all([
    loadTopics(m), getJSON(run.file), getJSON(app.tips.files.entries),
  ]);
  const answers = decodeRun(runData, { questions: app.tips.questions, runs: app.tips.runs });
  const entryMap = new Map(entries.ids.map((id, i) => [id, {
    mode: entries.modes[entries.mode[i]], headline: entries.headline[i], body: entries.body[i], ok: entries.ok[i],
  }]));
  app.areaQ = app.tips.questions.find((q) => q.id === 'area');
  app.typeQ = app.tips.questions.find((q) => q.id === 'qa');
  const keys = { area: app.areaQ.choices.map((c) => c.key), qa: app.typeQ.choices.map((c) => c.key) };
  app.items = buildItems(topics, answers, entryMap, keys, app.tips.area_min_p);
  app.items.forEach((it) => app.byId.set(it.id, it));
  app.run = run;
  app.topicCount = topics.length;

  // keyword index over what the page shows: entry headline/body, original title, tags
  app.index = new KeywordIndex(app.items.map((it) => ({
    id: it.id, title: it.headline, title_en: it.topic.title_en || it.topic.title, line: it.body, tags: it.topic.tags,
  })));
  app.semantic = new Semantic(m.embeddings);
  app.semantic.onProgress = onModelProgress;

  renderStatic();
  syncControls();
  update({ replace: true });
  if (app.state.mode !== 'kw') startSemantic();
  loadText(m, topics).then(() => { app.textLoaded = true; }).catch((e) => console.warn('text.json', e));

  window.addEventListener('popstate', () => {
    app.state = parseTipsState(location.search);
    syncControls();
    update({ history: false });
    if (app.state.mode !== 'kw') startSemantic();
  });
}

function fail(html) {
  $('#count').textContent = '';
  showStatus(html, 'error');
}

// ------------------------------------------------------------------------------------------- search

function candidates() {
  const s = app.state;
  const q = s.q.trim();
  app.terms = [];
  if (!q) return { base: app.items, relevance: null, note: '' };
  const wantSem = s.mode !== 'kw' && app.semantic.available;
  const semReady = wantSem && app.sem.q === q && app.sem.list;
  let kw = [];
  if (s.mode !== 'sem' || !semReady) {
    kw = app.index.search(q);
    app.terms = [...new Set(kw.slice(0, 200).flatMap((r) => r.terms))];
  }
  if (!semReady) {
    const note = wantSem ? (app.sem.error ? 'semantic search unavailable — keyword results' : 'keyword results while the semantic model loads…') : '';
    return { base: kw.map((r) => app.byId.get(r.id)), relevance: new Map(kw.map((r) => [r.id, r.score])), note };
  }
  const sem = app.sem.list.filter((r) => app.byId.has(r.id)).slice(0, 400);
  if (s.mode === 'sem') {
    return { base: sem.map((r) => app.byId.get(r.id)), relevance: new Map(sem.map((r) => [r.id, r.score])), note: 'semantic: most similar topics' };
  }
  const fused = rrf([kw.map((r) => r.id), sem.map((r) => r.id)]);
  return { base: [...fused.keys()].map((id) => app.byId.get(id)), relevance: fused, note: 'hybrid: keyword + semantic' };
}

async function startSemantic() {
  const q = app.state.q.trim();
  if (!q || !app.semantic.available || app.sem.busy || app.sem.q === q) return;
  app.sem.busy = true;
  try {
    const list = await app.semantic.search(q, SEM_CANDIDATES);
    app.sem = { ...app.sem, q, list, error: null, progress: null };
  } catch (e) {
    app.sem.error = e.message;
    console.warn('semantic', e);
  } finally {
    app.sem.busy = false;
  }
  if (app.state.q.trim() !== q && app.state.mode !== 'kw') return startSemantic();
  update({ history: false, keepPage: true });
}

const progress = new Map();
function onModelProgress(p) {
  if (p.file && p.total) progress.set(p.file, [p.loaded || 0, p.total]);
  if (p.status === 'done' && progress.has(p.file)) progress.set(p.file, [progress.get(p.file)[1], progress.get(p.file)[1]]);
  let l = 0, t = 0;
  for (const [a, b] of progress.values()) { l += a; t += b; }
  if (!app.sem.list && t) {
    showStatus(`Loading the semantic model (first time only): ${(l / 1e6).toFixed(1)} / ${(t / 1e6).toFixed(1)} MB…`
      + `<span class="bar"><span style="width:${Math.round((100 * l) / t)}%"></span></span>`, 'info');
  }
}

// ------------------------------------------------------------------------------------------- update

function update({ replace = false, history = true, keepPage = false } = {}) {
  const s = app.state;
  if (history) {
    const qs = serializeTipsState(s);
    const url = location.pathname + (qs ? '?' + qs : '');
    if (url !== location.pathname + location.search) (replace ? window.history.replaceState : window.history.pushState).call(window.history, null, '', url);
  }
  if (!keepPage) app.shown = PAGE;
  const { base, relevance, note } = candidates();
  const preds = tipsPredicates(s);
  const sort = s.sort && (s.sort !== 'relevance' || relevance) ? s.sort : (relevance ? 'relevance' : 'new');
  app.results = sortTips(applyTips(base, preds), sort, relevance);
  renderAreas(base, preds);
  renderTypes(base, preds);
  renderSort(!!relevance, sort);
  renderResults(note);
}

// ------------------------------------------------------------------------------------------- render

function renderStatic() {
  const m = app.manifest;
  const years = app.items.map((it) => it.topic.created).filter((d) => d != null).map((d) => new Date(d * 864e5).getUTCFullYear());
  const lo = Math.min(...years), hi = Math.max(...years);
  $('#since').innerHTML = '<option value="0">Any year</option>'
    + Array.from({ length: hi - lo + 1 }, (_, i) => hi - i).map((y) => `<option value="${y}">${y}+</option>`).join('');
  $('#presets').innerHTML = '<span class="muted">Try:</span> ' + (app.tips.presets || []).map((p) =>
    `<a class="preset" href="?${esc(p.params)}" data-params="${esc(p.params)}">${esc(p.label)}</a>`).join('');
  const cover = app.run.count < 0.98 * m.stats.enriched
    ? ` <strong>Partial:</strong> ${num.format(app.run.count)} of ${num.format(m.stats.enriched)} topics classified so far.` : '';
  $('#footer').innerHTML = `
    <p>${esc(app.tips.description)}${cover}</p>
    <p>${num.format(app.tips.entries)} knowledge-base entries written by a local model; area and type by
      <code>${esc(app.run.model)}</code> (${esc(app.run.prompt_version)}). Entries are machine-generated from public
      threads on <a href="${esc(m.forum_url)}">${esc(m.forum_url.replace(/^https?:\/\//, ''))}</a>; the forum posts
      themselves are not republished. Data ${esc((m.generated_at || '').slice(0, 10))}.
      <a href="../">Forum Insights</a> · <a href="https://github.com/miyako/4d-forum-insights">Source</a></p>`;

  $('#search-form').addEventListener('submit', (e) => { e.preventDefault(); setQuery($('#q').value); });
  let timer;
  $('#q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => setQuery($('#q').value, true), 220); });
  document.querySelectorAll('.modes button').forEach((b) => b.addEventListener('click', () => {
    app.state.mode = b.dataset.mode;
    syncControls();
    update({ keepPage: true });
    if (app.state.mode !== 'kw') startSemantic();
  }));
  $('#conf').addEventListener('change', (e) => { app.state.conf = Number(e.target.value); update(); });
  $('#since').addEventListener('change', (e) => { app.state.since = Number(e.target.value); update(); });
  $('#sort').addEventListener('change', (e) => { app.state.sort = e.target.value; update(); });
  $('#presets').addEventListener('click', (e) => {
    const a = e.target.closest('[data-params]');
    if (!a) return;
    e.preventDefault();
    app.state = parseTipsState('?' + a.dataset.params + (app.state.mode !== 'kw' ? `&mode=${app.state.mode}` : ''));
    syncControls();
    update();
    if (app.state.mode !== 'kw') startSemantic();
  });
  $('#areas').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-area]');
    if (!b) return;
    const k = b.dataset.area;
    if (!k) app.state.area = [];
    else app.state.area = app.state.area.includes(k) ? app.state.area.filter((x) => x !== k) : [...app.state.area, k];
    update();
  });
  $('#types').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-type]');
    if (!b) return;
    const k = b.dataset.type;
    const next = app.state.type.includes(k) ? app.state.type.filter((x) => x !== k) : [...app.state.type, k];
    app.state.type = next.length ? next : [k];
    update();
  });
  $('#results').addEventListener('click', (e) => {
    const a = e.target.closest('[data-area-chip]');
    if (a) {
      e.preventDefault();
      app.state.area = [a.dataset.areaChip];
      update();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  });
  $('#results').addEventListener('toggle', (e) => {
    const d = e.target;
    if (d.matches?.('details.why') && d.open) renderWhy(d);
  }, true);
  $('#more').addEventListener('click', (e) => {
    if (!e.target.closest('button')) return;
    app.shown += PAGE;
    renderResults();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName)) { e.preventDefault(); $('#q').focus(); }
  });
}

function setQuery(v, typing = false) {
  if (v === app.state.q) return;
  app.state.q = v;
  if (!v.trim() && app.state.sort === 'relevance') app.state.sort = '';
  update({ replace: typing });
  if (app.state.mode !== 'kw') startSemantic();
}

function syncControls() {
  const s = app.state;
  if ($('#q').value !== s.q) $('#q').value = s.q;
  document.querySelectorAll('.modes button').forEach((b) => {
    b.setAttribute('aria-checked', String(b.dataset.mode === s.mode));
    b.classList.toggle('unavailable', b.dataset.mode !== 'kw' && !app.semantic?.available);
  });
  $('#conf').value = [0, 50, 70].includes(s.conf) ? String(s.conf) : '0';
  $('#since').value = String(s.since || 0);
}

function renderAreas(base, preds) {
  const pool = applyTips(base, preds, 'area');
  const counts = new Map();
  for (const it of pool) for (const a of it.areas) counts.set(a.key, (counts.get(a.key) || 0) + 1);
  const sel = new Set(app.state.area);
  $('#areas').innerHTML = `<button type="button" class="area${sel.size ? '' : ' active'}" data-area="" aria-pressed="${!sel.size}">All areas <span class="n">${num.format(pool.length)}</span></button>`
    + app.areaQ.choices.map((c) => {
      const n = counts.get(c.key) || 0;
      const on = sel.has(c.key);
      return `<button type="button" class="area area-${esc(c.key)}${on ? ' active' : ''}${n ? '' : ' zero'}" data-area="${esc(c.key)}" aria-pressed="${on}" title="${esc(c.long)}">${esc(c.label)} <span class="n">${num.format(n)}</span></button>`;
    }).join('');
}

function renderTypes(base, preds) {
  const pool = applyTips(base, preds, 'type');
  const conf = app.state.conf / 100;
  const counts = new Map();
  for (const it of pool) if (it.type.p >= conf - 1e-9) counts.set(it.type.key, (counts.get(it.type.key) || 0) + 1);
  $('#types').innerHTML = app.typeQ.choices.map((c) => {
    const on = app.state.type.includes(c.key);
    return `<button type="button" class="type type-${esc(c.key)}${on ? ' active' : ''}" data-type="${esc(c.key)}" aria-pressed="${on}" title="${esc(c.long)}">${esc(c.label)} <span class="n">${num.format(counts.get(c.key) || 0)}</span></button>`;
  }).join('');
}

function renderSort(hasQuery, current) {
  $('#sort').innerHTML = SORTS.filter((o) => !o.needsQuery || hasQuery)
    .map((o) => `<option value="${o.key}"${o.key === current ? ' selected' : ''}>${o.label}</option>`).join('');
}

function highlight(text) {
  const t = esc(text);
  const terms = app.terms.filter((x) => x.length > 2).slice(0, 30);
  if (!terms.length) return t;
  const re = new RegExp(`(${terms.map((x) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
  return t.replace(/(^|>)([^<]+)/g, (m, a, b) => a + b.replace(re, '<mark>$1</mark>'));
}

const fmtDate = (d) => (d == null ? '' : new Date(d * 864e5).toISOString().slice(0, 10));

function card(it) {
  const t = it.topic;
  const m = app.manifest;
  const url = `${m.forum_url}/t/${t.slug}/${t.id}`;
  const label = (k) => app.areaQ.choices.find((c) => c.key === k)?.label || k;
  const type = it.type.key;
  const fallback = !it.entry;
  const orig = it.entry && (t.title_en || t.title) !== it.headline ? (t.title_en || t.title) : null;
  const accepted = t.flags & (m.flags.accepted || 4);
  return `<article class="tip-card type-${esc(type)}">
    <div class="tip-head">
      <span class="badge badge-${esc(type)}" title="${esc(app.typeQ.choices.find((c) => c.key === type)?.long || '')} (${pct(it.type.p)})">${esc(TYPE_BADGE[type] || type)}</span>
      <h2 class="tip-title"><a href="${esc(url)}" target="_blank" rel="noopener">${highlight(it.headline)}</a></h2>
    </div>
    <p class="tip-body">${highlight(it.body)}${fallback ? ' <span class="muted small">(thread summary; no knowledge-base entry yet)</span>' : ''}</p>
    <div class="tip-meta">
      ${it.areas.map((a) => `<a href="?area=${esc(a.key)}" class="chip area-chip area-${esc(a.key)}" data-area-chip="${esc(a.key)}" title="${pct(a.p)}">${esc(label(a.key))}</a>`).join('')}
      <span class="meta">${orig ? `<span class="orig-title" title="Thread title">“${esc(orig)}”</span><span class="dot">·</span>` : ''}
        <span>${fmtDate(t.created)}</span><span class="dot">·</span><span>${esc((t.lang || '').toUpperCase())}</span>
        <span class="dot">·</span><span>💬 ${num.format(Math.max(0, t.posts - 1))}</span>
        <span class="dot">·</span><span title="views">👁 ${compact.format(t.views)}</span>
        ${t.likes ? `<span class="dot">·</span><span title="likes">♥ ${num.format(t.likes)}</span>` : ''}
        ${accepted ? '<span class="dot">·</span><span class="solved" title="The thread has an accepted answer">✓ accepted answer</span>' : ''}
      </span>
      <a class="thread-link" href="${esc(url)}" target="_blank" rel="noopener">Open thread ↗</a>
    </div>
    <details class="why" data-id="${t.id}"><summary>Why is this here?</summary><div class="why-body"></div></details>
  </article>`;
}

function renderWhy(d) {
  const it = app.byId.get(Number(d.dataset.id));
  if (!it) return;
  const ans = it.topic;
  const area = it.areas.map((a) => `${esc(app.areaQ.choices.find((c) => c.key === a.key)?.label || a.key)} ${pct(a.p)}`).join(', ');
  const typeLabel = app.typeQ.choices.find((c) => c.key === it.type.key)?.label || it.type.key;
  const status = it.entry
    ? (it.entry.ok === 0 ? 'The writer model found no real answer in the thread (hidden unless “show unverified”).'
      : it.entry.ok === 2 ? 'The entry did not pass every quality check (length or phrasing).' : 'Entry passed the quality checks.')
    : 'No knowledge-base entry yet: the text above is the thread summary.';
  d.querySelector('.why-body').innerHTML = `
    <dl class="facts">
      <dt>Type</dt><dd>${esc(typeLabel)} (${pct(it.type.p)})</dd>
      <dt>Area</dt><dd>${area || '—'}</dd>
      <dt>Entry</dt><dd>${esc(status)}</dd>
      <dt>Thread</dt><dd>${esc(ans.title)}</dd>
    </dl>
    ${app.textLoaded && ans.para ? `<p class="small"><strong>Thread summary:</strong> ${esc(ans.para)}</p>` : ''}
    <p class="small"><a href="../?t=${ans.id}">Open in Forum Insights</a></p>`;
}

function renderResults(note = app.lastNote || '') {
  app.lastNote = note;
  const s = app.state;
  const total = app.results.length;
  const scope = s.area.length ? ` in ${s.area.map((k) => app.areaQ.choices.find((c) => c.key === k)?.label || k).join(' + ')}` : '';
  $('#count').innerHTML = `<strong>${num.format(total)}</strong> ${total === 1 ? 'entry' : 'entries'}${esc(scope)}`
    + (note ? ` <span class="muted small">· ${esc(note)}</span>` : '');
  if (app.sem.list || !(s.mode !== 'kw' && s.q.trim())) {
    if (!app.sem.error) $('#status').hidden = true;
  }
  if (app.sem.error && s.mode !== 'kw') showStatus(`Semantic search unavailable: ${esc(app.sem.error)}`, 'warn');
  if (!total) {
    $('#results').innerHTML = `<div class="empty">Nothing found. Try other words, the <strong>Semantic</strong> mode, more areas or types${s.conf ? ', or a lower confidence' : ''}.</div>`;
    $('#more').innerHTML = '';
    return;
  }
  $('#results').innerHTML = app.results.slice(0, app.shown).map(card).join('');
  $('#more').innerHTML = total > app.shown
    ? `<button type="button" class="btn">Show more (${num.format(total - app.shown)} left)</button>` : '';
}

function showStatus(html, kind = 'info') {
  const st = $('#status');
  st.className = `status ${kind}`;
  st.innerHTML = html;
  st.hidden = false;
}

init().catch((e) => {
  console.error(e);
  fail(`Something went wrong: ${esc(e.message)}`);
});
