import { loadManifest, getJSON } from './data.js';
import { KeywordIndex } from './search.js';
import { Semantic } from './semantic.js';
import { createLocale, preferredLocale, installLocale, localeURL, stateURL, modelDisclosure, loadOverlay } from './locale.js';
import {
  COVERAGE, IMPACT, COMPLEXITY, DOMAINS, STATE_KEYS, RATING_TONES, coverageIcon, buildQuestions, parseQuestionsState,
  serializeQuestionsState, filterQuestions, sortQuestions, rankQuestions, questionsEnabled, questionsPreview, questionsPublicPreview, questionsSemanticMeta,
} from './questions-core.js';

const $ = (selector) => document.querySelector(selector);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const i18n = createLocale(preferredLocale(location.search));
const { t: tr, number: num, pct } = i18n;
const PAGE = 30;
const facets = { coverage: ['Answer coverage', COVERAGE], impact: ['Importance (impact)', IMPACT], complexity: ['Complexity', COMPLEXITY], domain: ['Domains', DOMAINS] };
const app = { items: [], state: parseQuestionsState(location.search), shown: PAGE, semanticResult: null, busy: false, error: null, ready: false };
installLocale(i18n, 'questions');

function status(message, error = false) {
  $('#status').textContent = message;
  $('#status').className = 'status ' + (error ? 'error' : 'info');
  $('#status').hidden = !message;
}

async function init() {
  try {
    const manifest = await loadManifest();
    if (!(questionsEnabled(manifest) || questionsPreview(manifest) || questionsPublicPreview(manifest))) throw new Error(tr('Questions is not enabled in this dataset.'));
    const data = await getJSON(manifest.questions_view.files.entries);
    const questions = buildQuestions(data);
    const localized = await loadOverlay(data.entries.map(({ id, title, body }) => ({ id, title, body: body ?? '' })),
      'questions', i18n.locale, getJSON);
    const byID = new Map(localized.map((item) => [item.id, item]));
    app.items = questions.map((item) => ({ ...item, ...byID.get(item.id) }));
    if (i18n.locale !== 'en') {
      $('#translation-coverage').hidden = false;
      $('#translation-coverage').textContent = tr('{current} of {total} records translated; {eligibleCurrent} of {eligible} eligible questions translated. Missing or stale records use labelled English fallback.', {
        current: num.format(localized.filter((item) => !item.translationFallback).length),
        total: num.format(localized.length),
        eligibleCurrent: num.format(app.items.filter((item) => !item.translationFallback).length),
        eligible: num.format(app.items.length),
      });
    }
    app.semantic = new Semantic(questionsSemanticMeta(manifest));
    app.semantic.onProgress = (p) => {
      if (app.busy && p.file && p.total) status(tr('Loading semantic search…') + ' ' + pct((p.loaded || 0) / p.total));
    };
    app.index = await KeywordIndex.create(app.items.map((it) => ({
      id: it.id, title: it.title, title_en: it.canonicalFields?.title || it.title,
      line: [it.body, it.canonicalFields?.body].filter(Boolean).join(' '), tags: it.tags,
    })));
    if (questionsPreview(manifest)) {
      $('#preview').hidden = false;
      $('#preview').textContent = manifest.questions_view.snapshot_scope === 'processed'
        ? tr('Private processed snapshot: {count} records, {questions} eligible questions. Corpus incomplete; classification paused. Nothing here is published.', {
          count: num.format(data.entries.length), questions: num.format(app.items.length),
        })
        : tr('Private frozen sample: {count} threads, not representative. Full processing continues; nothing here is published.', { count: num.format(data.entries.length) });
    } else if (questionsPublicPreview(manifest)) {
      if (data.public_preview !== true || data.publish_allowed !== true || data.private_preview !== false) {
        throw new Error('Public Questions preview requires publication-approved data');
      }
      $('#preview').hidden = false;
      $('#preview').classList.add('warn');
      $('#preview').textContent = manifest.questions_view.snapshot_scope === 'processed'
        ? tr('Public processed preview: {count} of {total} corpus records, {questions} eligible questions. Corpus incomplete and not representative. Model assessments are unreviewed; semantic search is disabled.', {
          count: num.format(data.entries.length), total: num.format(manifest.questions_view.corpus_total),
          questions: num.format(app.items.length),
        })
        : tr('Public preview: a frozen sample of {count} threads, not the full dataset or a representative sample. Model assessments are unreviewed. Full processing continues; semantic search is not enabled.', { count: num.format(data.entries.length) });
    }
    $('#semantic-disclosure').textContent = app.semantic.available ? modelDisclosure(manifest.embeddings, i18n) : tr('Semantic search is unavailable in this preview; keyword search is ready.');
    for (const button of document.querySelectorAll('[data-mode]')) {
      button.disabled = button.dataset.mode !== 'kw' && !app.semantic.available;
    }
    $('#footer').textContent = tr('Unreviewed decision-model assessments. Consult the original thread; no claim that an old issue persists in current 4D versions.');
    app.ready = true;
    controls();
    update({ replace: true });
  } catch (e) {
    $('#count').textContent = '';
    status(tr('Could not load Questions: {error}', { error: e.message }), true);
    console.error('Questions initialization', e);
  }
}

function controls() {
  $('#q').value = app.state.q;
  for (const key of ['solved', 'closed', 'replies']) $('#' + key).value = app.state[key];
  $('#sort').value = app.state.sort || (app.state.q.trim() ? 'relevance' : 'new');
  $('#clarification').checked = app.state.clarification;
  for (const button of document.querySelectorAll('[data-mode]')) button.setAttribute('aria-checked', String(app.state.mode === button.dataset.mode));
}

function update({ history = true, replace = false, keepPage = false } = {}) {
  if (!app.ready) return;
  const s = app.state;
  if (history) {
    const qs = stateURL(location.search, serializeQuestionsState(s), STATE_KEYS, i18n.locale);
    const url = location.pathname + '?' + qs + location.hash;
    if (url !== location.pathname + location.search + location.hash) window.history[replace ? 'replaceState' : 'pushState'](null, '', url);
  }
  if (!keepPage) app.shown = PAGE;
  const query = s.q.trim();
  const semantic = app.semanticResult?.q === query ? app.semanticResult.list : null;
  const ranked = rankQuestions(app.items, query, s.mode, app.index.search(query), semantic);
  const sort = s.sort || (query ? 'relevance' : 'new');
  const results = sortQuestions(filterQuestions(ranked.items, s), sort, ranked.relevance);
  $('#count').textContent = tr('{count} questions', { count: num.format(results.length) });
  $('#facets').innerHTML = Object.entries(facets).map(([key, [label, choices]]) => {
    const base = filterQuestions(ranked.items, s, key);
    return `<fieldset class="question-facet"><legend>${esc(tr(label))}</legend><div class="areas">`
      + Object.entries(choices).map(([value, name]) => {
        const count = base.filter((it) => key === 'domain' ? it.domains.includes(value) : it[key].choice === value).length;
        const selected = s[key].includes(value);
        const tone = RATING_TONES[key]?.[value];
        return `<button type="button" class="area${selected ? ' active' : ''}${tone ? ' rating-tone tone-' + tone : ''}" data-facet="${key}" data-value="${value}" aria-pressed="${selected}">${key === 'coverage' ? coverageIcon(value) : ''}${esc(tr(name))} <span class="n">${num.format(count)}</span></button>`;
      }).join('') + '</div></fieldset>';
  }).join('');
  $('#results').innerHTML = results.length ? results.slice(0, app.shown).map(card).join('') : `<p class="empty">${esc(tr('No questions match. Try removing a filter.'))}</p>`;
  $('#more').innerHTML = results.length > app.shown ? `<button type="button" class="btn" id="show-more">${esc(tr('Show more'))}</button>` : '';
  if (s.t) {
    const focus = document.getElementById('question-' + s.t);
    if (focus) focus.classList.add('focused');
    else {
      const item = app.items.find((it) => it.id === s.t);
      $('#results').insertAdjacentHTML('afterbegin', item
        ? `<p class="small muted">${esc(tr('Linked question shown separately from current filters.'))}</p>${card(item)}`
        : `<p class="small muted">${esc(tr('This topic is not a classified question in this dataset.'))}</p>`);
    }
  }
  installLocale(i18n, 'questions');
  if (!query || s.mode === 'kw') status('');
  else if (!app.semantic.available) status(tr('Semantic search is unavailable in this preview; keyword search is ready.'));
  else if (app.error?.q === query) status(tr('Semantic search failed: {error}. Showing keyword results.', { error: app.error.message }), true);
  else if (!semantic) status(tr('Loading semantic search; showing keyword results.'));
  else status('');
  if (query && s.mode !== 'kw' && app.semantic.available && !semantic && app.error?.q !== query) void searchSemantic();
}

async function searchSemantic() {
  if (app.busy) return;
  const q = app.state.q.trim();
  app.busy = true;
  try {
    // Rank the entire existing index before filtering by this page's stable topic IDs.
    const list = await app.semantic.search(q, app.semantic.meta.count);
    app.semanticResult = { q, list };
    app.error = null;
  } catch (e) {
    app.error = { q, message: e.message };
    console.error('Questions semantic search', e);
  } finally {
    app.busy = false;
    update({ history: false, keepPage: true });
  }
}

function card(it) {
  const flag = (value) => tr(value === null ? 'Unknown' : value ? 'Yes' : 'No');
  const distribution = (name, value, labels) => `<div><strong>${esc(tr(name))}</strong><dl>`
    + Object.entries(labels).map(([key, label]) => `<dt>${esc(tr(label))}</dt><dd>${pct(value.probabilities[key])}</dd>`).join('') + '</dl></div>';
  const link = (page, params = '') => esc(localeURL(`../${page}/${params}`, i18n.locale, location.href));
  const permalink = '?' + stateURL(location.search, serializeQuestionsState({ ...app.state, t: it.id }), STATE_KEYS, i18n.locale);
  const contentLocale = it.contentLocale || 'en';
  return `<article id="question-${it.id}" class="tip-card question-card coverage-${it.coverage.choice}">
    <h2 class="tip-title" lang="${contentLocale}"><a href="https://discuss.4d.com/t/${it.id}" target="_blank" rel="noopener">${esc(it.title)}</a></h2>
    ${it.translationFallback ? `<p class="translation-fallback">${esc(tr('English fallback'))}</p>` : ''}
    <div class="question-ratings">
      ${[['Answer coverage', it.coverage, COVERAGE, 'coverage'], ['Importance (impact)', it.impact, IMPACT, 'impact'], ['Complexity', it.complexity, COMPLEXITY, 'complexity']].map(([name, value, labels, key]) => `<span class="rating-tone${RATING_TONES[key] ? ' tone-' + RATING_TONES[key][value.choice] : ''}">${key === 'coverage' ? coverageIcon(value.choice) : ''}${esc(tr(name))}: ${esc(tr(labels[value.choice]))} (${pct(value.probabilities[value.choice])})</span>`).join('')}
    </div>
    <p class="small muted">${esc(tr('Solved'))}: ${flag(it.solved)} · ${esc(tr('Closed'))}: ${flag(it.closed)} · ${it.replies === null ? esc(tr('Replies')) + ': ' + esc(tr('Unknown')) : it.replies === 0 ? esc(tr('Unanswered (zero replies)')) : num.format(it.replies) + ' ' + esc(tr('replies'))}
      ${it.lang ? ' · ' + esc(it.lang.toUpperCase()) : ''}${it.active === null ? '' : ' · ' + i18n.date(it.active / 864e5)}
      ${it.clarification >= .5 ? ' · ' + esc(tr('Needs clarification')) : ''}</p>
    ${it.body ? `<p class="small muted">${esc(tr(contentLocale === 'en' ? 'Thread summary (English)' : contentLocale === 'fr' ? 'Thread summary (French)' : 'Thread summary (Japanese)'))}</p><p class="tip-body" lang="${contentLocale}">${esc(it.body)}</p>` : `<p class="small muted">${esc(tr('No existing thread summary.'))}</p>`}
    <div class="tip-meta">${it.domains.map((key) => `<span class="chip">${esc(tr(DOMAINS[key]))}</span>`).join('')}</div>
    <details class="why"><summary>${esc(tr('Model probabilities and evidence limits'))}</summary>
      <div class="why-body">
        <p>${esc(tr('Question present'))}: ${pct(it.questionProbability)} · ${esc(tr('Needs clarification'))}: ${pct(it.clarification)}</p>
        <div class="question-distributions">${distribution('Answer coverage', it.coverage, COVERAGE)}${distribution('Importance (impact)', it.impact, IMPACT)}${distribution('Complexity', it.complexity, COMPLEXITY)}</div>
        <p>${Object.entries(DOMAINS).map(([key, label]) => `${esc(tr(label))}: ${pct(it.domainProbabilities[key])}`).join(' · ')}</p>
        ${it.warnings.map((warning) => `<p lang="en">${esc(warning)}</p>`).join('')}
        <p class="muted">${esc(it.decisionModel)} · ${esc(it.decisionVersion)}</p>
      </div>
    </details>
    <p class="question-links"><a href="${esc(permalink)}">${esc(tr('Permalink'))}</a>
      <a href="${link('requests', '?t=' + it.id)}">${esc(tr('Open in Forum Requests'))}</a>
      <a href="${link('tips', '?q=' + encodeURIComponent(it.title))}">${esc(tr('Tips'))}</a>
      <a href="${link('voices', '?q=' + encodeURIComponent(it.title))}">${esc(tr('Voices'))}</a>
    </p>
  </article>`;
}

$('#search-form').addEventListener('submit', (event) => {
  event.preventDefault();
  app.state.q = $('#q').value;
  app.state.t = 0;
  app.error = null;
  controls();
  update();
});
document.addEventListener('click', (event) => {
  const facet = event.target.closest('[data-facet]');
  const mode = event.target.closest('[data-mode]');
  if (facet) {
    const key = facet.dataset.facet, value = facet.dataset.value;
    app.state[key] = app.state[key].includes(value) ? app.state[key].filter((v) => v !== value) : [...app.state[key], value];
    update();
  } else if (mode && !mode.disabled) {
    app.state.mode = mode.dataset.mode;
    app.error = null;
    controls(); update();
  } else if (event.target.closest('#show-more')) {
    app.shown += PAGE; update({ keepPage: true, history: false });
  } else if (event.target.closest('#reset')) {
    app.state = parseQuestionsState(''); controls(); update();
  }
});
for (const key of ['solved', 'closed', 'replies', 'sort', 'clarification']) $('#' + key).addEventListener('change', () => {
  app.state[key] = key === 'clarification' ? $('#' + key).checked : $('#' + key).value;
  update();
});
window.addEventListener('popstate', () => {
  if (preferredLocale(location.search) !== i18n.locale) return location.reload();
  app.state = parseQuestionsState(location.search); controls(); update({ history: false });
});
document.addEventListener('keydown', (event) => {
  if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey
    && !event.target.closest('input, textarea, select, [contenteditable]')) {
    event.preventDefault(); $('#q').focus();
  }
});
void init();
