"""In-page functions, evaluated per call (never installed as page globals a hostile page could patch)."""

# Shared helpers are inlined into each function so every evaluate() is self-contained.
_HELPERS = r"""
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
const strip = s => norm(s).replace(/:$/, '').trim();
const visible = el => {
  if (!el || !el.getBoundingClientRect) return false;
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return false;
  for (let p = el; p; p = p.parentElement) {
    const cs = getComputedStyle(p);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
  }
  return true;
};
const roleOf = el => {
  const explicit = el.getAttribute('role');
  if (explicit) return explicit;
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || 'text').toLowerCase();
  if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
  if (tag === 'button') return 'button';
  if (tag === 'select') return 'combobox';
  if (tag === 'textarea') return 'textbox';
  if (tag === 'input') {
    if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    return 'textbox';
  }
  if (el.hasAttribute('onclick')) return 'button';
  if (tag === 'td' || tag === 'th') return 'cell';
  return 'generic';
};
const hasControl = el => !!el.querySelector('input:not([type=hidden]),select,button,textarea,a[href]');
const cellLabel = el => {
  const td = el.closest('td,th');
  if (!td) return null;
  for (let c = td.previousElementSibling; c; c = c.previousElementSibling) {
    const t = strip(c.innerText);
    if (t && !hasControl(c)) return t;
  }
  return null;
};
const nameOf = el => {
  const aria = el.getAttribute('aria-label');
  if (aria && norm(aria)) return [norm(aria), 'aria'];
  const lb = el.getAttribute('aria-labelledby');
  if (lb) {
    const t = lb.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(e => norm(e.innerText)).join(' ');
    if (t) return [t, 'aria'];
  }
  if (el.labels && el.labels.length) {
    const t = norm(Array.from(el.labels).map(l => l.innerText).join(' '));
    if (t) return [t, 'label'];
  }
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'input' && ['submit', 'button', 'reset'].includes(type)) {
    const v = norm(el.value || el.getAttribute('value'));
    if (v) return [v, 'value'];
  }
  if (tag === 'a' || tag === 'button' || el.getAttribute('role') === 'button' || el.getAttribute('role') === 'link') {
    const t = norm(el.innerText);
    if (t) return [t, 'content'];
  }
  const title = el.getAttribute('title');
  if (title && norm(title)) return [norm(title), 'title'];
  const ph = el.getAttribute('placeholder');
  if (ph && norm(ph)) return [norm(ph), 'placeholder'];
  const cl = cellLabel(el);
  if (cl) return [cl, 'adjacent_cell'];
  if (tag === 'input' && type === 'checkbox' && el.parentElement) {
    const t = norm(el.parentElement.innerText);
    if (t) return [t, 'content'];
  }
  if (tag === 'td' || tag === 'th') return [norm(el.innerText).slice(0, 80), 'content'];
  return ['', 'none'];
};
const cssPath = el => {
  const parts = [];
  for (let e = el; e && e.nodeType === 1 && e.tagName.toLowerCase() !== 'html'; e = e.parentElement) {
    const tag = e.tagName.toLowerCase();
    if (tag === 'body') { parts.unshift('body'); break; }
    let i = 1;
    for (let s = e.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === e.tagName) i++;
    parts.unshift(`${tag}:nth-of-type(${i})`);
  }
  return parts.join(' > ');
};
const looksGenerated = v => /[0-9a-f]{8,}|\d{4,}|^(ember|react|ng-|mui|css-|jsx-|_)/i.test(v || '');
const describe = el => {
  const [name, source] = nameOf(el);
  const tag = el.tagName.toLowerCase();
  const attrs = {};
  for (const a of ['name', 'id', 'type']) {
    const v = el.getAttribute(a);
    if (v && !(a === 'id' && looksGenerated(v))) attrs[a] = v;
  }
  let formMethod = null, formAction = null, submits = false;
  const form = el.form || el.closest('form');
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (form) {
    formMethod = (form.getAttribute('method') || 'GET').toUpperCase();
    formAction = form.action || location.href;
    submits = (tag === 'input' && ['submit', 'image'].includes(type)) || (tag === 'button' && (type === '' || type === 'submit'));
  }
  const cx = el.getBoundingClientRect();
  const cxX = cx.left + cx.width / 2, cxY = cx.top + cx.height / 2;
  const top = document.elementFromPoint(cxX, cxY);
  const obscured = !!top && top !== el && !el.contains(top) && !top.contains(el);
  return {
    role: roleOf(el), name, name_source: source, tag,
    input_type: tag === 'input' ? (type || 'text') : null,
    value: (tag === 'input' || tag === 'textarea') ? (el.value || '') : (tag === 'select' ? (el.options[el.selectedIndex] ? norm(el.options[el.selectedIndex].text) : '') : null),
    enabled: !el.disabled,
    checked: (tag === 'input' && ['checkbox', 'radio'].includes(type)) ? !!el.checked : null,
    options: tag === 'select' ? Array.from(el.options).map(o => norm(o.text)) : [],
    option_values: tag === 'select' ? Array.from(el.options).map(o => o.value) : [],
    href: (tag === 'a' && el.getAttribute('href') && !el.getAttribute('href').startsWith('javascript:')) ? el.href : null,
    form_method: formMethod, form_action: formAction, submits_form: submits,
    // Adjacent-cell labels are meaningful for form fields and value cells, not for links/buttons.
    anchor_text: (['select', 'textarea', 'td', 'th'].includes(tag) || (tag === 'input' && !['submit', 'button', 'reset', 'image'].includes(type))) ? cellLabel(el) : null,
    attributes: attrs, css_path: cssPath(el),
    text: norm(el.innerText || '').slice(0, 200), obscured,
  };
};
"""

INVENTORY = (
    "() => {"
    + _HELPERS
    + r"""
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [onclick]';
  const els = Array.from(document.querySelectorAll(sel)).filter(visible);
  const items = els.map(describe);
  return { items, els };
}"""
)

DESCRIBE = "(el) => {" + _HELPERS + " return describe(el); }"

LABEL_ANCHOR = (
    "(args) => {"
    + _HELPERS
    + r"""
  const { anchor, control, inputType } = args;
  const want = strip(anchor).toLowerCase();
  const selectors = {
    input: 'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=checkbox]):not([type=radio]):not([type=image]), textarea',
    select: 'select',
    button: 'button, input[type=submit], input[type=button], input[type=reset]',
    link: 'a[href]',
    checkbox: 'input[type=checkbox]',
  };
  const anchors = Array.from(document.querySelectorAll('td,th,label,span,b,font,div'))
    .filter(e => strip(e.innerText).toLowerCase() === want && !hasControl(e) && visible(e));
  const cells = new Set(anchors.map(a => a.closest('td,th') || a));
  const out = [];
  for (const cell of cells) {
    for (let c = cell.nextElementSibling; c; c = c.nextElementSibling) {
      if (control === 'cell') { if (visible(c)) { out.push(c); } break; }
      let found = Array.from(c.querySelectorAll(selectors[control])).filter(visible);
      if (inputType) found = found.filter(e => (e.getAttribute('type') || 'text').toLowerCase() === inputType);
      if (found.length) { out.push(...found); break; }
    }
  }
  return Array.from(new Set(out));
}"""
)

TEXT_TARGETS = (
    "(args) => {"
    + _HELPERS
    + r"""
  const { text, tag } = args;
  const want = norm(text).toLowerCase();
  const pool = Array.from(document.querySelectorAll(tag || 'a,button,td,th,span,div,b,font,label,p'));
  return pool.filter(e => visible(e) && norm(e.innerText).toLowerCase() === want && !Array.from(e.children).some(c => norm(c.innerText).toLowerCase() === want));
}"""
)

SENSITIVE_SCAN = (
    "(args) => {"
    + _HELPERS
    + r"""
  const { labels, patterns } = args;
  const toRect = r => ({ x: r.left, y: r.top, w: r.width, h: r.height });
  const cells = [];
  for (const td of document.querySelectorAll('td,th')) {
    if (hasControl(td)) continue;
    const label = strip(td.innerText);
    if (!label) continue;
    for (const [re, kind] of labels) {
      if (new RegExp(re, 'i').test(label)) {
        const v = td.nextElementSibling;
        // A header row (label-styled neighbour) is not a label/value pair.
        const header = v && (v.tagName === 'TH' || (td.className && v.className === td.className));
        if (v && !header && visible(v) && norm(v.innerText)) cells.push({ rect: toRect(v.getBoundingClientRect()), text: norm(v.innerText), kind });
        break;
      }
    }
  }
  const inputs = [];
  for (const i of document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select')) {
    if (!visible(i)) continue;
    const v = i.tagName.toLowerCase() === 'select' ? (i.options[i.selectedIndex] ? i.options[i.selectedIndex].value : '') : i.value;
    if (!v) continue;
    inputs.push({ rect: toRect(i.getBoundingClientRect()), value: v, type: (i.getAttribute('type') || i.tagName).toLowerCase() });
  }
  const matches = [];
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  const regs = patterns.map(([re, kind]) => [new RegExp(re, 'g'), kind]);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n.nodeValue;
    if (!t || !t.trim() || !n.parentElement || !visible(n.parentElement)) continue;
    for (const [re, kind] of regs) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(t)) !== null) {
        if (!m[0]) { re.lastIndex++; continue; }
        const range = document.createRange();
        range.setStart(n, m.index); range.setEnd(n, m.index + m[0].length);
        for (const r of range.getClientRects()) matches.push({ rect: toRect(r), kind, text: m[0] });
      }
    }
  }
  return { cells, inputs, matches };
}"""
)

PAGE_TEXT = "() => (document.body ? document.body.innerText : '')"

GENERATOR = "() => { const m = document.querySelector('meta[name=generator]'); return m ? m.getAttribute('content') : null; }"

SANITIZED_DOM = (
    "(labels) => {"
    + _HELPERS
    + r"""
  const clone = document.documentElement.cloneNode(true);
  clone.querySelectorAll('input').forEach(i => { if (i.getAttribute('value') !== null || i.type === 'password') i.setAttribute('value', '⟦masked⟧'); });
  for (const td of clone.querySelectorAll('td,th')) {
    const label = norm(td.textContent).replace(/:$/, '');
    for (const [re] of labels) {
      if (label && new RegExp(re, 'i').test(label) && td.nextElementSibling) { td.nextElementSibling.textContent = '⟦masked⟧'; break; }
    }
  }
  return '<!-- sanitized DOM snapshot: input values and label-mapped sensitive cells masked -->\n' + clone.outerHTML;
}"""
)

ELEMENT_AT_POINT = (
    "([x, y]) => {"
    + _HELPERS
    + r"""
  let el = document.elementFromPoint(x, y);
  if (!el) return null;
  const clickable = el.closest('a[href], button, input, select, textarea, [role=button], [role=link], [onclick], label');
  return clickable || el;
}"""
)

# Journal for direct manipulation of the headed window by a human. Installed as an init script in
# every frame; reports through an exposed binding. Attribution comes from the lease, not isTrusted.
HUMAN_JOURNAL_INIT = (
    "(() => {"
    + _HELPERS
    + r"""
  if (window.__cuaJournal) return; window.__cuaJournal = true;
  const send = (kind, el, extra) => {
    try { if (window.cuaHumanEvent) window.cuaHumanEvent({ kind, el: el ? describe(el) : null, url: location.href, frame_name: window === window.top ? null : (window.name || null), ...extra }); } catch (e) {}
  };
  document.addEventListener('click', e => {
    const el = e.target.closest ? (e.target.closest('a[href], button, input, select, textarea, [role=button], [role=link], [onclick], label') || e.target) : e.target;
    send('click', el, {});
  }, true);
  document.addEventListener('change', e => {
    const el = e.target;
    const sensitive = el.type === 'password';
    const v = el.tagName && el.tagName.toLowerCase() === 'select' ? el.value : (el.value || '');
    send(el.type === 'checkbox' ? 'check' : (el.tagName.toLowerCase() === 'select' ? 'select' : 'type'), el,
         { value_len: v.length, value: sensitive ? null : v, checked: el.checked });
  }, true);
})();"""
)

# Input fence for the headed window. Capture-phase listeners on window run before any page or journal
# handler and swallow input unless (a) the automation is dispatching its own action (__cuaAuto, set by
# the adapter around each action) or (b) a human holds a live lease (__cuaHumanUntil, the lease expiry
# in epoch ms, pushed by the handoff manager). Expiry is checked here, in the page, so an expired lease
# closes the fence immediately. New documents start closed.
INPUT_FENCE_INIT = r"""(() => {
  if (window.__cuaFence) return; window.__cuaFence = true;
  window.__cuaAuto = false; window.__cuaHumanUntil = 0;
  let lastReport = 0;
  const open = () => window.__cuaAuto === true || Date.now() < window.__cuaHumanUntil;
  const block = e => {
    if (open()) return;
    e.stopImmediatePropagation(); e.preventDefault();
    if ((e.type === 'pointerdown' || e.type === 'keydown') && Date.now() - lastReport > 1000) {
      lastReport = Date.now();
      try { if (window.cuaHumanEvent) window.cuaHumanEvent({ kind: 'blocked_input', input: e.type, url: location.href }); } catch (err) {}
    }
  };
  for (const t of ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'click', 'dblclick', 'auxclick', 'contextmenu',
                   'keydown', 'keypress', 'keyup', 'beforeinput', 'input', 'change', 'paste', 'cut', 'drop', 'dragstart',
                   'submit', 'touchstart', 'touchend'])
    window.addEventListener(t, block, true);
})();"""

# Headed mode only: a badge telling the person at the window whether their input will land. It lives in
# a closed shadow root on <html> (outside <body>), is aria-hidden and ignores the pointer, so the page's
# text, accessibility tree and hit-testing - everything the automation perceives - are unchanged.
FENCE_BADGE_INIT = r"""(() => {
  if (window !== window.top || window.__cuaBadge) return; window.__cuaBadge = true;
  const mount = () => {
    if (!document.documentElement) return setTimeout(mount, 50);
    const host = document.createElement('cua-fence-badge');
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:2147483647;pointer-events:none';
    const root = host.attachShadow({ mode: 'closed' });
    const b = document.createElement('div');
    b.style.cssText = 'font:12px system-ui,sans-serif;padding:6px 10px;border-radius:6px;color:#fff;box-shadow:0 1px 4px rgba(0,0,0,.3)';
    root.appendChild(b);
    document.documentElement.appendChild(host);
    const tick = () => {
      const human = Date.now() < (window.__cuaHumanUntil || 0);
      b.textContent = human ? 'You are in control (lease live)'
                            : 'Input locked: claim this session in the operator console to take control';
      b.style.background = human ? 'rgba(0,110,40,.9)' : 'rgba(150,0,0,.9)';
    };
    tick(); setInterval(tick, 500);
  };
  mount();
})();"""
