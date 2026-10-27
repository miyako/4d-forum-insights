// `lang` already filters source-thread languages in Forum Insights; UI language uses `ui`.
import { messages } from './locale-messages.js';

export const LOCALES = ['en', 'fr', 'ja'];
export const localeOf = (search = '') => {
  const value = new URLSearchParams(search).get('ui');
  return LOCALES.includes(value) ? value : 'en';
};

export function preferredLocale(search = '', storage, languages = globalThis.navigator?.languages || []) {
  const explicit = new URLSearchParams(search).get('ui');
  if (LOCALES.includes(explicit)) return explicit;
  try {
    const saved = (storage === undefined ? globalThis.localStorage : storage)?.getItem('ui-locale');
    if (LOCALES.includes(saved)) return saved;
  } catch { /* Storage is optional in private browsing. */ }
  for (const language of languages) {
    const locale = language.toLowerCase().split('-')[0];
    if (LOCALES.includes(locale)) return locale;
  }
  return 'en';
}

/** Change only the UI locale, retaining filters, source language, query, and fragment. */
export function localeURL(href, locale, base = 'https://locale.invalid/') {
  const url = new URL(href, base);
  url.searchParams.set('ui', LOCALES.includes(locale) ? locale : 'en');
  return url.pathname + url.search + url.hash;
}

export function navigationURL(href, locale, current) {
  const target = new URL(localeURL(href, locale, current), current);
  const source = new URL(current);
  if (target.pathname !== source.pathname && source.searchParams.get('q')) {
    target.searchParams.set('q', source.searchParams.get('q'));
    const mode = source.searchParams.get('mode');
    if (['kw', 'hyb', 'sem'].includes(mode)) target.searchParams.set('mode', mode);
  }
  return target.pathname + target.search + target.hash;
}

/** Merge a page's serialized state without discarding unrelated URL state, notably `lang`. */
export function stateURL(search, serialized, keys, locale) {
  const params = new URLSearchParams(search);
  for (const key of keys) params.delete(key);
  for (const [key, value] of new URLSearchParams(serialized)) params.set(key, value);
  params.set('ui', LOCALES.includes(locale) ? locale : 'en');
  return params.toString().replace(/%2C/g, ',');
}

export function createLocale(locale = 'en') {
  if (!LOCALES.includes(locale)) locale = 'en';
  const number = new Intl.NumberFormat(locale);
  const compact = new Intl.NumberFormat(locale, { notation: 'compact', maximumFractionDigits: 1 });
  const decimal = new Intl.NumberFormat(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const percent = new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 0 });
  const date = new Intl.DateTimeFormat(locale, { timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric' });
  return {
    locale, number, compact, decimal,
    pct: (value) => percent.format(value),
    date: (days) => days == null ? '' : date.format(new Date(days * 864e5)),
    t: (key, values = {}) => {
      const dictionary = messages[locale] || {};
      const message = Object.hasOwn(dictionary, key) ? dictionary[key] : String(key ?? '');
      return message.replace(/\{(\w+)\}/g, (match, name) => values[name] ?? match);
    },
  };
}

export function modelDisclosure(embeddings, i18n) {
  const { model_download_bytes: webgpu, wasm_model_download_bytes: wasm } = embeddings || {};
  if (Number.isFinite(webgpu) && webgpu > 0 && Number.isFinite(wasm) && wasm > 0) {
    return i18n.t('Semantic search downloads a model on first use: about {webgpu} MB with WebGPU, or {wasm} MB for the WASM fallback. Keyword search does not download the model.', {
      webgpu: i18n.number.format(Math.round(webgpu / 1e6)),
      wasm: i18n.number.format(Math.round(wasm / 1e6)),
    });
  }
  return i18n.t('Semantic search downloads a model on first use. Keyword search does not download the model.');
}

/** Recursively sorted object keys; array order and string contents are significant. */
export function canonicalJSON(value) {
  if (Array.isArray(value)) return '[' + value.map(canonicalJSON).join(',') + ']';
  if (value !== null && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + canonicalJSON(value[key])).join(',') + '}';
  }
  return JSON.stringify(value);
}

export async function sourceHash(fields, crypto = globalThis.crypto) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonicalJSON(fields)));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export const translatedFields = (page, item) => page === 'requests'
  ? { title_en: item.title_en ?? '', line: item.line ?? '', para: item.para ?? '' }
  : page === 'tips'
  ? { headline: item.headline, body: item.body }
  : { question: item.question, positions: item.positions, outcome: item.outcome };

function validFields(page, fields, source) {
  if (!fields || typeof fields !== 'object' || Array.isArray(fields)) return false;
  const keys = page === 'requests' ? ['title_en', 'line', 'para']
    : page === 'tips' ? ['headline', 'body'] : ['question', 'positions', 'outcome'];
  if (Object.keys(fields).sort().join() !== [...keys].sort().join()) return false;
  return keys.every((key) => key === 'positions'
    ? Array.isArray(fields[key]) && fields[key].length === source[key].length
      && fields[key].every((part) => typeof part === 'string' && part.trim())
    : typeof fields[key] === 'string' && (page === 'requests'
      ? Boolean(fields[key].trim()) === Boolean(source[key].trim())
      : (!source[key] || fields[key].trim())));
}

/** Fail closed per entry, never mixing translated and stale/missing English fields. */
export async function applyOverlay(items, overlay, page, locale, hash = sourceHash) {
  if (locale === 'en') return items;
  const valid = overlay?.version === 1 && overlay.locale === locale && overlay.page === page
    && overlay.entries && typeof overlay.entries === 'object' && !Array.isArray(overlay.entries);
  return Promise.all(items.map(async (item) => {
    const source = translatedFields(page, item);
    const entry = valid && overlay.entries[item.id];
    let translated = false;
    if (entry?.translation_version === '1' && validFields(page, entry.fields, source)) {
      try { translated = entry.source_hash === await hash(source); } catch { /* no WebCrypto: English fallback */ }
    }
    return translated
      ? { ...item, ...entry.fields, canonicalFields: source, contentLocale: locale, translationFallback: false }
      : { ...item, contentLocale: 'en', translationFallback: true };
  }));
}

export async function loadOverlay(items, page, locale, fetchJSON) {
  if (locale === 'en') return items;
  let overlay = null;
  try { overlay = await fetchJSON(`locales/${locale}/${page}.json`); } catch { /* overlays are optional */ }
  return applyOverlay(items, overlay, page, locale);
}

/** Localize static markup without replacing controls or their event handlers. */
export function localizeDOM(root, i18n) {
  const { t } = i18n;
  const walk = (node) => {
    if (node.nodeType === 3 && node.textContent.trim()) {
      const text = node.textContent;
      node.textContent = text.replace(text.trim(), t(text.trim()));
    } else if (node.nodeType === 1 && !node.hasAttribute('lang') && !['SCRIPT', 'STYLE', 'CODE', 'SVG', 'NOSCRIPT'].includes(node.tagName)) {
      for (const attr of ['title', 'placeholder', 'aria-label']) {
        if (node.hasAttribute(attr)) node.setAttribute(attr, t(node.getAttribute(attr)));
      }
      for (const child of node.childNodes) walk(child);
    }
  };
  walk(root);
}

export function installLocale(i18n, page, document = globalThis.document, location = globalThis.location) {
  document.documentElement.lang = i18n.locale;
  document.documentElement.removeAttribute?.('data-localizing');
  try { globalThis.localStorage?.setItem('ui-locale', i18n.locale); } catch { /* Optional preference. */ }
  localizeDOM(document.body, i18n);
  const nav = document.getElementById('locale-nav');
  nav.setAttribute('aria-label', i18n.t('Interface language'));
  nav.innerHTML = LOCALES.map((locale) => `<a lang="${locale}" hreflang="${locale}" href="${localeURL(location.href, locale).replace(/&/g, '&amp;').replace(/"/g, '&quot;')}"${locale === i18n.locale ? ' aria-current="true"' : ''}>${{ en: 'EN', fr: 'FR', ja: '日本語' }[locale]}</a>`).join('');
  for (const link of document.querySelectorAll('a[href]')) {
    const href = link.dataset.localeHref || link.getAttribute('href');
    if (link.closest('#locale-nav') || !/^(?:\.\.\/(?:tips\/|voices\/|requests\/)?|\.\/)$/.test(href)) continue;
    link.dataset.localeHref = href;
    link.href = navigationURL(href, i18n.locale, location.href);
  }
  document.title = i18n.t(page === 'requests' ? '4D Forum Requests' : page === 'tips' ? '4D Forum Tips' : '4D Forum Voices');
  for (const option of document.querySelectorAll('#conf option')) {
    if (Number(option.value)) option.textContent = '≥ ' + i18n.pct(Number(option.value) / 100);
  }
}
