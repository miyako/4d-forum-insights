// Match preferredLocale before the first paint, without downloading page data.
{
  const supported = ['en', 'fr', 'ja'];
  const explicit = new URLSearchParams(location.search).get('ui');
  let saved;
  try { saved = localStorage.getItem('ui-locale'); } catch { /* Optional preference. */ }
  const browser = (navigator.languages || []).map(value => value.toLowerCase().split('-')[0])
    .find(value => supported.includes(value));
  document.documentElement.lang = supported.includes(explicit) ? explicit
    : supported.includes(saved) ? saved : browser || 'en';
}
if (document.documentElement.lang !== 'en') {
  document.documentElement.setAttribute('data-localizing', '');
  setTimeout(() => document.documentElement.removeAttribute('data-localizing'), 3000);
}
