// Colour theme: Auto (follow the OS, the default), Light or Dark, chosen from a menu button in the header.
// The choice is kept in localStorage; the inline script in index.html applies it before first paint.
// Auto removes data-theme so the prefers-color-scheme media query keeps following the OS live.

import { createLocale } from './locale.js';

const KEY = 'theme';
const THEMES = ['auto', 'light', 'dark'];
const LABEL = { auto: 'Auto (follows system)', light: 'Light', dark: 'Dark' };
const root = document.documentElement;
const meta = document.querySelector('meta[name=color-scheme]');

function stored() {
  try {
    const v = localStorage.getItem(KEY);
    return THEMES.includes(v) ? v : 'auto';
  } catch {
    return 'auto';
  }
}

function apply(theme) {
  if (theme === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', theme);
  if (meta) meta.content = theme === 'auto' ? 'light dark' : theme;
}

function save(theme) {
  try {
    if (theme === 'auto') localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch { /* private mode: the choice lasts for this page only */ }
}

function init() {
  const btn = document.getElementById('theme-btn');
  const list = document.getElementById('theme-list');
  if (!btn || !list) return;
  const items = [...list.querySelectorAll('[role=menuitemradio]')];

  const render = (theme) => {
    const label = createLocale(root.lang).t(`Color theme: ${LABEL[theme]}`);
    btn.setAttribute('aria-label', label);
    btn.title = label;
    btn.querySelector('use').setAttribute('href', `#i-${theme}`);
    for (const it of items) it.setAttribute('aria-checked', String(it.dataset.themeValue === theme));
  };

  const isOpen = () => !list.hidden;
  const open = (focus = 'checked') => {
    list.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    const idx = focus === 'last' ? items.length - 1
      : focus === 'first' ? 0
      : Math.max(0, items.findIndex((it) => it.getAttribute('aria-checked') === 'true'));
    items[idx].focus();
  };
  const close = (refocus = true) => {
    if (!isOpen()) return;
    list.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    if (refocus) btn.focus();
  };
  const choose = (theme) => {
    apply(theme);
    save(theme);
    render(theme);
    close();
  };

  btn.addEventListener('click', () => (isOpen() ? close() : open()));
  btn.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); open('checked'); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); open('last'); }
  });
  list.addEventListener('click', (e) => {
    const it = e.target.closest('[role=menuitemradio]');
    if (it) choose(it.dataset.themeValue);
  });
  list.addEventListener('keydown', (e) => {
    const i = items.indexOf(document.activeElement);
    const move = (j) => { e.preventDefault(); items[(j + items.length) % items.length].focus(); };
    switch (e.key) {
      case 'ArrowDown': move(i + 1); break;
      case 'ArrowUp': move(i - 1); break;
      case 'Home': move(0); break;
      case 'End': move(items.length - 1); break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        if (i >= 0) choose(items[i].dataset.themeValue);
        break;
      case 'Escape':
        e.preventDefault();
        e.stopPropagation(); // don't also close the topic detail panel
        close();
        break;
      case 'Tab':
        close(false);
        break;
      default: {
        // type-ahead: first letter of a theme name
        const k = e.key.length === 1 ? e.key.toLowerCase() : '';
        const j = items.findIndex((it) => it.dataset.themeValue.startsWith(k));
        if (k && j >= 0) move(j);
      }
    }
  });
  document.addEventListener('pointerdown', (e) => {
    if (isOpen() && !e.target.closest('#theme-menu')) close(false);
  });
  list.addEventListener('focusout', (e) => {
    if (isOpen() && !e.relatedTarget?.closest?.('#theme-menu')) close(false);
  });
  // keep several open tabs in step
  window.addEventListener('storage', (e) => {
    if (e.key === KEY || e.key === null) { const t = stored(); apply(t); render(t); }
  });

  const theme = stored();
  apply(theme);
  render(theme);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
