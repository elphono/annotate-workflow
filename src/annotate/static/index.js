/*
 * The index page: every control of the tray icon, in the browser (decision of
 * 2026-10-05: "il faut qu'on ait tous les contrôles qu'on a dans la pastille").
 *
 * Per document: Open and Send to session in sight; under More: New session,
 * Attach to session… and Detach, Unmanage, Delete file. A document without a
 * known session shows "Attach to session…" in sight (decision of 2026-10-08):
 * the picker lists the conversations of this machine (/api/sessions, open ones
 * first), filtered as the user types (title, folder, start of the id), and
 * attaching moves the document into that session's group.
 * For the daemon: Restart and Stop. Start is the one control that cannot live
 * here: a stopped daemon serves no page to click it on; the tray keeps it.
 * For the tracked folders (decision of 2026-10-07): add one under the home
 * directory, remove one, and "Rescan", which registers what was written in
 * the last N days; adding a folder does the same for that folder.
 *
 * Everything goes through the same API as the tray, with the same guard:
 * every POST/DELETE carries `X-Annotate`, and this page's Origin is the
 * daemon's own. Data reaches the DOM through textContent only: a document
 * title is not markup.
 *
 * The list is redrawn only when what it shows has changed, and never while a
 * menu or the picker is open or a click is being handled: a redraw under the
 * cursor would swap the button being pressed.
 */
(() => {
  'use strict';
  const HEADERS = { 'Content-Type': 'application/json', 'X-Annotate': 'index' };
  const REFRESH_MS = 4000;
  const TOAST_MS = 7000;
  const STATUS_TEXT = {
    new: 'no note yet', annotated: 'no note to send',
    delivered: 'delivered, the session is on it', answered: 'answered by the session',
  };
  const $ = (id) => document.getElementById(id);
  const groupsBox = $('groups');
  const daemonLine = $('daemon');
  const daemonText = daemonLine.querySelector('.text');
  const staleBanner = $('stale');
  const toast = $('toast');
  const messageBox = $('message');
  const folderList = $('folder-list');
  const folderInput = $('folder-input');
  const suggestions = $('folder-suggest');
  const daysSelect = $('days');
  const scanState = $('scan-state');
  const filterInput = $('filter');
  const showAll = $('show-all');
  const showPending = $('show-pending');
  const picker = $('picker');
  const pickerDoc = $('picker-doc');
  const pickerSearch = $('picker-search');
  const pickerList = $('picker-list');
  const pickerHint = $('picker-hint');
  const pickerDetach = $('picker-detach');
  let busy = false;
  let home = '';
  let shownFolders = '';
  let shownDocs = '';
  let lastData = null;
  let filterText = '';
  let onlyPending = false;
  let toastTimer = 0;

  const make = (name, cls, text) => {
    const node = document.createElement(name);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const plural = (count, word) => count + ' ' + word + (count === 1 ? '' : 's');

  // `~/x` for the user, the absolute path for the daemon.
  const tilde = (path) => (home && path && (path === home || path.startsWith(home + '/'))
    ? '~' + path.slice(home.length) : (path || ''));

  function ago(epoch) {
    if (!epoch) return '';
    const seconds = Math.max(0, Date.now() / 1000 - epoch);
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' min ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + ' h ago';
    if (seconds < 2 * 86400) return 'yesterday';
    if (seconds < 7 * 86400) return Math.floor(seconds / 86400) + ' days ago';
    return new Date(epoch * 1000).toLocaleDateString(undefined,
      { day: 'numeric', month: 'short', year: 'numeric' });
  }

  function say(text, error) {
    clearTimeout(toastTimer);
    messageBox.textContent = text || '';
    toast.classList.toggle('error', Boolean(error));
    toast.classList.toggle('show', Boolean(text));
    if (text && !error) toastTimer = setTimeout(() => toast.classList.remove('show'), TOAST_MS);
  }
  $('toast-close').addEventListener('click', () => toast.classList.remove('show'));

  async function call(method, path, body) {
    const init = { method, headers: HEADERS };
    if (body !== undefined) init.body = JSON.stringify(body);
    const response = await fetch(path, init);
    let data = null;
    try { data = await response.json(); } catch (err) { data = null; }
    if (!response.ok) throw new Error((data && data.error) || ('HTTP ' + response.status));
    return data;
  }

  // -- menus ------------------------------------------------------------------
  const menuOpen = () => Boolean(document.querySelector('details.more[open]'));
  function closeMenus(except) {
    for (const menu of document.querySelectorAll('details.more[open]')) {
      if (menu !== except) menu.open = false;
    }
  }
  document.addEventListener('click', (e) => {
    closeMenus(e.target instanceof Element ? e.target.closest('details.more') : null);
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenus(); });

  // One click at a time, and the list is refreshed right after: a button
  // that did something must show its effect without waiting for the poll.
  async function act(node, run) {
    if (busy) return;
    busy = true;
    node.disabled = true;
    closeMenus();
    try {
      say(await run(), false);
    } catch (err) {
      say(err.message, true);
    } finally {
      busy = false;
      node.disabled = false;
      await refresh();
    }
  }

  function button(label, cls, title, run) {
    const node = make('button', cls, label);
    node.type = 'button';
    if (title) node.title = title;
    node.addEventListener('click', () => act(node, run));
    return node;
  }

  function plain(label, cls, title, onClick) {
    const node = make('button', cls, label);
    node.type = 'button';
    if (title) node.title = title;
    node.addEventListener('click', onClick);
    return node;
  }

  // -- documents --------------------------------------------------------------
  function shortPath(doc) {
    const cwd = doc.cwd || '';
    return cwd && doc.path.startsWith(cwd + '/') ? doc.path.slice(cwd.length + 1) : tilde(doc.path);
  }

  function docRow(doc) {
    const pending = doc.pending || 0;
    const row = make('article', 'doc');
    row.dataset.id = doc.id;
    if (!doc.exists) row.classList.add('missing');
    else if (pending) row.classList.add('pending');
    else if (doc.status === 'delivered') row.classList.add('delivered');

    const main = make('div', 'doc-main');
    const line = make('div', 'doc-line');
    const link = make('a', 'title', doc.title || doc.path);
    link.href = '/docs/' + encodeURIComponent(doc.id);
    link.target = '_blank';
    link.rel = 'noopener';
    line.append(link);
    if (pending) {
      line.append(make('span', 'badge accent', plural(pending, 'note') + ' to send'));
    } else {
      line.append(make('span', doc.status === 'delivered' ? 'badge info' : 'badge',
                       STATUS_TEXT[doc.status] || doc.status));
    }
    if (doc.listening) line.append(make('span', 'badge live', 'session listening'));
    if (!doc.exists) line.append(make('span', 'badge danger', 'file missing'));
    const meta = make('div', 'meta');
    const where = make('span', 'mono', shortPath(doc));
    where.title = doc.path;
    meta.append(where);
    const facts = [];
    if (doc.mtime) facts.push('edited ' + ago(doc.mtime));
    if (doc.annotations) facts.push(plural(doc.annotations, 'note') + ' in all');
    if (facts.length) meta.append(' · ' + facts.join(' · '));
    main.append(line, meta);
    if (doc.last_error) main.append(make('div', 'problem', 'Last send failed: ' + doc.last_error));

    const api = '/api/docs/' + encodeURIComponent(doc.id);
    const actions = make('div', 'actions');
    actions.append(
      button('Open', '', 'Open the document in a new tab', async () => {
        window.open('/docs/' + encodeURIComponent(doc.id), '_blank');
        return 'Opened ' + (doc.title || doc.path) + '.';
      }),
      button('Send to session', pending ? 'primary' : '',
        doc.listening ? 'An open session listens: the notes go to it'
          : doc.session_id ? 'The notes go to its session: its inbox if it is open, else a terminal tab resumes it'
            : 'No known session: a terminal tab opens a new one (attach it to a session first)',
        async () => (await call('POST', api + '/send')).message));
    const attach = () => plain('Attach to session…', '',
      'Choose the conversation of this machine this document belongs to',
      () => openPicker(doc));
    if (!doc.session_id) actions.append(attach());

    const more = make('details', 'more');
    const summary = make('summary', 'more-toggle', 'More');
    summary.title = 'More actions';
    const menu = make('div', 'menu');
    menu.append(button('New session', '', 'Open the notes in a NEW session, in a terminal tab',
      async () => (await call('POST', api + '/new-session')).message));
    if (doc.session_id) {
      menu.append(attach(), button('Detach', '', 'Forget which session this document belongs to',
        async () => (await call('POST', api + '/session', { session: '' })).message));
    }
    menu.append(make('hr'),
      button('Unmanage', '', 'Stop tracking this document (the file stays)', async () => {
        if (!confirm('Stop tracking this document? The file stays on disk.\n\n' + doc.path)) {
          return '';
        }
        await call('DELETE', api);
        return 'No longer tracked: ' + doc.path;
      }),
      button('Delete file…', 'danger', 'Delete the file from the disk, and stop tracking it',
        async () => {
          if (!confirm('DELETE the file from the disk, and stop tracking it?\n\n' + doc.path)) {
            return '';
          }
          await call('DELETE', api + '?delete=1');
          return 'Deleted: ' + doc.path;
        }));
    more.append(summary, menu);
    more.addEventListener('toggle', () => { if (!more.open && lastData) render(lastData); });
    actions.append(more);
    row.append(main, actions);
    return row;
  }

  const ICONS = {
    // a speech bubble: a conversation; the same bubble struck through: none known
    session: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
    unknown: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/><path d="M9 9l6 6"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    id: '<path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18"/>',
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    repo: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="8" r="2.5"/>'
      + '<path d="M6 8.5v7M18 10.5c0 4-6 3-11 5.5"/>',
  };

  function icon(name) {
    const span = make('span', 'icon');
    span.setAttribute('aria-hidden', 'true');
    span.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
      + 'stroke-linecap="round" stroke-linejoin="round">' + ICONS[name] + '</svg>';
    return span.firstChild;
  }

  function chip(name, text, cls, title) {
    const node = make('span', 'chip' + (cls ? ' ' + cls : ''));
    node.append(icon(name), text);
    if (title) node.title = title;
    return node;
  }

  function groupCard(group, docs) {
    const known = Boolean(group.session_id);
    const section = make('section', 'card group' + (known ? '' : ' unknown')
      + (group.open ? ' open' : ''));
    section.dataset.session = group.session_id;
    const head = make('header', 'group-head');
    const badge = make('span', 'group-icon');
    badge.append(icon(known ? 'session' : 'unknown'));
    const kicker = make('div', 'group-kicker',
      !known ? 'No known session' : group.open ? 'Open session' : 'Session');
    if (group.open) kicker.title = 'The session is open: Send to session posts into it';
    const meta = make('div', 'group-meta');
    if (known) {
      // The repository section above already names the folder; only a session
      // run in a subfolder of it says which one.
      const cwd = docs.length ? docs[0].cwd || '' : '';
      const repo = group.repo || '';
      const folder = cwd && repo && cwd.startsWith(repo + '/') ? cwd.slice(repo.length + 1)
        : cwd && cwd !== repo ? tilde(cwd) : '';
      if (folder) meta.append(chip('folder', folder, 'mono', 'The folder the session runs in: ' + cwd));
      meta.append(chip('id', group.session_id.slice(0, 8), 'mono', group.session_id));
      meta.append(chip('doc', plural(docs.length, 'document')));
    } else {
      meta.textContent = 'Attach each one to its conversation: otherwise Send to session ' +
        'opens a new one.';
    }
    head.append(badge, kicker, make('h3', '', group.title), meta);
    section.append(head);
    for (const doc of docs) section.append(docRow(doc));
    return section;
  }

  function repoSection(repo, cards, sessions, documents) {
    const section = make('section', 'repo' + (repo.is_repo ? '' : ' plain'));
    section.dataset.repo = repo.path;
    const head = make('header', 'repo-head');
    const mark = make('span', 'repo-icon');
    mark.append(icon(repo.is_repo ? 'repo' : 'folder'));
    const name = make('h2', '', repo.label);
    name.title = repo.path;
    const kicker = make('span', 'repo-kicker', repo.is_repo ? 'Repository' : 'Folder');
    if (!repo.is_repo) kicker.title = 'No git repository holds this folder';
    const facts = make('span', 'repo-facts',
      plural(sessions, 'session') + ' · ' + plural(documents, 'document'));
    head.append(mark, kicker, name, facts);
    const body = make('div', 'repo-sessions');
    body.append(...cards);
    section.append(head, body);
    return section;
  }

  function emptyState(title, lines, action) {
    const box = make('div', 'card empty');
    box.append(make('h2', '', title));
    for (const text of lines) box.append(make('p', '', text));
    if (action) box.append(action);
    return box;
  }

  function firstSteps() {
    const steps = make('ol');
    const alt = make('li');
    alt.append('Open it from here, then ', make('kbd', '', 'Alt'), '+click a passage to pin a note.');
    steps.append(make('li', '', 'Ask a Claude Code session for an HTML document in a tracked folder.'),
      alt, make('li', '', 'Send to session: the notes reach the conversation that wrote it.'));
    return steps;
  }

  function renderOverview(data) {
    const docs = data.docs || [];
    const pending = docs.reduce((sum, d) => sum + (d.pending || 0), 0);
    const open = (data.sessions || []).filter((g) => g.open).length;
    const stat = (id, value, cls) => {
      const node = $(id);
      node.querySelector('.value').textContent = String(value);
      node.className = 'stat' + (value ? ' ' + cls : '');
    };
    stat('stat-docs', docs.length, '');
    stat('stat-pending', pending, 'accent');
    stat('stat-open', open, 'live');
    showPending.textContent = pending ? 'Notes to send (' + pending + ')' : 'Notes to send';
  }

  function visible(doc, group) {
    if (onlyPending && !doc.pending) return false;
    if (!filterText) return true;
    const text = [doc.title, doc.path, group.title, group.session_id, group.repo_label]
      .join(' ').toLowerCase();
    return filterText.split(/\s+/).every((word) => text.includes(word));
  }

  function render(data) {
    lastData = data;
    daemonLine.className = 'up';
    daemonText.textContent = 'Daemon up on port ' + data.port;
    // The page is read from disk at every request, the daemon's Python once:
    // after an update, it is older than this page until restarted. A daemon
    // that does not say `stale` at all predates the check, so it is too.
    staleBanner.hidden = data.stale === false;
    renderOverview(data);
    if (busy || menuOpen() || picker.open) return;
    const key = JSON.stringify([data.docs, data.sessions, filterText, onlyPending, home,
                                Math.floor(Date.now() / 60000)]);
    if (key === shownDocs) return;
    shownDocs = key;
    const docs = new Map((data.docs || []).map((d) => [d.id, d]));
    groupsBox.textContent = '';
    if (!docs.size) {
      groupsBox.append(emptyState('No document yet', [
        'A document appears here as soon as a session writes it.',
      ], firstSteps()));
      groupsBox.lastChild.append(make('p', '',
        'Written before the daemon started? Rescan all folders, in the side panel.'));
      return;
    }
    // Sessions gathered by the repository they work in, in the order of their
    // most recent session; the documents without a known session come last.
    let shown = 0;
    const repos = new Map();
    const items = [];                 // repository buckets, and cards with no repository known
    let unknown = null;
    for (const group of data.sessions || []) {
      const members = group.docs.map((id) => docs.get(id)).filter(Boolean)
        .filter((doc) => visible(doc, group));
      if (!members.length) continue;
      shown += members.length;
      if (!group.session_id) {
        unknown = groupCard(group, members);
        continue;
      }
      if (!('repo' in group)) {        // a daemon older than this page: no repository to name
        items.push(groupCard(group, members));
        continue;
      }
      const key = group.repo || group.session_id;
      if (!repos.has(key)) {
        repos.set(key, { repo: { path: group.repo, label: group.repo_label || group.folder,
                                 is_repo: group.is_repo }, cards: [], documents: 0 });
        items.push(repos.get(key));
      }
      const bucket = repos.get(key);
      bucket.cards.push(groupCard(group, members));
      bucket.documents += members.length;
    }
    for (const item of items) {
      groupsBox.append(item instanceof Node ? item
        : repoSection(item.repo, item.cards, item.cards.length, item.documents));
    }
    if (unknown) groupsBox.append(unknown);
    if (!shown) {
      const reset = plain('Show every document', '', '', () => {
        filterInput.value = '';
        filterText = '';
        setPendingOnly(false);
      });
      groupsBox.append(emptyState(onlyPending && !filterText
        ? 'No note is waiting to be sent' : 'No document matches',
      [onlyPending && !filterText ? 'Every note has been sent.'
        : 'Nothing in the titles, files or sessions matches “' + filterInput.value.trim() + '”.'],
      reset));
    }
  }

  function setPendingOnly(value) {
    onlyPending = value;
    showAll.setAttribute('aria-pressed', String(!value));
    showPending.setAttribute('aria-pressed', String(value));
    if (lastData) render(lastData);
  }
  showAll.addEventListener('click', () => setPendingOnly(false));
  showPending.addEventListener('click', () => setPendingOnly(true));
  filterInput.addEventListener('input', () => {
    filterText = filterInput.value.trim().toLowerCase();
    if (lastData) render(lastData);
  });

  // -- the session picker -----------------------------------------------------
  let pickerFor = null;
  let pickerSessions = [];
  let pickerShown = [];
  let pickerLimit = 0;
  let pickerActive = 0;

  async function openPicker(doc) {
    closeMenus();
    pickerFor = doc;
    pickerDoc.textContent = (doc.title || doc.path) + (doc.session_id
      ? ' · attached to session ' + doc.session_id.slice(0, 8) : ' · no known session');
    pickerSearch.value = '';
    pickerDetach.hidden = !doc.session_id;
    pickerSessions = [];
    pickerShown = [];
    pickerActive = 0;
    pickerHint.className = 'hint';
    pickerHint.textContent = '↑ ↓ to move, Enter to attach, Esc to close';
    pickerList.textContent = '';
    pickerList.append(make('p', 'picker-empty', 'Reading the conversations of this machine…'));
    if (!picker.open) picker.showModal();
    pickerSearch.focus();
    try {
      const data = await call('GET', '/api/sessions');
      if (pickerFor !== doc) return;
      pickerSessions = data.sessions || [];
      pickerLimit = data.limit || 0;
      renderPicker();
    } catch (err) {
      pickerList.textContent = '';
      pickerList.append(make('p', 'picker-empty', 'Cannot list the sessions: ' + err.message));
    }
  }

  function matches(session, words) {
    const title = (session.title || '').toLowerCase();
    const folder = (session.cwd || '').toLowerCase();
    const short = tilde(session.cwd || '').toLowerCase();
    const id = session.id.toLowerCase();
    return words.every((w) => title.includes(w) || folder.includes(w) || short.includes(w) ||
                              id.startsWith(w));
  }

  function optionRow(session, index) {
    const option = make('button', 'option');
    option.type = 'button';
    option.id = 'picker-option-' + index;
    option.setAttribute('role', 'option');
    option.dataset.session = session.id;
    const line = make('span', 'line');
    line.append(make('span', session.title ? 'name' : 'name untitled',
                     session.title || 'Untitled session'));
    if (session.open) line.append(make('span', 'badge live', 'open'));
    if (pickerFor && session.id === pickerFor.session_id) line.append(make('span', 'badge', 'current'));
    if (pickerFor && session.cwd && pickerFor.path.startsWith(session.cwd + '/')) {
      line.append(make('span', 'badge', 'same folder'));
    }
    const sub = make('span', 'sub');
    sub.append(make('span', 'mono', tilde(session.cwd) || 'folder unknown'),
      ' · active ' + ago(session.last_active) + ' · ', make('span', 'mono id', session.id.slice(0, 8)));
    option.append(line, sub);
    option.addEventListener('click', () => pick(session.id));
    option.addEventListener('mousemove', () => { if (pickerActive !== index) setActive(index); });
    return option;
  }

  function setActive(index) {
    pickerActive = index;
    pickerList.querySelectorAll('.option').forEach((node, i) => {
      node.classList.toggle('active', i === index);
      node.setAttribute('aria-selected', String(i === index));
      if (i === index) {
        pickerSearch.setAttribute('aria-activedescendant', node.id);
        node.scrollIntoView({ block: 'nearest' });
      }
    });
  }

  function renderPicker() {
    const words = pickerSearch.value.toLowerCase().split(/\s+/).filter(Boolean);
    pickerShown = pickerSessions.filter((s) => matches(s, words));
    pickerList.textContent = '';
    if (!pickerSessions.length) {
      pickerList.append(make('p', 'picker-empty', 'No conversation of this machine can be ' +
        'resumed yet: start one with claude, then come back.'));
    } else if (!pickerShown.length) {
      pickerList.append(make('p', 'picker-empty',
        'No session matches “' + pickerSearch.value.trim() + '”.'));
    }
    pickerShown.forEach((session, index) => pickerList.append(optionRow(session, index)));
    if (pickerShown.length) setActive(Math.min(pickerActive, pickerShown.length - 1));
    else pickerSearch.removeAttribute('aria-activedescendant');
    pickerHint.className = 'hint';
    pickerHint.textContent = pickerShown.length + ' of ' + plural(pickerSessions.length, 'session') +
      (pickerLimit && pickerSessions.length >= pickerLimit
        ? ' (the ' + pickerLimit + ' most recent)' : '') + ' · ↑ ↓ Enter';
  }

  async function pick(session) {
    const doc = pickerFor;
    if (busy || !doc) return;
    busy = true;
    try {
      const answer = await call('POST', '/api/docs/' + encodeURIComponent(doc.id) + '/session',
                                { session });
      picker.close();
      say(answer.message, false);
    } catch (err) {
      // The toast lies under the modal dialog: the error is shown in it.
      pickerHint.className = 'hint problem';
      pickerHint.textContent = err.message;
    } finally {
      busy = false;
      await refresh();
    }
  }

  pickerSearch.addEventListener('input', () => { pickerActive = 0; renderPicker(); });
  pickerSearch.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!pickerShown.length) return;
      const step = e.key === 'ArrowDown' ? 1 : -1;
      setActive((pickerActive + step + pickerShown.length) % pickerShown.length);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (pickerShown[pickerActive]) pick(pickerShown[pickerActive].id);
    }
  });
  pickerDetach.addEventListener('click', () => pick(''));
  $('picker-cancel').addEventListener('click', () => picker.close());
  picker.addEventListener('click', (e) => { if (e.target === picker) picker.close(); });
  picker.addEventListener('close', () => {
    pickerFor = null;
    if (lastData) render(lastData);
  });

  // -- tracked folders --------------------------------------------------------
  function renderFolders(data) {
    home = data.home || home;
    const scan = data.scan || {};
    scanState.className = '';
    if (scan.running) {
      scanState.textContent = 'Rescan running (' + scan.running.days + ' day(s)' +
        (scan.running.folder ? ', ' + tilde(scan.running.folder) : '') + ')…';
    } else if (scan.last) {
      if (scan.last.error) scanState.className = 'error';
      scanState.textContent = scan.last.error ? 'Last rescan failed: ' + scan.last.error
        : 'Last rescan: ' + scan.last.added + ' document(s) added, ' +
          new Date(scan.last.ended_at).toLocaleTimeString();
    } else {
      scanState.textContent = '';
    }
    // Rebuilt only when the list changes: a rebuild under the cursor would
    // swap the Remove button being pressed.
    const key = JSON.stringify(data.folders);
    if (key === shownFolders) return;
    shownFolders = key;
    folderList.textContent = '';
    if (!data.folders.length) {
      folderList.append(make('p', 'meta', 'No folder tracked: nothing new will be listed.'));
    }
    for (const folder of data.folders) {
      const row = make('div', 'tracked');
      row.dataset.path = folder.path;
      const rule = make('span', 'rule', folder.docs_only ? 'only under a docs/ folder' : 'every .html');
      if (!folder.exists) rule.append(make('span', 'badge danger', 'folder missing'));
      row.append(make('code', '', tilde(folder.path)), rule,
        button('Remove', 'ghost', 'Stop tracking this folder (its documents stay listed)',
          async () => {
            if (!confirm('Stop tracking this folder? Documents already listed stay.\n\n' +
                         folder.path)) return '';
            await call('DELETE', '/api/folders?path=' + encodeURIComponent(folder.path));
            return 'No longer tracked: ' + tilde(folder.path);
          }));
      folderList.append(row);
    }
  }

  async function refresh() {
    try {
      const [docs, tracked] = await Promise.all([call('GET', '/api/docs'),
                                                call('GET', '/api/folders')]);
      renderFolders(tracked);
      render(docs);
    } catch (err) {
      daemonLine.className = 'down';
      daemonText.textContent = 'Daemon unreachable (' + err.message + '): start it from the ' +
        'tray icon, Daemon > start';
    }
  }

  const restart = (e) => act(e.currentTarget, async () => {
    await call('POST', '/api/daemon/restart');
    return 'Restart asked to systemd: the page comes back in a few seconds.';
  });
  $('restart').addEventListener('click', restart);
  $('stale-restart').addEventListener('click', restart);
  $('stop').addEventListener('click', (e) => act(e.currentTarget, async () => {
    if (!confirm('Stop the daemon? This page will stop working until it is started again ' +
                 'from the tray icon (Daemon > start).')) return '';
    await call('POST', '/api/daemon/stop');
    return 'Stop asked to systemd. Start it again from the tray icon (Daemon > start).';
  }));

  $('add-folder').addEventListener('submit', (e) => {
    e.preventDefault();
    act($('add'), async () => {
      const days = Number(daysSelect.value);
      const answer = await call('POST', '/api/folders', { path: folderInput.value, days });
      folderInput.value = '';
      return 'Tracked: ' + tilde(answer.folder.path) + (answer.started
        ? '. Its files of the last ' + days + ' day(s) are being registered.'
        : '. A rescan was already running: rescan once it ends to take its recent files.');
    });
  });
  $('rescan').addEventListener('click', (e) => act(e.currentTarget, async () => {
    const answer = await call('POST', '/api/scan', { days: Number(daysSelect.value) });
    return answer.started ? 'Rescan started: files written in the last ' + answer.days +
      ' day(s), in every tracked folder.' : 'A rescan is already running.';
  }));

  // Subfolders as the user types, written the way the user writes them.
  let suggestTimer = 0;
  folderInput.addEventListener('input', () => {
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(async () => {
      try {
        const answer = await call('GET', '/api/folders/suggest?path=' +
                                  encodeURIComponent(folderInput.value || '~/'));
        const typedTilde = (folderInput.value || '~').startsWith('~');
        suggestions.textContent = '';
        for (const path of answer.folders) {
          const option = document.createElement('option');
          option.value = (typedTilde ? tilde(path) : path) + '/';
          suggestions.append(option);
        }
      } catch (err) { /* suggestions are a convenience */ }
    }, 150);
  });

  setInterval(() => { if (!busy) refresh(); }, REFRESH_MS);
  refresh();
})();
