// Left filter column shared by Tips, Voices and Questions: on narrow screens it becomes a full-screen panel
// opened by #open-filters and closed by #close-filters or Escape (same body class as the Requests page).
export function installFiltersPanel(document = globalThis.document) {
  const body = document.body;
  const open = document.getElementById('open-filters');
  const close = document.getElementById('close-filters');
  if (!open || !close) return;
  open.addEventListener('click', () => { body.classList.add('show-filters'); close.focus(); });
  close.addEventListener('click', () => { body.classList.remove('show-filters'); open.focus(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && body.classList.contains('show-filters')) { body.classList.remove('show-filters'); open.focus(); }
  });
}

installFiltersPanel();
