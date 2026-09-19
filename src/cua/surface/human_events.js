// Capture-phase listeners that report what a person did.
//
// Installed through add_init_script so every frame gets it, including ones
// the operator navigates to after taking control. Capture phase specifically:
// a page that calls stopPropagation in a bubble handler would otherwise
// silence the audit trail, and a legacy app doing that is not hypothetical.
//
// Reports identity, never content. The value never leaves the page — only its
// length does, and not even that for a password field.
(() => {
  if (window.__cuaHumanCaptureInstalled) return;
  window.__cuaHumanCaptureInstalled = true;

  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();

  function roleOf(el) {
    if (!el || !el.tagName) return 'unknown';
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit.toLowerCase();
    switch (el.tagName) {
      case 'A': return 'link';
      case 'BUTTON': return 'button';
      case 'SELECT': return 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'FORM': return 'form';
      case 'TD': return 'cell';
      case 'INPUT': {
        const t = (el.type || 'text').toLowerCase();
        if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
        if (t === 'checkbox') return 'checkbox';
        if (t === 'radio') return 'radio';
        if (t === 'password') return 'textbox';
        return 'textbox';
      }
      default: return 'generic';
    }
  }

  // The same near-text idea the locator ladder uses: a bare input in this
  // application has no accessible name, so the only way to say *which* field
  // the operator touched is the text sitting beside it.
  function nameOf(el) {
    const aria = norm(el.getAttribute && el.getAttribute('aria-label'));
    if (aria) return aria;
    if (el.tagName === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset') return norm(el.value);
    }
    if (el.id) {
      const label = el.ownerDocument.querySelector('label[for="' + el.id + '"]');
      if (label) return norm(label.innerText || label.textContent);
    }
    // Only a *leaf's* own text is a name. Taking innerText from a container
    // returns the whole screen — useless as an audit record, and it would
    // copy whatever record is on display into the evidence log.
    const isLeaf = !el.querySelector || !el.querySelector('*');
    const own = norm(el.innerText || el.textContent);
    if (isLeaf && own && own.length <= 60
        && el.tagName !== 'INPUT' && el.tagName !== 'SELECT') {
      return own;
    }

    const cell = el.closest ? el.closest('td') : null;
    const previous = cell && cell.previousElementSibling;
    if (previous) {
      const near = norm(previous.innerText || previous.textContent);
      if (near) return near.slice(0, 80);
    }
    return null;
  }

  function report(kind, el) {
    if (!el || !window.__cuaHumanEvent) return;
    const isPassword =
      el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'password';
    let length = 0;
    if (el.value != null && typeof el.value === 'string') length = el.value.length;
    try {
      window.__cuaHumanEvent({
        kind: kind,
        role: roleOf(el),
        name: nameOf(el),
        is_password: isPassword,
        value_len: length,
        url: document.location ? document.location.href : null
      });
    } catch (e) {
      /* never let the audit hook break the page the operator is using */
    }
  }

  document.addEventListener('click', (e) => report('click', e.target), true);
  document.addEventListener('change', (e) => report('change', e.target), true);
  document.addEventListener('submit', (e) => report('submit', e.target), true);
})();
