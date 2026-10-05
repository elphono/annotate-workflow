/*
 * annotate overlay: Alt+click on the document pins a numbered note there.
 *
 * Injected by the daemon at the end of <body> (see htmldoc.inject_overlay),
 * with the document id in `data-annotate-doc`. Vanilla JS, no build step.
 *
 * The model is `annotations`: ALWAYS the full list, orphans included. Every
 * change PUTs that whole list, so an annotation whose anchor no longer
 * resolves must stay in the model, or the next save would delete it in
 * silence. Orphans are listed in the bar with their quote instead of a pin.
 *
 * The UI lives in a shadow root: the documents style `button`, `textarea`
 * and `div` freely, and none of it must reach the bar or the bubble.
 */
(() => {
  'use strict';
  const tag = document.currentScript ||
    document.querySelector('script[data-annotate-doc]');
  const DOC = tag && tag.getAttribute('data-annotate-doc');
  if (!DOC || window.__annotateLoaded) return;
  window.__annotateLoaded = true;

  const API = '/api/docs/' + encodeURIComponent(DOC);
  const HEADERS = { 'Content-Type': 'application/json', 'X-Annotate': 'overlay' };
  const QUOTE_MAX = 120;
  const SELECTION_MAX = 600;
  const STATUS_TEXT = {
    new: 'no note yet', annotated: 'notes waiting', sent: 'session running',
    answered: 'answered',
  };

  let annotations = [];
  let entry = null;
  let message = '';
  let editing = null;          // { item, isNew }
  let suppressClick = false;
  let collapsed = false;
  let lastStatus = null;
  let saving = Promise.resolve();

  // -- DOM scaffolding -----------------------------------------------------
  const host = document.createElement('annotate-root');
  host.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;' +
    'z-index:2147483000;';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = '<link rel="stylesheet" href="/static/annotate.css">' +
    '<div class="layer"></div><div class="bar" role="region" ' +
    'aria-label="annotations"></div>';
  const layer = root.querySelector('.layer');
  const bar = root.querySelector('.bar');
  document.body.appendChild(host);

  const make = (name, cls, text) => {
    const node = document.createElement(name);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const squash = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const isUi = (event) => event.composedPath().includes(host);

  // -- anchoring -------------------------------------------------------------
  function anchorFor(target) {
    let node = target && target.nodeType === 1 ? target : document.body;
    // Inside an SVG figure, anchor to the outermost <svg>: stable, and its
    // box is what the reader sees.
    let svg = node.closest && node.closest('svg');
    while (svg && svg.parentElement && svg.parentElement.closest('svg')) {
      svg = svg.parentElement.closest('svg');
    }
    if (svg) node = svg;
    if (node === document.documentElement) node = document.body;
    return node;
  }

  function selectorFor(node) {
    const parts = [];
    while (node && node.nodeType === 1 && node !== document.body &&
           node !== document.documentElement) {
      if (node.id && document.querySelectorAll('#' + CSS.escape(node.id)).length === 1) {
        parts.unshift('#' + CSS.escape(node.id));
        return parts.join(' > ');
      }
      let index = 1;
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) {
        if (s.localName === node.localName) index++;
      }
      parts.unshift(node.localName + ':nth-of-type(' + index + ')');
      node = node.parentElement;
    }
    parts.unshift('body');
    return parts.join(' > ');
  }

  function resolve(selector) {
    try {
      return selector ? document.querySelector(selector) : null;
    } catch (err) {
      return null;
    }
  }

  // The nearest box that is actually rendered (a closed <details> hides
  // its content: the pin goes on the visible ancestor).
  function visibleBox(node) {
    for (let n = node; n && n !== document.documentElement; n = n.parentElement) {
      const r = n.getBoundingClientRect();
      if (r.width > 0 || r.height > 0) return r;
    }
    return document.body.getBoundingClientRect();
  }

  // -- persistence -----------------------------------------------------------
  async function call(method, path, body) {
    const response = await fetch(API + path, {
      method, headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch (err) { data = null; }
    if (!response.ok) {
      throw new Error((data && data.error) || ('HTTP ' + response.status));
    }
    return data;
  }

  function persist() {
    const snapshot = annotations.map((a) => ({ ...a }));
    saving = saving.then(async () => {
      try {
        const data = await call('PUT', '/annotations', snapshot);
        annotations = data.annotations;
        message = '';
        await refreshEntry();
      } catch (err) {
        message = 'Not saved: ' + err.message;
      }
      render();
    });
    return saving;
  }

  async function refreshEntry() {
    try {
      entry = await call('GET', '');
    } catch (err) {
      message = 'Daemon unreachable: ' + err.message;
      return;
    }
    const busy = entry.status === 'sent' || entry.running;
    if (lastStatus && (lastStatus === 'sent') && !busy) {
      const data = await call('GET', '/annotations');
      annotations = data.annotations;
      message = entry.status === 'answered'
        ? 'The session answered: reload to see its changes.'
        : 'The session ended without answering: ' + (entry.last_error || 'see the daemon log');
    }
    lastStatus = busy ? 'sent' : entry.status;
  }

  // -- editor bubble -----------------------------------------------------------
  function openEditor(item, isNew, x, y) {
    closeEditor();
    editing = { item, isNew };
    const sent = Boolean(item.sent_at);
    const box = make('div', 'bubble');
    box.style.left = Math.max(8, Math.min(x, window.innerWidth + window.scrollX - 330)) + 'px';
    box.style.top = (y + 14) + 'px';
    const head = make('div', 'bubble-head', '#' + item.number + (sent ? ' · sent' : ''));
    const quote = make('div', 'bubble-quote', item.quote ? '“' + item.quote + '”' : '');
    const area = make('textarea', 'bubble-text');
    area.value = item.note || '';
    area.readOnly = sent;
    area.placeholder = 'Your note (Enter saves, Shift+Enter new line, Esc cancels)';
    const actions = make('div', 'bubble-actions');
    const save = make('button', 'primary', sent ? 'Close' : 'Save');
    const cancel = make('button', '', 'Cancel');
    actions.append(save, cancel);
    if (!isNew) {
      const remove = make('button', 'danger', 'Delete');
      remove.addEventListener('click', () => {
        annotations = annotations.filter((a) => a.id !== item.id);
        closeEditor();
        persist();
      });
      actions.append(remove);
    }
    const commit = () => {
      const text = area.value.trim();
      if (!sent && text) {
        item.note = text;
        if (isNew) annotations.push(item);
        closeEditor();
        persist();
      } else {
        closeEditor();
      }
    };
    save.addEventListener('click', commit);
    cancel.addEventListener('click', closeEditor);
    area.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commit(); }
      if (e.key === 'Escape') { e.preventDefault(); closeEditor(); }
    });
    box.append(head, quote, area, actions);
    layer.append(box);
    // The layer's origin is not the page origin when <body> is positioned.
    const origin = layer.getBoundingClientRect();
    box.style.left = (parseFloat(box.style.left) - window.scrollX - origin.left) + 'px';
    box.style.top = (parseFloat(box.style.top) - window.scrollY - origin.top) + 'px';
    area.focus();
  }

  function closeEditor() {
    editing = null;
    layer.querySelectorAll('.bubble').forEach((b) => b.remove());
    render();
  }

  // -- rendering ------------------------------------------------------------
  // Pins are updated IN PLACE, keyed by annotation id. Recreating them on
  // every resize or poll removed the element under the cursor: a hovered
  // pin vanished without `mouseleave`, and its highlight stuck on the text.
  const pins = new Map();
  let flashed = null;

  function flash(node) {
    if (flashed) flashed.classList.remove('annotate-flash');
    flashed = node;
    if (node) node.classList.add('annotate-flash');
  }

  function pinFor(item) {
    let pin = pins.get(item.id);
    if (!pin) {
      pin = make('button', 'pin');
      pin.dataset.id = item.id;
      pin.addEventListener('click', (e) => {
        e.stopPropagation();
        const current = annotations.find((a) => a.id === pin.dataset.id);
        if (current) openEditor(current, false, e.pageX, e.pageY);
      });
      pin.addEventListener('mouseenter', () => flash(pin.target || null));
      pin.addEventListener('mouseleave', () => flash(null));
      pins.set(item.id, pin);
      layer.append(pin);
    }
    return pin;
  }

  function render() {
    const origin = layer.getBoundingClientRect();
    const orphans = [];
    const placed = new Set();
    for (const item of annotations) {
      const target = resolve(item.selector);
      if (!target) { orphans.push(item); continue; }
      const box = visibleBox(target);
      const dx = Math.min(Math.max(item.offset_x || 0, 0), Math.max(box.width, 0));
      const dy = Math.min(Math.max(item.offset_y || 0, 0), Math.max(box.height, 0));
      const pin = pinFor(item);
      pin.className = 'pin' + (item.sent_at ? ' sent' : '');
      pin.textContent = String(item.number);
      pin.style.left = (box.left - origin.left + dx) + 'px';
      pin.style.top = (box.top - origin.top + dy) + 'px';
      pin.title = item.note;
      pin.target = target;
      placed.add(item.id);
    }
    for (const [id, pin] of pins) {
      if (!placed.has(id)) {
        if (flashed === pin.target) flash(null);
        pin.remove();
        pins.delete(id);
      }
    }
    renderBar(orphans);
  }

  function renderBar(orphans) {
    bar.textContent = '';
    bar.classList.toggle('collapsed', collapsed);
    const pending = annotations.filter((a) => !a.sent_at).length;
    const sent = annotations.length - pending;
    const top = make('div', 'bar-top');
    const counts = make('span', 'counts', pending + ' to send · ' + sent + ' sent');
    const toggle = make('button', 'toggle', collapsed ? '▴' : '▾');
    toggle.title = collapsed ? 'Expand' : 'Collapse';
    toggle.addEventListener('click', () => { collapsed = !collapsed; render(); });
    top.append(counts, toggle);
    bar.append(top);
    if (collapsed) return;

    const busy = entry && (entry.status === 'sent' || entry.running);
    const status = make('div', 'status' + (busy ? ' busy' : ''),
      'Status: ' + (entry ? (busy ? STATUS_TEXT.sent : (STATUS_TEXT[entry.status] || entry.status)) : '…'));
    bar.append(status);

    const actions = make('div', 'bar-actions');
    const send = make('button', 'primary', 'Send to session');
    send.addEventListener('click', () => trigger('send'));
    const fresh = make('button', '', 'New session');
    fresh.title = 'Send the notes to a NEW Claude Code session';
    fresh.addEventListener('click', () => trigger('new-session'));
    actions.append(send, fresh);
    if (message.startsWith('The session answered')) {
      const reload = make('button', '', 'Reload');
      reload.addEventListener('click', () => location.reload());
      actions.append(reload);
    }
    bar.append(actions);

    if (orphans.length) {
      const list = make('div', 'orphans');
      list.append(make('div', 'orphans-title',
        orphans.length + ' note(s) lost their anchor (the passage changed):'));
      for (const item of orphans) {
        const row = make('div', 'orphan' + (item.sent_at ? ' sent' : ''));
        row.dataset.id = item.id;
        row.append(make('span', 'orphan-num', '#' + item.number + ' '),
          make('span', 'orphan-quote', item.quote ? '“' + item.quote.slice(0, 80) + '” ' : ''),
          make('span', 'orphan-note', item.note));
        row.addEventListener('click', (e) => openEditor(item, false, e.pageX - 340, e.pageY - 200));
        list.append(row);
      }
      bar.append(list);
    }
    if (message) bar.append(make('div', 'message', message));
    bar.append(make('div', 'hint', 'Alt+click on the document to annotate'));
  }

  async function trigger(action) {
    await saving;
    try {
      const result = await call('POST', '/' + action);
      message = result.count + ' note(s) sent to ' +
        (result.fresh ? 'a new session' : 'the producing session') + '.';
      lastStatus = 'sent';
      const data = await call('GET', '/annotations');
      annotations = data.annotations;
      await refreshEntry();
    } catch (err) {
      message = err.message;
    }
    render();
  }

  // -- events ---------------------------------------------------------------
  document.addEventListener('mousedown', (e) => {
    if (!e.altKey || e.button !== 0 || isUi(e)) return;
    e.preventDefault();
    e.stopPropagation();
    suppressClick = true;
    const selection = squash(window.getSelection());
    const anchor = anchorFor(e.target);
    const rect = anchor.getBoundingClientRect();
    const id = (window.crypto && crypto.randomUUID)
      ? crypto.randomUUID().replace(/-/g, '').slice(0, 16)
      : Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    const number = annotations.reduce((m, a) => Math.max(m, a.number || 0), 0) + 1;
    const item = {
      id, number, selector: selectorFor(anchor),
      quote: selection ? selection.slice(0, SELECTION_MAX) : squash(anchor.innerText || anchor.textContent).slice(0, QUOTE_MAX),
      offset_x: Math.round(e.clientX - rect.left),
      offset_y: Math.round(e.clientY - rect.top),
      note: '', created_at: new Date().toISOString(), sent_at: '',
    };
    openEditor(item, true, e.pageX, e.pageY);
  }, true);

  document.addEventListener('click', (e) => {
    if (suppressClick || (e.altKey && !isUi(e))) {
      e.preventDefault();
      e.stopPropagation();
      suppressClick = false;
    }
  }, true);

  // The <base> injected for relative files would make `href="#x"` leave the
  // page: route fragment-only links to the current document instead.
  document.addEventListener('click', (e) => {
    if (e.defaultPrevented || isUi(e)) return;
    const link = e.target.closest && e.target.closest('a[href^="#"]');
    if (!link) return;
    e.preventDefault();
    location.hash = link.getAttribute('href');
  });

  let frame = 0;
  const schedule = () => {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; if (!editing) render(); });
  };
  window.addEventListener('resize', schedule);
  window.addEventListener('load', schedule);
  if (window.ResizeObserver) new ResizeObserver(schedule).observe(document.body);

  async function poll() {
    await refreshEntry();
    if (!editing) render();
    const busy = entry && (entry.status === 'sent' || entry.running);
    setTimeout(poll, busy ? 3000 : 15000);
  }

  (async () => {
    try {
      const data = await call('GET', '/annotations');
      annotations = data.annotations;
    } catch (err) {
      message = 'Cannot load the notes: ' + err.message;
    }
    await refreshEntry();
    render();
    const busy = entry && (entry.status === 'sent' || entry.running);
    setTimeout(poll, busy ? 3000 : 15000);
  })();
})();
