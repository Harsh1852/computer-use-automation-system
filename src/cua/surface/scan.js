// Perception for one document.
//
// Returns { nodes, elements, dialogs, banners } where `nodes[i]` describes
// `elements[i]`, so the Python side can keep a ref -> element mapping without
// mutating the page or inventing test ids.
//
// Why this exists rather than the driver's own accessibility snapshot: we need
// four things at once — a role and an accessible name, coverage of *every*
// frame, a stable handle back to the element, and a shape a UIA or AX surface
// could populate identically. No single driver API gives all four, and the
// driver's a11y snapshot gives neither frame traversal nor element mapping.
// So the surface computes the same information the accessibility tree would,
// using the same inputs (ARIA attributes, label association, implicit roles).
() => {
  const SKIP = new Set([
    'SCRIPT', 'STYLE', 'HEAD', 'META', 'LINK', 'TITLE', 'NOSCRIPT', 'BASE',
    'FRAMESET', 'FRAME', 'IFRAME', 'HTML', 'BODY', 'BR', 'HR'
  ]);
  const INTERACTIVE = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA']);
  const TEXTUAL = new Set([
    'TD', 'TH', 'LABEL', 'LEGEND', 'CAPTION', 'LI', 'P',
    'H1', 'H2', 'H3', 'H4', 'H5', 'H6'
  ]);
  const LEAFY = new Set(['SPAN', 'FONT', 'B', 'I', 'EM', 'STRONG', 'DIV', 'PRE']);
  const GROUPING = new Set(['row', 'dialog', 'form', 'table']);
  const TEXT_CONTAINER_SEL = 'td,th,label,legend,caption,li,p,h1,h2,h3,h4,h5,h6';
  const FOCUSABLE_SEL = 'input,select,textarea,button,a[href]';

  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const attr = (el, n) => (el.getAttribute ? norm(el.getAttribute(n)) : '');

  // Hidden nodes are dropped here, at the perception boundary, and never
  // reach the resolver. A hidden __VIEWSTATE input is still a textbox in the
  // raw tree; leaving it in would pollute the match counts that the
  // "ambiguity is a miss" rule depends on, and make an unambiguous target
  // look ambiguous.
  function visible(el) {
    if (el.hasAttribute && el.hasAttribute('hidden')) return false;
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'hidden') return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    return true;
  }

  function roleOf(el) {
    const explicit = attr(el, 'role');
    if (explicit) return explicit.toLowerCase();
    switch (el.tagName) {
      case 'A': return el.hasAttribute('href') ? 'link' : 'generic';
      case 'BUTTON': return 'button';
      case 'SELECT': return el.multiple ? 'listbox' : 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'OPTION': return 'option';
      case 'TABLE': return 'table';
      case 'TR': return 'row';
      case 'TD': return 'cell';
      case 'TH': return 'columnheader';
      case 'IMG': return 'img';
      case 'FORM': return 'form';
      case 'LABEL': return 'generic';
      case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6':
        return 'heading';
      case 'INPUT': {
        const t = (el.type || 'text').toLowerCase();
        if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
        if (t === 'checkbox') return 'checkbox';
        if (t === 'radio') return 'radio';
        return 'textbox';
      }
      default: return 'generic';
    }
  }

  function labelFor(el) {
    if (el.id) {
      const esc = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id;
      const l = el.ownerDocument.querySelector('label[for="' + esc + '"]');
      if (l) return norm(l.innerText || l.textContent);
    }
    const wrap = el.closest ? el.closest('label') : null;
    if (wrap) return norm(wrap.innerText || wrap.textContent);
    return '';
  }

  // Returns [name, source]. `source` is what lets label_text be a genuinely
  // different rung of the ladder from a11y_role_name.
  function accName(el, role) {
    const aria = attr(el, 'aria-label');
    if (aria) return [aria, 'aria'];

    const ids = attr(el, 'aria-labelledby');
    if (ids) {
      const joined = norm(ids.split(/\s+/)
        .map((id) => el.ownerDocument.getElementById(id))
        .filter(Boolean)
        .map((n) => norm(n.innerText || n.textContent))
        .join(' '));
      if (joined) return [joined, 'aria'];
    }

    const tag = el.tagName;
    if (tag === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset') return [norm(el.value), 'value'];
      if (t === 'image') return [attr(el, 'alt'), 'alt'];
    }
    if (tag === 'IMG') return [attr(el, 'alt'), 'alt'];

    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
      const lbl = labelFor(el);
      if (lbl) return [lbl, 'label'];
      const ph = attr(el, 'placeholder');
      if (ph) return [ph, 'title'];
      const ti = attr(el, 'title');
      if (ti) return [ti, 'title'];
      // The legacy case: a bare input whose label is adjacent table text.
      // It genuinely has no accessible name, and saying so is the point.
      return ['', 'none'];
    }

    if (tag === 'BUTTON' || tag === 'A' || TEXTUAL.has(tag) || LEAFY.has(tag)
        || tag === 'TR' || tag === 'OPTION') {
      const t = norm(el.innerText || el.textContent);
      if (t) return [t, 'text'];
    }
    const ti = attr(el, 'title');
    if (ti) return [ti, 'title'];
    return ['', 'none'];
  }

  function valueOf(el) {
    const tag = el.tagName;
    if (tag === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox' || t === 'radio') return el.checked ? 'true' : 'false';
      if (t === 'submit' || t === 'button' || t === 'reset') return null;
      return el.value == null ? null : String(el.value);
    }
    if (tag === 'TEXTAREA') return el.value == null ? null : String(el.value);
    if (tag === 'SELECT') return el.value == null ? null : String(el.value);
    return null;
  }

  function emits(el) {
    const tag = el.tagName;
    if (SKIP.has(tag)) return false;
    if (INTERACTIVE.has(tag)) {
      if (tag === 'A') return el.hasAttribute('href') || el.hasAttribute('onclick');
      return true;
    }
    if (tag === 'OPTION') return false; // reachable through its select
    // Layout tables nest three deep in this class of application. A row whose
    // every cell just holds another table is chrome, not data: emitting it
    // would duplicate the text of everything inside it and create phantom
    // anchors that near_text could latch onto.
    if (tag === 'TR') {
      if (norm(el.innerText || el.textContent) === '') return false;
      const cells = Array.from(el.children).filter(
        (c) => c.tagName === 'TD' || c.tagName === 'TH');
      if (cells.length === 0) return false;
      return !cells.every((c) => c.querySelector('table'));
    }
    if (TEXTUAL.has(tag)) {
      if (el.querySelector(FOCUSABLE_SEL)) return false; // it is a wrapper, not a label
      if (el.querySelector('table')) return false;       // ditto
      return norm(el.innerText || el.textContent) !== '';
    }
    if (LEAFY.has(tag)) {
      if (el.querySelector('*')) return false;           // only innermost text
      if (norm(el.textContent) === '') return false;
      // Skip it only if the cell around it is itself being emitted and will
      // therefore carry this text. When the enclosing cell is a layout
      // wrapper we dropped, the leaf is the *only* carrier of the text — and
      // that is exactly where this application puts its error messages.
      const container = el.closest(TEXT_CONTAINER_SEL);
      return !(container && emits(container));
    }
    return false;
  }

  const all = Array.from(document.querySelectorAll('*'));
  const elements = [];
  const nodes = [];
  const indexOfEl = new Map();

  for (const el of all) {
    if (!emits(el) || !visible(el)) continue;
    const role = roleOf(el);
    const [name, source] = accName(el, role);
    const r = el.getBoundingClientRect();
    const idx = elements.length;
    indexOfEl.set(el, idx);
    elements.push(el);
    nodes.push({
      role: role,
      name: name || null,
      name_source: source,
      value: valueOf(el),
      enabled: !(el.disabled === true) && attr(el, 'aria-disabled') !== 'true',
      focused: el === document.activeElement,
      visible: true,
      bbox: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      order: idx,
      dom_id: el.id || null,
      container: null
    });
  }

  // Second pass: nearest emitted grouping ancestor. On the web that is almost
  // always the table row, which is exactly the scope near_text needs.
  for (let i = 0; i < elements.length; i++) {
    let p = elements[i].parentElement;
    while (p) {
      const j = indexOfEl.get(p);
      if (j !== undefined && GROUPING.has(nodes[j].role)) {
        nodes[i].container = j;
        break;
      }
      p = p.parentElement;
    }
  }

  // Structural overlays only. Classifying *which* banner means what is the
  // detectors' job, not perception's.
  const dialogs = [];
  for (const el of all) {
    if (!visible(el)) continue;
    const s = getComputedStyle(el);
    const z = parseInt(s.zIndex, 10);
    const overlay = (s.position === 'fixed' || s.position === 'absolute') && z >= 100;
    if (attr(el, 'role') !== 'dialog' && !overlay) continue;
    const text = norm(el.innerText || el.textContent);
    if (!text) continue;
    if (el.querySelector('[role=dialog]')) continue;
    let dismiss = null;
    for (const b of el.querySelectorAll('button,input[type=button],input[type=submit],a[href]')) {
      const label = norm(b.value || b.innerText || b.textContent);
      if (/^(DISMISS|CLOSE|OK|CONTINUE|ACKNOWLEDGE)$/i.test(label)) {
        const j = indexOfEl.get(b);
        if (j !== undefined) { dismiss = j; break; }
      }
    }
    dialogs.push({ title: norm(el.firstElementChild ? el.firstElementChild.innerText : '') || null,
                   text: text, dismiss: dismiss });
  }

  const banners = [];
  for (const el of document.querySelectorAll('[role=alert],[role=status],[aria-live]')) {
    if (!visible(el)) continue;
    const text = norm(el.innerText || el.textContent);
    if (text) banners.push({ text: text, region: attr(el, 'role') || 'live' });
  }

  return { nodes: nodes, elements: elements, dialogs: dialogs, banners: banners,
           title: document.title || '' };
}
