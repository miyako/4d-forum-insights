// /voices/ (Forum Voices): discussions where the community weighed in. Threads are rated by the "voices-…" model
// run and summarised anonymously (question, main views, outcome) into voices/entries.json; see manifest.voices.
import { loadManifest, loadTopics, dataUrl } from './data.js';
import { KeywordIndex } from './search.js';
import { Semantic } from './semantic.js';
import { rrf } from './filter.js';
import { createLocale, localeOf, localeURL, stateURL, loadOverlay, installLocale, localizeDOM, modelDisclosure } from './locale.js';
import {
  parseVoicesState, serializeVoicesState, buildVoices, debateLevel, voicesPredicates, applyVoices, sortVoices, SORTS,
} from './voices-core.js';

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const i18n = createLocale(localeOf(location.search));
const { t: tr, number: num, compact, pct, date: fmtDate } = i18n;
installLocale(i18n, 'voices');
const STATE_KEYS = ['q', 'mode', 'subject', 'settled', 'since', 'sort', 'all'];
const PAGE = 25;
const SEM_CANDIDATES = 3000;

const app = {
  manifest: null, voices: null, items: [], byId: new Map(), index: null, semantic: null, state: null,
  q: {}, shown: PAGE, terms: [],
  sem: { q: null, list: null, busy: false, error: null },
};

async function getJSON(rel) {
  const res = await fetch(dataUrl(rel));
  if (!res.ok) throw new Error(`${rel}: HTTP ${res.status}`);
  return res.json();
}

const choice = (qid, key) => {
  const c = app.q[qid]?.choices.find((c) => c.key === key) || { key, label: key, long: '' };
  return { ...c, label: tr(c.label), long: tr(c.long) };
};

// ------------------------------------------------------------------------------------------- boot

async function init() {
  try {
    app.manifest = await loadManifest();
  } catch (e) {
    return fail(esc(tr('Could not load data/manifest.json ({error}).', { error: e.message })));
  }
  const m = app.manifest;
  app.voices = m.voices;
  if (!app.voices?.entries) return fail(`${tr('No Forum Voices data yet: run')} <code>fminer voices-classify</code>, <code>fminer voices-write</code>, <code>make data</code>.`);
  document.title = tr(app.voices.title);
  app.state = parseVoicesState(location.search);
  app.q = Object.fromEntries(app.voices.questions.map((q) => [q.id, q]));

  const [topics, entries] = await Promise.all([loadTopics(m), getJSON(app.voices.files.entries)]);
  app.items = await loadOverlay(buildVoices(entries, topics, app.voices.questions), 'voices', i18n.locale, getJSON);
  app.items.forEach((it) => app.byId.set(it.id, it));

  app.index = new KeywordIndex(app.items.map((it) => ({
    id: it.id, title: it.question, title_en: [it.canonicalFields?.question, it.topic.title_en, it.topic.title].filter(Boolean).join(' '),
    line: [...it.positions, it.outcome, ...(it.canonicalFields?.positions || []), it.canonicalFields?.outcome].filter(Boolean).join(' '), tags: it.topic.tags,
  })));
  app.semantic = new Semantic(m.embeddings);
  app.semantic.onProgress = onModelProgress;

  renderStatic();
  syncControls();
  update({ replace: true });
  if (app.state.mode !== 'kw') startSemantic();

  window.addEventListener('popstate', () => {
    if (localeOf(location.search) !== i18n.locale) return location.reload();
    app.state = parseVoicesState(location.search);
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
  // semantic scores cover every topic; keep the Voices threads and only reasonably similar ones
  const sem = app.sem.list.filter((r) => app.byId.has(r.id)).slice(0, 60);
  if (s.mode === 'sem') {
    return { base: sem.map((r) => app.byId.get(r.id)), relevance: new Map(sem.map((r) => [r.id, r.score])), note: 'semantic: most similar discussions' };
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
    app.sem = { ...app.sem, q, list, error: null };
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
    showStatus(esc(tr('Loading the semantic model (first time only): {loaded} / {total} MB…', { loaded: i18n.decimal.format(l / 1e6), total: i18n.decimal.format(t / 1e6) }))
      + `<span class="bar"><span style="width:${Math.round((100 * l) / t)}%"></span></span>`, 'info');
  }
}

// ------------------------------------------------------------------------------------------- update

function update({ replace = false, history = true, keepPage = false } = {}) {
  const s = app.state;
  if (history) {
    const qs = stateURL(location.search, serializeVoicesState(s), STATE_KEYS, i18n.locale);
    const url = location.pathname + (qs ? '?' + qs : '') + location.hash;
    if (url !== location.pathname + location.search + location.hash) (replace ? window.history.replaceState : window.history.pushState).call(window.history, null, '', url);
  }
  if (!keepPage) app.shown = PAGE;
  const { base, relevance, note } = candidates();
  const preds = voicesPredicates(s);
  const sort = s.sort && (s.sort !== 'relevance' || relevance) ? s.sort : (relevance ? 'relevance' : 'lively');
  app.results = sortVoices(applyVoices(base, preds), sort, relevance);
  renderSubjects(base, preds);
  renderSettled(base, preds);
  renderSort(!!relevance, sort);
  renderResults(note);
  installLocale(i18n, 'voices');
}

// ------------------------------------------------------------------------------------------- render

function renderStatic() {
  const m = app.manifest;
  $('#semantic-disclosure').textContent = modelDisclosure(m.embeddings, i18n);
  $('#semantic-disclosure').hidden = !app.semantic.available;
  const v = app.voices;
  const years = app.items.map((it) => it.topic.created).filter((d) => d != null).map((d) => new Date(d * 864e5).getUTCFullYear());
  const lo = Math.min(...years), hi = Math.max(...years);
  $('#since').innerHTML = `<option value="0">${tr('Any year')}</option>`
    + Array.from({ length: hi - lo + 1 }, (_, i) => hi - i).map((y) => `<option value="${y}">${y}+</option>`).join('');
  $('#presets').innerHTML = `<span class="muted">${tr('Try:')}</span> ` + (v.presets || []).map((p) =>
    `<a class="preset" href="${esc(localeURL('?' + p.params, i18n.locale, location.href))}" data-params="${esc(p.params)}">${esc(tr(p.label))}</a>`).join('');
  const removal = v.removal_url ? ` <a href="${esc(v.removal_url)}">${tr('Ask for a thread to be removed')}</a>.` : '';
  $('#footer').innerHTML = `
    <p>${esc(tr(v.description))}</p>
    <p>${esc(tr('{count} discussions out of {total} lively threads rated by', { count: num.format(v.entries), total: num.format(v.run.count) }))}
      <code>${esc(v.run.model)}</code> (${esc(v.run.prompt_version)}). ${tr('Summaries written by a local model')}
      (${esc(v.run.write_version)}). ${tr('Participants are never named; the summaries are machine-generated from public threads on')}
      <a href="${esc(m.forum_url)}">${esc(m.forum_url.replace(/^https?:\/\//, ''))}</a>.
      ${tr('They may be wrong or one-sided.')} ${tr('The forum posts themselves are not republished.')}${removal}
      ${tr('Data')} ${esc(dataDate(m.generated_at))}.
      <a href="../">Forum Insights</a> · <a href="../tips/">${tr('Forum Tips')}</a> · <a href="https://github.com/miyako/4d-forum-insights">${tr('Source')}</a></p>`;

  $('#search-form').addEventListener('submit', (e) => { e.preventDefault(); setQuery($('#q').value); });
  let timer;
  $('#q').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => setQuery($('#q').value, true), 220); });
  document.querySelectorAll('.modes button').forEach((b) => b.addEventListener('click', () => {
    app.state.mode = b.dataset.mode;
    syncControls();
    update({ keepPage: true });
    if (app.state.mode !== 'kw') startSemantic();
  }));
  $('#since').addEventListener('change', (e) => { app.state.since = Number(e.target.value); update(); });
  $('#sort').addEventListener('change', (e) => { app.state.sort = e.target.value; update(); });
  $('#presets').addEventListener('click', (e) => {
    const a = e.target.closest('[data-params]');
    if (!a) return;
    e.preventDefault();
    app.state = parseVoicesState('?' + a.dataset.params + (app.state.mode !== 'kw' ? `&mode=${app.state.mode}` : ''));
    syncControls();
    update();
    if (app.state.mode !== 'kw') startSemantic();
  });
  $('#subjects').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-subject]');
    if (!b) return;
    app.state.subject = toggle(app.state.subject, b.dataset.subject);
    update();
  });
  $('#settled').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-settled]');
    if (!b) return;
    app.state.settled = toggle(app.state.settled, b.dataset.settled);
    update();
  });
  $('#results').addEventListener('click', (e) => {
    const a = e.target.closest('[data-subject-chip]');
    if (a) {
      e.preventDefault();
      app.state.subject = [a.dataset.subjectChip];
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

const toggle = (arr, k) => (!k ? [] : arr.includes(k) ? arr.filter((x) => x !== k) : [...arr, k]);

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
  $('#since').value = String(s.since || 0);
}

function renderSubjects(base, preds) {
  const pool = applyVoices(base, preds, 'subject');
  const counts = new Map();
  for (const it of pool) counts.set(it.subject.key, (counts.get(it.subject.key) || 0) + 1);
  const sel = new Set(app.state.subject);
  $('#subjects').innerHTML = `<button type="button" class="area${sel.size ? '' : ' active'}" data-subject="" aria-pressed="${!sel.size}">${tr('All subjects')} <span class="n">${num.format(pool.length)}</span></button>`
    + app.q.subject.choices.map((c) => {
      const n = counts.get(c.key) || 0;
      const on = sel.has(c.key);
      return `<button type="button" class="area${on ? ' active' : ''}${n ? '' : ' zero'}" data-subject="${esc(c.key)}" aria-pressed="${on}" title="${esc(tr(c.long))}">${esc(tr(c.label))} <span class="n">${num.format(n)}</span></button>`;
    }).join('');
}

function renderSettled(base, preds) {
  const pool = applyVoices(base, preds, 'settled');
  const counts = new Map();
  for (const it of pool) counts.set(it.settled.key, (counts.get(it.settled.key) || 0) + 1);
  $('#settled').innerHTML = app.q.settled.choices.filter((c) => counts.get(c.key) || app.state.settled.includes(c.key)).map((c) => {
    const on = app.state.settled.includes(c.key);
    return `<button type="button" class="type settled-${esc(c.key)}${on ? ' active' : ''}" data-settled="${esc(c.key)}" aria-pressed="${on}" title="${esc(tr(c.long))}">${esc(tr(c.label))} <span class="n">${num.format(counts.get(c.key) || 0)}</span></button>`;
  }).join('');
}

function renderSort(hasQuery, current) {
  $('#sort').innerHTML = SORTS.filter((o) => !o.needsQuery || hasQuery)
    .map((o) => `<option value="${o.key}"${o.key === current ? ' selected' : ''}>${tr(o.label)}</option>`).join('');
}

function highlight(text) {
  const t = esc(text);
  const terms = app.terms.filter((x) => x.length > 2).slice(0, 30);
  if (!terms.length) return t;
  const re = new RegExp(`(${terms.map((x) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
  return t.replace(/(^|>)([^<]+)/g, (m, a, b) => a + b.replace(re, '<mark>$1</mark>'));
}

const dataDate = (value) => value && Number.isFinite(Date.parse(value)) ? fmtDate(Date.parse(value) / 864e5) : '';

function card(it) {
  const t = it.topic;
  const m = app.manifest;
  const url = `${m.forum_url}/t/${t.slug}/${t.id}`;
  const settled = choice('settled', it.settled.key);
  const subject = choice('subject', it.subject.key);
  const level = debateLevel(it.debate, app.voices.debate.map((label) => tr(label)));
  const span = it.days >= 1 ? tr(it.days === 1 ? ' over {days} day' : ' over {days} days', { days: num.format(it.days) }) : '';
  return `<article class="voice-card settled-${esc(it.settled.key)}">
    <div class="tip-head">
      <span class="badge badge-${esc(it.settled.key)}" title="${esc(settled.long)} (${pct(it.settled.p)})">${esc(settled.label)}</span>
      <h2 class="tip-title" lang="${it.contentLocale || 'en'}"><a href="${esc(url)}" target="_blank" rel="noopener">${highlight(it.question)}</a></h2>
    </div>
    ${it.translationFallback ? `<p class="translation-fallback">${tr('English fallback')}</p>` : ''}
    ${it.positions.length ? `<ul class="positions" lang="${it.contentLocale || 'en'}">${it.positions.map((p) => `<li>${highlight(p)}</li>`).join('')}</ul>` : ''}
    ${it.outcome ? `<p class="outcome"><span class="outcome-label">${tr('Outcome')}</span> <span lang="${it.contentLocale || 'en'}">${highlight(it.outcome)}</span></p>` : ''}
    <div class="tip-meta">
      <a href="${esc(localeURL('?subject=' + encodeURIComponent(it.subject.key), i18n.locale, location.href))}" class="chip area-chip" data-subject-chip="${esc(it.subject.key)}" title="${esc(subject.long)} (${pct(it.subject.p)})">${esc(subject.label)}</a>
      <span class="chip debate debate-${level.i}" title="${tr('How far apart the views are (model estimate)')}">${esc(level.label)}</span>
      <span class="meta"><span class="orig-title" title="${tr('Thread title')}">${tr('Original title')}: <span lang="${esc(t.lang || 'en')}">“${esc(t.title)}”</span></span><span class="dot">·</span>
        <span>${fmtDate(t.created)}</span><span class="dot">·</span><span>${esc((t.lang || '').toUpperCase())}</span>
        <span class="dot">·</span><span title="${tr('participants')}">👥 ${num.format(it.participants)}</span>
        <span class="dot">·</span><span title="${tr('replies')}">💬 ${num.format(Math.max(0, t.posts - 1))}${span}</span>
        <span class="dot">·</span><span title="${tr('views')}">👁 ${compact.format(t.views)}</span>
      </span>
      <a class="thread-link" href="${esc(url)}" target="_blank" rel="noopener">${tr('Read the thread ↗')}</a>
    </div>
    <details class="why" data-id="${t.id}"><summary>${tr('About this summary')}</summary><div class="why-body"></div></details>
  </article>`;
}

function renderWhy(d) {
  const it = app.byId.get(Number(d.dataset.id));
  if (!it) return;
  const v = app.voices;
  const level = debateLevel(it.debate, v.debate.map((label) => tr(label)));
  const status = it.ok === 0 ? 'The writer model found no real exchange of views in this thread (hidden unless all=1).'
    : it.ok === 2 ? 'The summary did not pass every format check (length or phrasing).' : 'The summary passed the checks (including: no names).';
  const removal = v.removal_url ? ` · <a href="${esc(v.removal_url)}${it.id}">${tr('Ask for removal')}</a>` : '';
  d.querySelector('.why-body').innerHTML = `
    <dl class="facts">
      <dt>${tr('Debate')}</dt><dd>${esc(level.label)} (${i18n.decimal.format(it.debate * 4)} / ${num.format(4)})</dd>
      <dt>${tr('Kind')}</dt><dd>${esc(choice('exchange', it.exchange).label)}</dd>
      <dt>${tr('Outcome')}</dt><dd>${esc(choice('settled', it.settled.key).label)} (${pct(it.settled.p)})</dd>
      <dt>${tr('Subject')}</dt><dd>${esc(choice('subject', it.subject.key).label)} (${pct(it.subject.p)})</dd>
      <dt>${tr('Thread')}</dt><dd>${esc(tr('{people} participants, {returning} of them posting 3 times or more', { people: num.format(it.participants), returning: num.format(it.returning) }))}</dd>
      <dt>${tr('Summary')}</dt><dd>${esc(tr(status))}</dd>
    </dl>
    <p class="small">${tr('Ratings by')} <code>${esc(v.run.model)}</code>.
      ${tr('The question, views and outcome are written by a local model from the thread and may miss nuances.')}
      <a href="${esc(localeURL('../?t=' + it.id, i18n.locale, location.href))}">${tr('Open in Forum Insights')}</a>${removal}</p>`;
  localizeDOM(d.querySelector('.why-body'), i18n);
}

function renderResults(note = app.lastNote || '') {
  app.lastNote = note;
  const s = app.state;
  const total = app.results.length;
  const scope = s.subject.length ? tr(' about {scope}', { scope: s.subject.map((k) => choice('subject', k).label).join(' + ') }) : '';
  $('#count').innerHTML = `<strong>${num.format(total)}</strong> ${tr(total === 1 ? 'discussion' : 'discussions')}${esc(scope)}`
    + (note ? ` <span class="muted small">· ${esc(tr(note))}</span>` : '');
  if (app.sem.list || !(s.mode !== 'kw' && s.q.trim())) {
    if (!app.sem.error) $('#status').hidden = true;
  }
  if (app.sem.error && s.mode !== 'kw') showStatus(esc(tr('Semantic search unavailable: {error}', { error: app.sem.error })), 'warn');
  if (!total) {
    $('#results').innerHTML = `<div class="empty">${tr('Nothing found. Try other words, Semantic mode, or fewer filters.')}</div>`;
    $('#more').innerHTML = '';
    return;
  }
  $('#results').innerHTML = app.results.slice(0, app.shown).map(card).join('');
  $('#more').innerHTML = total > app.shown
    ? `<button type="button" class="btn">${tr('Show more ({count} left)', { count: num.format(total - app.shown) })}</button>` : '';
  localizeDOM($('#results'), i18n);
}

function showStatus(html, kind = 'info') {
  const st = $('#status');
  st.className = `status ${kind}`;
  st.innerHTML = html;
  st.hidden = false;
}

init().catch((e) => {
  console.error(e);
  fail(esc(tr('Something went wrong: {error}', { error: e.message })));
});
