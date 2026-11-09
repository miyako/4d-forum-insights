export function installPageSwitcher(document = globalThis.document) {
  const switcher = document.querySelector('.page-switcher');
  if (!switcher) return;
  const summary = switcher.querySelector('summary');
  const links = () => [...switcher.querySelectorAll('a[href]')].filter((link) => !link.hidden);
  document.addEventListener('click', (event) => {
    if (!switcher.contains(event.target)) switcher.open = false;
  });
  document.addEventListener('focusin', (event) => {
    if (!switcher.contains(event.target)) switcher.open = false;
  });
  switcher.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && switcher.open) {
      event.preventDefault();
      switcher.open = false;
      summary.focus();
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      const items = links();
      if (!items.length) return;
      event.preventDefault();
      switcher.open = true;
      const current = items.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : event.key === 'ArrowDown' ? (current + 1) % items.length
        : current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length;
      items[next].focus();
    }
  });
}

if (globalThis.document) installPageSwitcher();
