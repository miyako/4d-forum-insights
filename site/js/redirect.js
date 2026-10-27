import { preferredLocale } from './locale.js';
import { REQUEST_STATE_KEYS } from './state.js';

export function landingURL(href, locale = 'en') {
  const url = new URL(href);
  const legacy = [...url.searchParams.keys()].some(key => REQUEST_STATE_KEYS.includes(key) || key.startsWith('a.'));
  const directory = new URL(url);
  directory.pathname = directory.pathname.endsWith('/index.html')
    ? directory.pathname.slice(0, -'index.html'.length)
    : directory.pathname.replace(/\/?$/, '/');
  const target = new URL(legacy ? 'requests/' : 'voices/', directory);
  target.search = url.search;
  target.hash = url.hash;
  target.searchParams.set('ui', locale);
  return target.pathname + target.search + target.hash;
}

if (typeof window !== 'undefined') {
  window.location.replace(landingURL(window.location.href, preferredLocale(window.location.search)));
}
