(() => {
  const theme = localStorage.getItem('reaper-theme');
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    const current = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    const next = current === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('reaper-theme', next);
  });
  document.querySelectorAll('.expand-button').forEach(button => button.addEventListener('click', () => {
    const target = document.getElementById(button.getAttribute('aria-controls'));
    const expanded = button.getAttribute('aria-expanded') === 'true';
    target.hidden = expanded;
    button.setAttribute('aria-expanded', String(!expanded));
    button.textContent = expanded ? '＋ Expand audit' : '− Collapse audit';
  }));
  const selection = document.getElementById('repository-selection');
  if (selection) {
    const boxes = [...selection.querySelectorAll('input[name="repositories"]')];
    const update = () => {
      const count = boxes.filter(box => box.checked).length;
      document.getElementById('selection-count').textContent = `${count} selected · maximum 20 per scan`;
      selection.querySelector('button:not([type="button"])').disabled = count === 0 || count > 20;
    };
    document.getElementById('select-all').addEventListener('click', () => { boxes.forEach(box => { box.checked = true; }); update(); });
    document.getElementById('deselect-all').addEventListener('click', () => { boxes.forEach(box => { box.checked = false; }); update(); });
    boxes.forEach(box => box.addEventListener('change', update));
    update();
  }
  document.querySelectorAll('form[data-confirm]').forEach(form => form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  }));
  const jobs = [...document.querySelectorAll('[data-scan-url]')];
  let delay = 3000;
  async function poll() {
    if (document.hidden) { setTimeout(poll, 10000); return; }
    let terminal = false;
    for (const node of jobs) {
      try {
        const response = await fetch(node.dataset.scanUrl, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
        if (response.redirected || response.status === 403) return;
        if (!response.ok) { delay = Math.min(delay * 2, 30000); continue; }
        const data = await response.json();
        node.textContent = data.label;
        node.title = data.error || '';
        if (data.status === 'complete' || data.status === 'failed') terminal = true;
      } catch { delay = Math.min(delay * 2, 30000); }
    }
    if (terminal) { window.location.reload(); return; }
    setTimeout(poll, delay);
  }
  if (jobs.length) setTimeout(poll, delay);
})();
