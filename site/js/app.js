// UI: everything (filters, sort options, table columns, chips, detail panel) is generated from
// data/manifest.json, so a new model question shows up without code changes.
import { loadManifest, loadTopics, loadText, loadRun, getJSON } from './data.js';
import { parseState, parseRequestsState, serializeState, hasFilters } from './state.js';
import {
  buildPredicates, applyFilters, facetCounts, sortOptions, sortTopics, rrf, NOUL_FILTERS, dateToDays,
} from './filter.js';
import { KeywordIndex, normalize } from './search.js';
import { Semantic } from './semantic.js';
import { preferredLocale, localeURL, navigationURL, stateURL, installLocale, loadOverlay } from './locale.js';
import { createRequestsLocale, localizeRequestsManifest } from './requests-messages.js';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const i18n = createRequestsLocale(preferredLocale(location.search));
const { t: tr, number: num, compact, pct, date: fmtDate } = i18n;
const decimal = new Intl.NumberFormat(i18n.locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const score = (value) => decimal.format(value);
const stateDate = (value) => Number.isFinite(dateToDays(value)) ? fmtDate(dateToDays(value)) : value;
installLocale(i18n, 'requests');
document.title = tr('4D Forum Requests');
const STATE_KEYS = ['q', 'mode', 'sort', 'run', 'view', 't', 'cat', 'xcat', 'lang', 'tag', 'from', 'to', 'votes', 'cls', 'enriched', 'scope'];
const noulLabel = (key) => tr({
  yes: 'Yes (≥ {percent})', strong: 'Strong yes (≥ {percent})', no: 'No (< {percent})',
}[key] || key, { percent: pct(key === 'strong' ? .8 : .5) });
const PAGE = 50;
const SEM_CANDIDATES = 500;

const app = {
  manifest: null, topics: [], byId: new Map(), cats: new Map(), index: null, semantic: null,
  state: null, questions: [], shown: PAGE, results: [], relevance: null, terms: [],
  sem: { q: null, list: null, busy: false, error: null, progress: null }, textLoaded: false,
};

// ------------------------------------------------------------------------------------------- boot

async function init() {
  try {
    app.manifest = localizeRequestsManifest(await loadManifest(), i18n);
  } catch (e) {
    $('#count').textContent = '';
    showStatus(`${esc(tr('Could not load data/manifest.json ({error}). Build it with', { error: e.message }))} <code>make data</code>.`, 'error');
    return;
  }
  const m = app.manifest;
  document.title = tr('4D Forum Requests');
  m.categories.forEach((c) => app.cats.set(c.id, c));
  app.state = parseRequestsState(location.search, m.questions, m.presets);
  app.topics = await loadTopics(m);
  // Hash all generated fields together before applying an overlay, including empty paragraphs.
  await loadText(m, app.topics);
  app.textLoaded = true;
  app.topics = await loadOverlay(app.topics, 'requests', i18n.locale, getJSON);
  app.topics.forEach((t) => app.byId.set(t.id, t));
  app.semantic = new Semantic(m.embeddings);
  app.semantic.onProgress = onModelProgress;
  await selectRun(app.state.run);
  app.index = new KeywordIndex(app.topics);
  await app.index.addParagraphs();

  renderStatic();
  update({ replace: true });
  if (app.state.mode !== 'kw') startSemantic();
  if (app.state.t) openDetail(app.state.t, { push: false });

  window.addEventListener('popstate', async () => {
    if (preferredLocale(location.search) !== i18n.locale) return location.reload();
    const prevRun = app.state.run;
    app.state = parseRequestsState(location.search, m.questions, m.presets);
    if (app.state.run !== prevRun) await selectRun(app.state.run);
    syncControls();
    update({ history: false });
    if (app.state.mode !== 'kw') startSemantic();
    app.state.t ? openDetail(app.state.t, { push: false }) : closeDetail({ push: false });
  });
}

async function selectRun(runId) {
  const m = app.manifest;
  const run = m.runs.find((r) => r.id === runId) || m.runs.find((r) => r.id === m.default_run) || m.runs[0];
  app.state.run = run && run.id !== m.default_run ? run.id : '';
  app.run = run || null;
  app.questions = run ? m.questions.filter((q) => run.questions.includes(q.id)) : [];
  const answers = run ? await loadRun(m, run.id) : new Map();
  for (const t of app.topics) t.ans = answers.get(t.id) || null;
}

// ------------------------------------------------------------------------------------------- search

function currentSort() {
  const s = app.state;
  const valid = sortOptions(app.questions, !!s.q.trim()).map((o) => o.key);
  if (s.sort && valid.includes(s.sort)) return s.sort;
  return s.q.trim() ? 'relevance' : 'active';
}

/** Candidate topics for the query (before filters) and their relevance scores. */
function candidates() {
  const s = app.state;
  const q = s.q.trim();
  app.terms = [];
  if (!q) return { base: app.topics, relevance: null, note: '' };
  const wantSem = s.mode !== 'kw' && app.semantic.available;
  const semReady = wantSem && app.sem.q === q && app.sem.list;
  let kw = [];
  if (s.mode !== 'sem' || !semReady) {
    kw = app.index.search(q);
    app.terms = [...new Set(kw.slice(0, 200).flatMap((r) => r.terms))];
    app.terms.push(...q.split(/\s+/).filter((w) => w.length > 1 && !w.startsWith('-')).map((w) => normalize(w.replace(/"/g, ''))));
  }
  if (!semReady) {
    const note = wantSem ? tr(app.sem.error ? 'semantic search unavailable — keyword results' : 'keyword results while the semantic model loads…') : '';
    return { base: kw.map((r) => app.byId.get(r.id)), relevance: new Map(kw.map((r) => [r.id, r.score])), note };
  }
  const sem = app.sem.list.slice(0, SEM_CANDIDATES);
  const cover = app.manifest.embeddings.count < app.topics.length
    ? tr(' · embeddings cover {count} of {total} topics', { count: num.format(app.manifest.embeddings.count), total: num.format(app.topics.length) }) : '';
  if (s.mode === 'sem') {
    return {
      base: sem.map((r) => app.byId.get(r.id)).filter(Boolean),
      relevance: new Map(sem.map((r) => [r.id, r.score])),
      sim: new Map(sem.map((r) => [r.id, r.score])),
      note: tr('semantic: top {count} most similar topics', { count: num.format(sem.length) }) + cover,
    };
  }
  const fused = rrf([kw.map((r) => r.id), sem.map((r) => r.id)]);
  return {
    base: [...fused.keys()].map((id) => app.byId.get(id)).filter(Boolean),
    relevance: fused,
    sim: new Map(sem.map((r) => [r.id, r.score])),
    note: tr('hybrid: keyword + semantic (reciprocal rank fusion)') + cover,
  };
}

function update({ replace = false, history = true, keepPage = false } = {}) {
  const s = app.state;
  if (!hasFilters(s)) s.scope = 'all';
  // filter/search changes replace the current history entry; opening a topic pushes one (Back closes it)
  if (history || replace) window.history.replaceState(null, '', urlFor(s));
  refreshLocaleLinks();
  const { base, relevance, sim, note } = candidates();
  const ctx = { questions: app.questions, flags: app.manifest.flags };
  const preds = buildPredicates(s, ctx);
  const filtered = applyFilters(base, preds);
  const sort = currentSort();
  app.relevance = relevance;
  app.sim = sim || null;
  app.results = sortTopics(filtered, sort, app.questions, relevance);
  if (!keepPage) app.shown = PAGE;

  renderFacets(base, preds);
  renderSort(sort);
  renderActive();
  renderPresets();
  $('#count').innerHTML = tr(app.results.length === 1 ? '{count} topic' : '{count} topics', { count: `<strong>${num.format(app.results.length)}</strong>` })
    + (note ? ` <span class="muted">· ${esc(note)}</span>` : '');
  renderResults();
}

let semTimer = 0;
function scheduleSemantic() {
  clearTimeout(semTimer);
  if (app.state.mode === 'kw' || !app.state.q.trim() || !app.semantic.available) return;
  semTimer = setTimeout(runSemantic, 350);
}

async function runSemantic() {
  const q = app.state.q.trim();
  if (!q || app.sem.q === q && app.sem.list) return;
  try {
    const list = await app.semantic.search(q, SEM_CANDIDATES);
    if (app.state.q.trim() !== q) return;
    app.sem = { ...app.sem, q, list, error: null };
    hideStatus();
    update({ history: false });
  } catch (e) {
    app.sem.error = e.message;
    showStatus(esc(tr('Semantic search failed: {error}', { error: e.message })), 'error');
  }
}

function startSemantic() {
  if (!app.semantic.available) {
    showStatus(app.manifest.embeddings
      ? esc(tr('Semantic search is unavailable: this release has an incompatible embedding index. Rebuild and publish the matching EmbeddingGemma 2 index; keyword search still works.'))
      : `${esc(tr('Semantic search is not available: this data build has no embeddings yet'))} (<code>make embeddings</code>).`, 'warn');
    return;
  }
  if (!app.sem.ready) {
    showStatus(esc(tr('Loading text-only EmbeddingGemma 2: about 207 MB for q4 WebGPU, or 575 MB for the fp16 WASM fallback. Downloaded on first use and cached by your browser when supported…')), 'info');
    app.semantic.ready().then(() => {
      app.sem.ready = true;
      hideStatus();
      scheduleSemantic();
    }).catch((e) => {
      app.sem.error = e.message;
      showStatus(esc(tr('Could not load the semantic model: {error}. Keyword search still works.', { error: e.message })), 'error');
      update({ history: false });
    });
  }
  scheduleSemantic();
}

const progress = new Map();
function onModelProgress(p) {
  if (p.status === 'fallback') {
    progress.clear();
    showStatus(esc(tr('WebGPU is unavailable. Loading text-only EmbeddingGemma 2 with fp16 WASM (about 575 MB); keyword search still works.')), 'info');
    return;
  }
  if (p.file && p.total) progress.set(p.file, [p.loaded || 0, p.total]);
  if (p.status === 'done' && progress.has(p.file)) progress.set(p.file, [progress.get(p.file)[1], progress.get(p.file)[1]]);
  let l = 0, t = 0;
  for (const [a, b] of progress.values()) { l += a; t += b; }
  if (!app.sem.ready && t) {
    showStatus(esc(tr('Loading text-only EmbeddingGemma 2: {loaded} / {total} MB…', { loaded: i18n.decimal.format(l / 1e6), total: i18n.decimal.format(t / 1e6) }))
      + `<span class="bar"><span style="width:${Math.round((100 * l) / t)}%"></span></span>`, 'info');
  }
}

// ------------------------------------------------------------------------------------------- static UI

function renderStatic() {
  const m = app.manifest;
  $('#tips-link').hidden = !m.tips?.runs?.length;
  $('#voices-link').hidden = !m.voices?.entries;
  const runSel = $('#run');
  runSel.innerHTML = m.runs.map((r) => `<option value="${esc(r.id)}">${esc(r.label)} — ${esc(tr(r.count === 1 ? '{count} topic' : '{count} topics', { count: num.format(r.count) }))}${r.id === m.default_run ? esc(tr(' (default)')) : ''}</option>`).join('');
  runSel.closest('.run-select').hidden = m.runs.length < 2;
  runSel.addEventListener('change', async () => {
    await selectRun(runSel.value);
    renderFilterShell();
    update();
    if (app.state.t) renderDetail(app.state.t);
  });

  const qInput = $('#q');
  let kwTimer = 0;
  qInput.addEventListener('input', () => {
    clearTimeout(kwTimer);
    kwTimer = setTimeout(() => { app.state.q = qInput.value; update(); scheduleSemantic(); }, 120);
  });
  $('#search-form').addEventListener('submit', (e) => { e.preventDefault(); app.state.q = qInput.value; update(); scheduleSemantic(); });
  $$('.modes button').forEach((b) => b.addEventListener('click', () => {
    app.state.mode = b.dataset.mode;
    syncControls();
    if (app.state.mode !== 'kw') startSemantic(); else hideStatus();
    update();
  }));
  if (!app.semantic.available) $$('.modes button:not([data-mode=kw])').forEach((b) => {
    b.title += tr(m.embeddings ? ' — unavailable: incompatible embedding index' : ' — unavailable: no embeddings in this build');
    b.classList.add('unavailable');
  });

  $('#sort').addEventListener('change', (e) => { app.state.sort = e.target.value; update(); });
  $$('.views button').forEach((b) => b.addEventListener('click', () => { app.state.view = b.dataset.view; syncControls(); update({ keepPage: true }); }));
  $('#clear-filters').addEventListener('click', clearFilters);
  $('#open-filters').addEventListener('click', () => document.body.classList.add('show-filters'));
  $('#close-filters').addEventListener('click', () => document.body.classList.remove('show-filters'));

  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); qInput.focus(); qInput.select(); }
    if (e.key === 'Escape' && app.state.t) closeDetail();
  });

  $('#results').addEventListener('click', onResultClick);
  $('#detail').addEventListener('click', onDetailClick);
  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting) && app.shown < app.results.length) { app.shown += PAGE; renderResults(); }
  }, { rootMargin: '600px' }).observe($('#more'));

  renderFilterShell();
  syncControls();
  renderFooter();
}

function syncControls() {
  const s = app.state;
  $('#q').value = s.q;
  $$('.modes button').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.mode === s.mode)));
  $$('.views button').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.view === s.view)));
  $('#run').value = app.run?.id || '';
  const f = $('#filter-body');
  $('[name=from]', f).value = s.from;
  $('[name=to]', f).value = s.to;
  $('[name=votes]', f).value = s.votes || '';
  $('[name=cls]', f).checked = s.cls;
  $('[name=enriched]', f).checked = s.enriched;
  const frCat = app.manifest.feature_request_category;
  $('[name=hidefr]', f).checked = s.xcat.includes(String(frCat));
}

function renderFooter() {
  const m = app.manifest;
  const st = m.stats;
  const emb = m.embeddings;
  $('#footer').innerHTML = `
    <p>${tr('{topics} topics from {forum}', { topics: num.format(st.topics), forum: `<a href="${esc(m.forum_url)}" target="_blank" rel="noopener">discuss.4d.com</a>` })}
      · ${esc(tr('{count} summarised', { count: num.format(st.enriched) }))} · ${esc(tr('{count} classified', { count: num.format(st.classified) }))}
      · ${esc(app.semantic.available ? tr('semantic index: {count} topics ({model}, {quant}, {dim}d)', { count: num.format(emb.count), model: emb.model.split('/').pop(), quant: emb.quant, dim: num.format(emb.dim) }) : tr(emb ? 'semantic index incompatible — rebuild required' : 'no semantic index'))}
      · ${esc(tr('data built {date}', { date: fmtDate(dateToDays(m.generated_at.slice(0, 10))) }))}${st.last_fetch ? esc(tr(', forum fetched {date}', { date: fmtDate(dateToDays(st.last_fetch.slice(0, 10))) })) : ''}</p>
    <p class="muted">${esc(tr('Titles link to the original discussions; posts are not republished. English titles, summaries and classifications are generated by local language models and can be wrong — always check the thread. Everything runs in your browser.'))} <a href="https://github.com/miyako/4d-forum-insights">${esc(tr('Source'))}</a></p>
    <p class="muted">${esc(tr('Semantic and hybrid search use text-only EmbeddingGemma 2: about 207 MB on first use with q4 WebGPU, or 575 MB for the fp16 WASM fallback, cached when supported. Keyword search does not load the model.'))}</p>
    ${i18n.locale !== 'en' ? `<p class="muted">${esc(tr('Translation coverage: {translated} of {total} topics. Missing or stale translations use English.', { translated: num.format(app.topics.filter((topic) => !topic.translationFallback).length), total: num.format(app.topics.length) }))}</p>` : ''}`;
}

// ------------------------------------------------------------------------------------------- filters

function section(title, body, extra = '', open = true) {
  return `<details class="facet" ${open ? 'open' : ''}><summary>${esc(title)}${extra}</summary>${body}</details>`;
}

/** Build the filter panel skeleton (once per run); option lists are refreshed by renderFacets(). */
function renderFilterShell() {
  const m = app.manifest;
  const qs = app.questions.map((q) => section(q.label, `<div class="opts" data-facet="a.${esc(q.id)}"></div>`,
    q.description ? ` <span class="help" title="${esc(q.description)}">?</span>` : ''));
  const frCat = app.manifest.feature_request_category;
  const frName = app.cats.get(frCat)?.name || tr('Feature Request');
  $('#filter-body').innerHTML = `
    ${app.questions.length ? `<div class="group-label">${esc(tr('Model answers'))} <span class="muted">(${esc(app.run.label)})</span></div>` : ''}
    ${qs.join('')}
    <div class="group-label">${esc(tr('Topic'))}</div>
    ${section(tr('Category'), `<label class="opt toggle"><input type="checkbox" name="hidefr"> ${esc(tr('Hide “{name}” category', { name: frName }))}</label><div class="opts" data-facet="cat"></div>`)}
    ${section(tr('Language'), '<div class="opts" data-facet="lang"></div>')}
    ${section(tr('Tags'), `<input type="search" class="tag-filter" placeholder="${esc(tr('Find a tag…'))}" aria-label="${esc(tr('Find a tag'))}"><div class="opts" data-facet="tag"></div>`, '', false)}
    ${section(tr('Created'), `<div class="dates"><label>${esc(tr('From'))} <input type="date" name="from"></label><label>${esc(tr('To'))} <input type="date" name="to"></label></div>
      <div class="quick">${[1, 2, 5].map((y) => `<button type="button" class="link" data-years="${y}">${esc(tr(y === 1 ? 'last {count} yr' : 'last {count} yrs', { count: num.format(y) }))}</button>`).join(' ')}</div>`, '', false)}
    ${section(tr('Votes'), `<label class="opt">${esc(tr('At least'))} <input type="number" min="0" name="votes" class="num"> ${esc(tr('votes'))}</label>`, '', false)}
    ${section(tr('Status'), `<label class="opt"><input type="checkbox" name="cls"> ${esc(tr('Classified by the model run'))}</label>
      <label class="opt"><input type="checkbox" name="enriched"> ${esc(tr('Has an English summary'))}</label>`, '', false)}`;
  const f = $('#filter-body');
  f.onchange = onFilterChange;
  f.oninput = (e) => {
    if (e.target.classList.contains('tag-filter')) renderTagOptions();
  };
  f.onclick = (e) => {
    const y = e.target.dataset?.years;
    if (y) {
      const d = new Date();
      d.setUTCFullYear(d.getUTCFullYear() - Number(y));
      app.state.from = d.toISOString().slice(0, 10);
      app.state.to = '';
      syncControls();
      update();
    }
  };
  if (app.state) syncControls();
}

function onFilterChange(e) {
  const el = e.target;
  const s = app.state;
  const facet = el.closest('[data-facet]')?.dataset.facet;
  if (facet) {
    if (facet.startsWith('a.') && el.type === 'radio') {
      s.ans[facet.slice(2)] = el.value || undefined;
      if (!el.value) delete s.ans[facet.slice(2)];
    } else {
      const list = facet.startsWith('a.') ? (s.ans[facet.slice(2)] ||= []) : s[facet];
      const i = list.indexOf(el.value);
      if (el.checked && i < 0) list.push(el.value);
      if (!el.checked && i >= 0) list.splice(i, 1);
    }
  } else if (el.name === 'hidefr') {
    const fr = String(app.manifest.feature_request_category);
    s.xcat = s.xcat.filter((x) => x !== fr);
    if (el.checked) s.xcat.push(fr);
  } else if (el.name === 'from' || el.name === 'to') s[el.name] = el.value;
  else if (el.name === 'votes') s.votes = Math.max(0, Number(el.value) || 0);
  else if (el.name === 'cls' || el.name === 'enriched') s[el.name] = el.checked;
  else return;
  update();
}

function clearFilters() {
  const s = app.state;
  Object.assign(s, { cat: [], xcat: [], lang: [], tag: [], from: '', to: '', votes: 0, cls: false, enriched: false, ans: {}, scope: 'all' });
  syncControls();
  update();
}

const LEVEL_CLASSES = (q, key) => {
  const i = q.levels.findIndex((l) => l.key === key);
  return `lv lv-${Math.round((4 * i) / Math.max(1, q.levels.length - 1))}`;
};
// bands are listed high → low
const BAND_CLASSES = (q, key) => {
  const i = q.bands.findIndex((b) => b.key === key);
  return `lv lv-${4 - Math.round((4 * i) / Math.max(1, q.bands.length - 1))}`;
};
const bandOfAnswer = (q, a) => (q.bands && a?.b ? q.bands.find((b) => b.key === a.b) : null);
const scoreClass = (q, a) => (bandOfAnswer(q, a) ? BAND_CLASSES(q, a.b) : LEVEL_CLASSES(q, a.lv));
const scoreTitle = (q, a) => {
  const l = q.levels.find((x) => x.key === a.lv);
  const b = bandOfAnswer(q, a);
  return tr(b ? '{question}: {band} band ({description}); score {score}, nearest level "{level}"' : '{question}: score {score}, nearest level "{level}"',
    { question: q.label, band: b?.label, description: b?.long, score: score(a.s), level: l?.long || a.lv });
};

function optRow(facet, value, label, count, checked, type = 'checkbox', extra = '') {
  return `<label class="opt${count ? '' : ' zero'}"><input type="${type}" name="${esc(facet)}" value="${esc(value)}" ${checked ? 'checked' : ''}>`
    + `${extra}<span class="opt-label">${label}</span><span class="n">${count == null ? '' : num.format(count)}</span></label>`;
}

let lastFacetArgs = null;
function renderFacets(base, preds) {
  lastFacetArgs = { base, preds };
  const s = app.state;
  for (const q of app.questions) {
    const box = $(`[data-facet="a.${CSS.escape(q.id)}"]`);
    if (!box) continue;
    const sel = s.ans[q.id];
    if (q.type === 'score' && q.bands?.length && app.run?.bands?.[q.id]) {
      const c = facetCounts(base, preds, 'a.' + q.id, (t) => t.ans?.[q.id]?.b);
      const cuts = app.run.bands[q.id].cuts;
      box.innerHTML = q.bands.map((b, i) => {
        const cutoff = cuts[i] == null ? '' : ` (${tr('score ≥ {score}', { score: score(cuts[i]) })})`;
        return optRow('a.' + q.id, b.key, esc(b.label), c.get(b.key) || 0,
          sel?.includes(b.key), 'checkbox', `<i class="${BAND_CLASSES(q, b.key)}" title="${esc(b.long)}"></i>`)
          .replace('<span class="opt-label">', `<span class="opt-label" title="${esc(b.long + cutoff)}">`);
      }).join('');
    } else if (q.type === 'score') {
      const c = facetCounts(base, preds, 'a.' + q.id, (t) => t.ans?.[q.id]?.lv);
      box.innerHTML = [...q.levels].reverse().map((l) => optRow('a.' + q.id, l.key, esc(l.label), c.get(l.key) || 0,
        sel?.includes(l.key), 'checkbox', `<i class="${LEVEL_CLASSES(q, l.key)}"></i>`)).join('');
    } else if (q.type === 'choice') {
      const c = facetCounts(base, preds, 'a.' + q.id, (t) => t.ans?.[q.id]?.c);
      box.innerHTML = q.choices.map((ch) => optRow('a.' + q.id, ch.key, esc(ch.label), c.get(ch.key) || 0, sel?.includes(ch.key))).join('');
    } else {
      const set = applyFilters(base, preds, 'a.' + q.id);
      const counts = NOUL_FILTERS.map((f) => set.filter((t) => t.ans?.[q.id] && f.test(t.ans[q.id].p)).length);
      box.innerHTML = optRow('a.' + q.id, '', esc(tr('Any')), null, !sel, 'radio')
        + NOUL_FILTERS.map((f, i) => optRow('a.' + q.id, f.key, esc(noulLabel(f.key)), counts[i], sel === f.key, 'radio')).join('');
    }
  }
  const cc = facetCounts(base, preds, 'cat', (t) => t.cat);
  $('[data-facet=cat]').innerHTML = app.manifest.categories.map((c) => optRow('cat', String(c.id), esc(c.name), cc.get(c.id) || 0, s.cat.includes(String(c.id)))).join('');
  const lc = facetCounts(base, preds, 'lang', (t) => t.lang);
  $('[data-facet=lang]').innerHTML = app.manifest.languages.map((l) => optRow('lang', l.code, esc(l.name), lc.get(l.code) || 0, s.lang.includes(l.code))).join('');
  app.tagCounts = facetCounts(base, preds, 'tag', (t) => t.tags);
  renderTagOptions();
}

function renderTagOptions() {
  const s = app.state;
  const needle = normalize($('.tag-filter')?.value || '');
  const counts = app.tagCounts || new Map();
  let tags = needle ? app.manifest.tags.filter((t) => normalize(t).includes(needle)) : [...counts.keys()].sort((a, b) => counts.get(b) - counts.get(a));
  tags = [...new Set([...s.tag, ...tags.slice(0, needle ? 40 : 15)])];
  $('[data-facet=tag]').innerHTML = tags.map((t) => optRow('tag', t, esc(t), counts.get(t) || 0, s.tag.includes(t))).join('')
    || `<div class="muted small">${esc(tr('No tags'))}</div>`;
}

function renderSort(sort) {
  const opts = sortOptions(app.questions, !!app.state.q.trim());
  const groups = {};
  for (const o of opts) (groups[o.group || ''] ||= []).push(o);
  $('#sort').innerHTML = Object.entries(groups).map(([g, list]) => {
    const html = list.map((o) => {
      const [qid, choice] = o.key.slice(2).split(':');
      const q = o.key.startsWith('a.') && app.questions.find((item) => item.id === qid);
      const label = q ? tr(q.type === 'score' ? '{label} score' : q.type === 'noul' ? '{label} (p)' : '{label}: P({choice})',
        { label: q.type === 'choice' ? q.short : q.label, choice: q.choices?.find((item) => item.key === choice)?.label || choice }) : tr(o.label);
      return `<option value="${esc(o.key)}" ${o.key === sort ? 'selected' : ''}>${esc(label)}</option>`;
    }).join('');
    return g ? `<optgroup label="${esc(tr(g))}">${html}</optgroup>` : html;
  }).join('');
}

function renderActive() {
  const s = app.state;
  const chips = [];
  const chip = (label, fn) => chips.push({ label, fn });
  for (const c of s.cat) chip(tr('Category: {name}', { name: app.cats.get(Number(c))?.name || c }), () => s.cat.splice(s.cat.indexOf(c), 1));
  for (const c of s.xcat) chip(tr('Not: {name}', { name: app.cats.get(Number(c))?.name || c }), () => s.xcat.splice(s.xcat.indexOf(c), 1));
  for (const l of s.lang) chip(tr('Language: {name}', { name: app.manifest.languages.find((x) => x.code === l)?.name || l }), () => s.lang.splice(s.lang.indexOf(l), 1));
  for (const t of s.tag) chip(tr('Tag: {name}', { name: t }), () => s.tag.splice(s.tag.indexOf(t), 1));
  if (s.from) chip(tr('From {date}', { date: stateDate(s.from) }), () => { s.from = ''; });
  if (s.to) chip(tr('To {date}', { date: stateDate(s.to) }), () => { s.to = ''; });
  if (s.votes) chip(tr('≥ {count} votes', { count: num.format(s.votes) }), () => { s.votes = 0; });
  if (s.cls) chip(tr('Classified'), () => { s.cls = false; });
  if (s.enriched) chip(tr('Summarised'), () => { s.enriched = false; });
  for (const q of app.questions) {
    const v = s.ans[q.id];
    if (!v || (Array.isArray(v) && !v.length)) continue;
    const label = q.type === 'score' ? v.map((k) => q.bands?.find((b) => b.key === k)?.label || q.levels.find((l) => l.key === k)?.label || k).join(' / ')
      : q.type === 'choice' ? v.map((k) => q.choices.find((c) => c.key === k)?.label || k).join(' / ')
        : noulLabel(v);
    chip(`${q.short}: ${label}`, () => { delete s.ans[q.id]; });
  }
  const box = $('#active-filters');
  box.innerHTML = chips.map((c, i) => `<button type="button" class="chip removable" data-i="${i}">${esc(c.label)} <span aria-hidden="true">×</span></button>`).join('')
    + (chips.length > 1 ? `<button type="button" class="link" data-clear>${esc(tr('Clear all'))}</button>` : '');
  box.onclick = (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.clear != null) return clearFilters();
    chips[Number(b.dataset.i)].fn();
    syncControls();
    update();
  };
}

function renderPresets() {
  const m = app.manifest;
  const s = app.state;
  const current = new URLSearchParams(serializeState({ ...s, q: '', mode: 'kw', run: '', view: 'list', t: null, scope: '' }));
  const same = (params) => {
    const p = new URLSearchParams(params);
    p.delete('scope');
    return [...p].length === [...current].length && [...p].every(([k, v]) => current.get(k) === v);
  };
  $('#presets').innerHTML = `<span class="muted">${esc(tr('Views:'))}</span>` + m.presets.map((p) => `<button type="button" class="preset${same(p.params) ? ' active' : ''}" data-params="${esc(p.params)}" title="${esc(p.description || '')}">${esc(p.label)}</button>`).join('');
  $('#presets').onclick = (e) => {
    const b = e.target.closest('.preset');
    if (!b) return;
    const keep = { q: s.q, mode: s.mode, run: s.run, view: s.view };
    app.state = { ...parseState(b.dataset.params, m.questions), ...keep, scope: 'all' };
    syncControls();
    update();
  };
}

// ------------------------------------------------------------------------------------------- results

function highlight(text) {
  if (!text) return '';
  const terms = app.terms.filter((t) => t && t.length > 1);
  if (!terms.length) return esc(text);
  return text.split(/([\p{L}\p{N}_]+)/u).map((part, i) => {
    if (i % 2 === 0) return esc(part);
    const n = normalize(part);
    return terms.some((t) => n.startsWith(t) || (t.length > 3 && n.includes(t))) ? `<mark>${esc(part)}</mark>` : esc(part);
  }).join('');
}

const urlFor = (state) => {
  const keys = [...STATE_KEYS, ...app.manifest.questions.map((q) => 'a.' + q.id),
    ...[...new URLSearchParams(location.search).keys()].filter((key) => key.startsWith('a.'))];
  const qs = stateURL(location.search, serializeState(state), keys, i18n.locale);
  return location.pathname + (qs ? `?${qs}` : '') + location.hash;
};
const topicUrl = (t) => `${app.manifest.forum_url}/t/${encodeURIComponent(t.slug)}/${t.id}`;
const detailHref = (t) => urlFor({ ...app.state, t: t.id });

function refreshLocaleLinks() {
  for (const link of $$('#locale-nav a')) link.href = localeURL(location.href, link.hreflang);
  for (const link of $$('a[data-locale-href]')) {
    link.href = navigationURL(link.dataset.localeHref, i18n.locale, location.href);
  }
}

const fallbackBadge = (topic) => topic.translationFallback
  ? `<span class="chip muted-chip translation-fallback">${esc(tr('English fallback'))}</span>` : '';
const originalTitleLabel = (topic) => !topic.title_en
  ? `<span class="muted small">${esc(tr('Original title'))}</span> ` : '';

function answerChips(t, { all = false } = {}) {
  if (!t.ans) return app.run ? `<span class="chip muted-chip" title="${esc(tr('Not classified by this model run yet'))}">${esc(tr('not classified'))}</span>` : '';
  return app.questions.map((q) => {
    const a = t.ans[q.id];
    if (!a) return '';
    if (q.type === 'score') {
      const label = bandOfAnswer(q, a)?.label || q.levels.find((x) => x.key === a.lv)?.label;
      return `<span class="chip ${scoreClass(q, a)}" title="${esc(scoreTitle(q, a))}">${esc(q.short)} · ${esc(label)}</span>`;
    }
    if (q.type === 'choice') {
      const i = q.choices.findIndex((c) => c.key === a.c);
      return `<span class="chip kind" title="${esc(q.label)} (${pct(a.p[i] || 0)})">${esc(q.choices[i]?.label || a.c)}</span>`;
    }
    if (!all && a.p < 0.5) return '';
    return `<span class="chip noul" style="--p:${a.p}" title="${esc(q.label)}: ${pct(a.p)}">${esc(q.short)} ${pct(a.p)}</span>`;
  }).join('');
}

function metaLine(t) {
  const cat = app.cats.get(t.cat);
  const parts = [
    cat ? `<span class="cat${t.cat === app.manifest.feature_request_category ? ' fr' : ''}">${esc(cat.name)}</span>` : '',
    `<span title="${esc(tr(t.flags & app.manifest.flags.lang_guessed ? 'Language (guessed)' : 'Language'))}">${esc(t.lang.toUpperCase())}</span>`,
    `<span title="${esc(tr('Created'))}">${esc(fmtDate(t.created))}</span>`,
    `<span title="${esc(tr('Replies'))}">💬 ${num.format(Math.max(0, t.posts - 1))}</span>`,
    t.likes ? `<span title="${esc(tr('Likes'))}">♥ ${compact.format(t.likes)}</span>` : '',
    t.votes ? `<span title="${esc(tr('Votes'))}">▲ ${num.format(t.votes)}</span>` : '',
    `<span title="${esc(tr('Views'))}">👁 ${compact.format(t.views)}</span>`,
    t.flags & app.manifest.flags.accepted ? `<span class="solved" title="${esc(tr('Has an accepted answer'))}">${esc(tr('✓ solved'))}</span>` : '',
    app.sim?.has(t.id) ? `<span class="sim" title="${esc(tr('Cosine similarity to the query'))}">≈ ${score(app.sim.get(t.id))}</span>` : '',
  ];
  return parts.filter(Boolean).join('<span class="dot">·</span>');
}

function renderResults() {
  const list = app.results.slice(0, app.shown);
  const box = $('#results');
  if (!app.results.length) {
    box.innerHTML = `<div class="empty">${esc(tr('No topics match.'))}${hasFilters(app.state) ? esc(tr(' Try removing a filter.')) : ''}${app.state.mode === 'kw' && app.state.q && app.semantic.available ? `${esc(tr(' Or try'))} <button type="button" class="link" data-mode-switch="hyb">${esc(tr('hybrid search'))}</button>.` : ''}</div>`;
  } else if (app.state.view === 'table') {
    box.innerHTML = renderTable(list);
  } else {
    box.innerHTML = list.map((t) => {
      const title = t.title_en || t.title;
      return `<article class="card${t.id === app.state.t ? ' selected' : ''}" data-id="${t.id}">
        <div class="card-main">
          ${originalTitleLabel(t)}
          <a class="title" lang="${esc(t.title_en ? t.contentLocale || 'en' : t.lang)}" href="${esc(detailHref(t))}" data-open="${t.id}">${highlight(title)}</a>
          ${t.title_en ? `<div class="orig" lang="${esc(t.lang)}">${highlight(t.title)}</div>` : ''}
          ${t.line ? `<p class="line" lang="${esc(t.contentLocale || 'en')}">${highlight(t.line)}</p>` : ''}
          ${fallbackBadge(t)}
          <div class="meta">${metaLine(t)}</div>
        </div>
        <div class="card-side">${answerChips(t)}</div>
      </article>`;
    }).join('');
  }
  $('#more').innerHTML = app.shown < app.results.length
    ? `<button type="button" class="btn" id="more-btn">${esc(tr('Show more ({count} left)', { count: num.format(app.results.length - app.shown) }))}</button>` : '';
  $('#more-btn')?.addEventListener('click', () => { app.shown += PAGE; renderResults(); });
}

function renderTable(list) {
  const sort = currentSort();
  const th = (key, label, cls = '') => `<th class="${cls}${key && sort === key ? ' sorted' : ''}" ${key ? `data-sort="${esc(key)}"` : ''}>${esc(label)}</th>`;
  const qCols = app.questions.map((q) => th(q.type === 'choice' ? '' : 'a.' + q.id, q.short, 'q'));
  const rows = list.map((t) => {
    const cells = app.questions.map((q) => {
      const a = t.ans?.[q.id];
      if (!a) return '<td class="q muted">–</td>';
      if (q.type === 'score') return `<td class="q"><span class="chip ${scoreClass(q, a)}" title="${esc(scoreTitle(q, a))}">${score(a.s)}</span></td>`;
      if (q.type === 'choice') return `<td class="q">${esc(q.choices.find((c) => c.key === a.c)?.label || '')}</td>`;
      return `<td class="q num"><span class="pbar" style="--p:${a.p}"></span>${pct(a.p)}</td>`;
    }).join('');
    return `<tr data-id="${t.id}" class="${t.id === app.state.t ? 'selected' : ''}">
      <td class="t">${originalTitleLabel(t)}<a lang="${esc(t.title_en ? t.contentLocale || 'en' : t.lang)}" href="${esc(detailHref(t))}" data-open="${t.id}">${highlight(t.title_en || t.title)}</a>${t.line ? `<div class="line small" lang="${esc(t.contentLocale || 'en')}">${highlight(t.line)}</div>` : ''}${fallbackBadge(t)}</td>
      <td>${esc(app.cats.get(t.cat)?.name || '')}</td><td>${esc(t.lang)}</td><td class="nowrap">${esc(fmtDate(t.created))}</td>
      <td class="num">${num.format(Math.max(0, t.posts - 1))}</td><td class="num">${t.votes ? num.format(t.votes) : ''}</td>${cells}</tr>`;
  }).join('');
  return `<div class="table-wrap"><table class="grid"><thead><tr>${th('', tr('Topic'))}${th('', tr('Category'))}${th('', tr('Lang'))}${th('new', tr('Created'))}${th('posts', tr('Replies'), 'num')}${th('votes', tr('Votes'), 'num')}${qCols.join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

function onResultClick(e) {
  const sw = e.target.closest('[data-mode-switch]');
  if (sw) { app.state.mode = sw.dataset.modeSwitch; syncControls(); startSemantic(); update(); return; }
  const th = e.target.closest('th[data-sort]');
  if (th) { app.state.sort = th.dataset.sort; update(); return; }
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return; // let "open in new tab" work
  const row = e.target.closest('[data-id]');
  if (!row || (e.target.closest('a') && !e.target.closest('[data-open]'))) return;
  if (getSelection().toString()) return;
  e.preventDefault();
  openDetail(Number(row.dataset.id));
}

// ------------------------------------------------------------------------------------------- detail

function openDetail(id, { push = true } = {}) {
  if (!app.byId.has(id)) return;
  app.state.t = id;
  if (push) window.history.pushState(null, '', urlFor(app.state));
  refreshLocaleLinks();
  $('#detail').hidden = false;
  document.body.classList.add('has-detail');
  $$('.selected', $('#results')).forEach((el) => el.classList.remove('selected'));
  $(`#results [data-id="${id}"]`)?.classList.add('selected');
  renderDetail(id);
  $('#detail').scrollTop = 0;
}

function closeDetail({ push = true } = {}) {
  app.state.t = null;
  if (push) window.history.pushState(null, '', urlFor(app.state));
  refreshLocaleLinks();
  $('#detail').hidden = true;
  document.body.classList.remove('has-detail');
  $$('.selected', $('#results')).forEach((el) => el.classList.remove('selected'));
}

function probRows(labels, probs, chosen) {
  return labels.map((l, i) => `<div class="prob${i === chosen ? ' chosen' : ''}"><span class="pl" title="${esc(l.long || '')}">${esc(l.label)}</span>`
    + `<span class="pb"><span style="width:${(probs[i] * 100).toFixed(1)}%"></span></span><span class="pv">${pct(probs[i] || 0)}</span></div>`).join('');
}

function answerBlock(q, a, run) {
  if (!a) return '';
  let body = '';
  if (q.type === 'score') {
    const chosen = q.levels.findIndex((l) => l.key === a.lv);
    const max = q.levels[q.levels.length - 1].value;
    const b = bandOfAnswer(q, a);
    const cuts = run?.bands?.[q.id]?.cuts || [];
    const bi = b ? q.bands.indexOf(b) : -1;
    const range = bi < 0 || !cuts.length ? '' : tr(bi < cuts.length ? 'score ≥ {score}' : 'score < {score}', { score: score(bi < cuts.length ? cuts[bi] : cuts[cuts.length - 1]) });
    body = `<div class="score-line">${esc(tr('Expected level'))} <strong>${score(a.s)}</strong> / ${num.format(max)} → <span class="chip ${LEVEL_CLASSES(q, a.lv)}">${esc(q.levels[chosen].label)}</span>`
      + (b ? ` · ${esc(tr('band'))} <span class="chip ${BAND_CLASSES(q, a.b)}" title="${esc(b.long)}">${esc(b.label)}</span> <span class="muted small">${esc(range)}: ${esc(b.long)}</span>` : '')
      + '</div>'
      + probRows([...q.levels].reverse(), [...a.p].reverse(), q.levels.length - 1 - chosen);
  } else if (q.type === 'choice') {
    body = probRows(q.choices, a.p, q.choices.findIndex((c) => c.key === a.c));
  } else {
    body = probRows([{ label: tr('Yes') }], [a.p], a.p >= 0.5 ? 0 : -1);
  }
  return `<div class="answer"><div class="q-title">${esc(q.label)}${q.description ? `<span class="q-desc">${esc(q.description)}</span>` : ''}</div>${body}</div>`;
}

async function renderDetail(id) {
  const t = app.byId.get(id);
  const m = app.manifest;
  const cat = app.cats.get(t.cat);
  const tags = t.tags.map((x) => `<button type="button" class="chip tag" data-tag="${esc(x)}">${esc(x)}</button>`).join('');
  const para = t.para === undefined && !app.textLoaded ? `<p class="muted">${esc(tr('Loading summary…'))}</p>`
    : t.para ? `<p lang="${esc(t.contentLocale || 'en')}">${esc(t.para)}</p>` : '';
  const summary = t.line || t.para
    ? `<p class="lead" lang="${esc(t.contentLocale || 'en')}">${esc(t.line || '')}</p>${para}`
    : `<p class="muted">${esc(tr('No English summary yet.'))}${t.flags & m.flags.enriched ? '' : ` ${esc(tr('This topic has not been processed by the summariser.'))}`}</p>`;
  const source = t.canonicalFields;
  const original = `<section><h3>${esc(tr('Original title'))}</h3><p lang="${esc(t.lang)}">${esc(t.title)}</p></section>`;
  const english = source ? `<details><summary>${esc(tr('English generated source'))}</summary>
    ${source.title_en ? `<h4>${esc(tr('English generated title'))}</h4><p lang="en">${esc(source.title_en)}</p>` : ''}
    ${source.line ? `<p lang="en">${esc(source.line)}</p>` : ''}
    ${source.para ? `<p lang="en">${esc(source.para)}</p>` : ''}</details>` : '';
  const embeddingSize = m.embeddings ? tr('{size} MB', { size: i18n.decimal.format(m.embeddings.bytes / 1e6) }) : '';
  const runs = [app.run, ...m.runs.filter((r) => r.id !== app.run?.id)].filter(Boolean);
  $('#detail-body').innerHTML = `
    <div class="detail-head">
      <button type="button" class="icon" data-close title="${esc(tr('Close (Esc)'))}" aria-label="${esc(tr('Close'))}">✕</button>
      <span class="muted small">#${t.id}</span> ${fallbackBadge(t)}
    </div>
    ${originalTitleLabel(t)}<h2 lang="${esc(t.title_en ? t.contentLocale || 'en' : t.lang)}">${esc(t.title_en || t.title)}</h2>
    <p class="actions">
      <a class="btn primary" href="${esc(topicUrl(t))}" target="_blank" rel="noopener">${esc(tr('Open on discuss.4d.com ↗'))}</a>
      <button type="button" class="btn" data-copy>${esc(tr('Copy link'))}</button>
    </p>
    <dl class="facts">
      <dt>${esc(tr('Category'))}</dt><dd>${esc(cat?.name || t.cat)}</dd>
      <dt>${esc(tr('Source language'))}</dt><dd>${esc(m.languages.find((l) => l.code === t.lang)?.name || t.lang)}${t.flags & m.flags.lang_guessed ? ` <span class="muted">${esc(tr('(guessed)'))}</span>` : ''}</dd>
      <dt>${esc(tr('Created'))}</dt><dd>${esc(fmtDate(t.created))}</dd>
      <dt>${esc(tr('Last activity'))}</dt><dd>${esc(fmtDate(t.active))}</dd>
      <dt>${esc(tr('Replies'))}</dt><dd>${num.format(Math.max(0, t.posts - 1))}</dd>
      <dt>${esc(tr('Views · Likes · Votes'))}</dt><dd>${num.format(t.views)} · ${num.format(t.likes)} · ${num.format(t.votes)}</dd>
      <dt>${esc(tr('Status'))}</dt><dd>${esc([t.flags & m.flags.accepted ? tr('solved') : '', t.flags & m.flags.closed ? tr('closed') : '', t.flags & m.flags.archived ? tr('archived') : ''].filter(Boolean).join(', ') || tr('open'))}</dd>
    </dl>
    ${tags ? `<div class="tags">${tags}</div>` : ''}
    <section><h3>${esc(tr('Summary'))} <span class="muted small">${esc(tr('(machine-generated)'))}</span></h3>${summary}</section>
    ${original}${english}
    <section><h3>${esc(tr('Model answers'))}</h3><div id="runs-detail">${runs.map((r) => `<details class="run" data-run="${esc(r.id)}" ${r.id === app.run?.id ? 'open' : ''}><summary>${esc(r.label)}${r.id === app.run?.id && runs.length > 1 ? ` <span class="muted">${esc(tr('(selected)'))}</span>` : ''}</summary><div class="run-body muted small">${esc(tr('Loading…'))}</div></details>`).join('') || `<p class="muted">${esc(tr('No model runs in this build.'))}</p>`}</div></section>
    ${app.semantic.available ? `<section><h3>${esc(tr('Similar topics'))}</h3><div id="similar">${app.semantic.vectors ? `<p class="muted small">${esc(tr('Loading…'))}</p>` : `<button type="button" class="btn" data-similar>${esc(tr('Find similar topics'))} <span class="muted">(${esc(embeddingSize)})</span></button>`}</div></section>` : ''}`;
  for (const r of runs) {
    loadRun(m, r.id).then((answers) => {
      if (app.state.t !== id) return;
      const a = answers.get(id);
      const box = $(`[data-run="${CSS.escape(r.id)}"] .run-body`);
      if (!box) return;
      box.classList.remove('muted', 'small');
      const qs = m.questions.filter((q) => r.questions.includes(q.id));
      box.innerHTML = a ? qs.map((q) => answerBlock(q, a[q.id], r)).join('') : `<p class="muted small">${esc(tr('Not classified by this run.'))}</p>`;
      const sum = $(`[data-run="${CSS.escape(r.id)}"] summary`);
      if (sum && !a) sum.classList.add('muted');
    }).catch((e) => {
      if (app.state.t !== id) return;
      const box = $(`[data-run="${CSS.escape(r.id)}"] .run-body`);
      if (box) box.innerHTML = `<p>${esc(tr('Could not load model answers: {error}', { error: e.message }))}</p>`;
    });
  }
  if (app.semantic.vectors) renderSimilar(id);
}

async function renderSimilar(id) {
  const box = $('#similar');
  if (!box) return;
  try {
    const sims = await app.semantic.similar(id, 8);
    if (app.state.t !== id) return;
    box.innerHTML = sims.length ? `<ul class="similar">${sims.map((s) => {
      const t = app.byId.get(s.id);
      return t ? `<li>${originalTitleLabel(t)}<a lang="${esc(t.title_en ? t.contentLocale || 'en' : t.lang)}" href="${esc(detailHref(t))}" data-open-detail="${t.id}">${esc(t.title_en || t.title)}</a> <span class="muted small">≈ ${score(s.score)} · ${esc(app.cats.get(t.cat)?.name || '')}</span> ${fallbackBadge(t)} ${answerChips(t)}</li>` : '';
    }).join('')}</ul>` : `<p class="muted small">${esc(tr('This topic is not in the semantic index yet.'))}</p>`;
  } catch (e) {
    box.innerHTML = `<p class="muted small">${esc(tr('Could not load embeddings: {error}', { error: e.message }))}</p>`;
  }
}

function onDetailClick(e) {
  if (e.target.closest('[data-close]')) return closeDetail();
  if (e.target.closest('[data-copy]')) {
    navigator.clipboard?.writeText(location.href);
    e.target.closest('[data-copy]').textContent = tr('Copied');
    return;
  }
  if (e.target.closest('[data-similar]')) { $('#similar').innerHTML = `<p class="muted small">${esc(tr('Loading…'))}</p>`; renderSimilar(app.state.t); return; }
  const tag = e.target.closest('[data-tag]');
  if (tag) { app.state.tag = [tag.dataset.tag]; syncControls(); update(); return; }
  const link = e.target.closest('[data-open-detail]');
  if (link && !(e.metaKey || e.ctrlKey)) { e.preventDefault(); openDetail(Number(link.dataset.openDetail)); }
}

// ------------------------------------------------------------------------------------------- status

function showStatus(html, kind = 'info') {
  const el = $('#status');
  el.className = `status ${kind}`;
  el.innerHTML = html;
  el.hidden = false;
}
function hideStatus() { $('#status').hidden = true; }

init().catch((e) => {
  $('#count').textContent = '';
  showStatus(esc(tr('Could not load requests: {error}', { error: e.message })), 'error');
});
