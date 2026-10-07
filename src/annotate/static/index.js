/*
 * The index page: every control of the tray icon, in the browser (decision of
 * 2026-10-05: "il faut qu'on ait tous les contrôles qu'on a dans la pastille").
 *
 * Per document: Open, Send to session, New session, Unmanage, Delete file.
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
 */
(() => {
  'use strict';
  const HEADERS = { 'Content-Type': 'application/json', 'X-Annotate': 'index' };
  const REFRESH_MS = 4000;
  const STATUS_TEXT = {
    new: 'no note yet', annotated: 'notes waiting to be sent',
    delivered: 'notes delivered, the session is on it',
    answered: 'the session edited the document',
  };
  const groupsBox = document.getElementById('groups');
  const daemonLine = document.getElementById('daemon');
  const messageBox = document.getElementById('message');
  const folderList = document.getElementById('folder-list');
  const folderInput = document.getElementById('folder-input');
  const suggestions = document.getElementById('folder-suggest');
  const daysSelect = document.getElementById('days');
  const scanState = document.getElementById('scan-state');
  let busy = false;
  let home = '';
  let shownFolders = '';

  const make = (name, cls, text) => {
    const node = document.createElement(name);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function say(text, error) {
    messageBox.textContent = text;
    messageBox.className = error ? 'error' : '';
  }

  async function call(method, path, body) {
    const init = { method, headers: HEADERS };
    if (body !== undefined) init.body = JSON.stringify(body);
    const response = await fetch(path, init);
    let data = null;
    try { data = await response.json(); } catch (err) { data = null; }
    if (!response.ok) throw new Error((data && data.error) || ('HTTP ' + response.status));
    return data;
  }

  // One click at a time, and the list is refreshed right after: a button
  // that did something must show its effect without waiting for the poll.
  async function act(button, run) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    try {
      say(await run(), false);
    } catch (err) {
      say(err.message, true);
    } finally {
      busy = false;
      button.disabled = false;
      await refresh();
    }
  }

  function button(label, cls, title, run) {
    const node = make('button', cls, label);
    if (title) node.title = title;
    node.addEventListener('click', () => act(node, run));
    return node;
  }

  function docRow(doc) {
    const row = make('div', 'doc');
    row.dataset.id = doc.id;
    const top = make('div', 'top');
    const link = make('a', 'title', doc.title || doc.path);
    link.href = '/docs/' + encodeURIComponent(doc.id);
    link.target = '_blank';
    top.append(link, make('span', 'badge', STATUS_TEXT[doc.status] || doc.status));
    if (doc.listening) top.append(make('span', 'badge live', 'session listening'));
    if (!doc.exists) top.append(make('span', 'badge missing', 'file missing'));
    top.append(make('span', 'meta', doc.pending + ' to send / ' + doc.annotations + ' note(s)'));
    row.append(top, make('div', 'path', doc.path));
    const api = '/api/docs/' + encodeURIComponent(doc.id);
    const actions = make('div', 'actions');
    actions.append(
      button('Open', '', 'Open the document in a new tab', async () => {
        window.open('/docs/' + encodeURIComponent(doc.id), '_blank');
        return 'Opened ' + (doc.title || doc.path) + '.';
      }),
      button('Send to session', 'primary',
        doc.listening ? 'An open session listens: the notes go to it'
                      : 'No open session listens: a terminal tab resumes its session',
        async () => (await call('POST', api + '/send')).message),
      button('New session', '', 'Open the notes in a NEW session, in a terminal tab',
        async () => (await call('POST', api + '/new-session')).message),
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
    row.append(actions);
    return row;
  }

  function render(data) {
    const docs = new Map((data.docs || []).map((d) => [d.id, d]));
    const listening = (data.docs || []).filter((d) => d.listening).length;
    daemonLine.textContent = 'daemon up on :' + data.port + ' · ' + docs.size +
      ' document(s)' + (listening ? ' · ' + listening + ' with a session listening' : '');
    groupsBox.textContent = '';
    if (!docs.size) {
      groupsBox.append(make('p', 'empty',
        'No document yet. A session that writes an HTML file in a tracked folder adds it ' +
        'here; "Rescan all folders" takes the files written recently.'));
      return;
    }
    for (const group of data.sessions || []) {
      const section = make('section');
      section.dataset.session = group.session_id;
      const head = make('h2', '', group.title);
      if (group.folder) head.append(make('span', 'folder', ' · ' + group.folder));
      section.append(head);
      for (const id of group.docs) if (docs.has(id)) section.append(docRow(docs.get(id)));
      groupsBox.append(section);
    }
  }

  // `~/x` for the user, the absolute path for the daemon.
  const tilde = (path) => (home && (path === home || path.startsWith(home + '/'))
    ? '~' + path.slice(home.length) : path);

  function renderFolders(data) {
    home = data.home || home;
    const scan = data.scan || {};
    if (scan.running) {
      scanState.textContent = 'Rescan running (' + scan.running.days + ' day(s)' +
        (scan.running.folder ? ', ' + tilde(scan.running.folder) : '') + ')…';
    } else if (scan.last) {
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
      row.append(make('code', '', tilde(folder.path)),
        make('span', 'badge', folder.docs_only ? 'only under a docs/ folder' : 'every .html'));
      if (!folder.exists) row.append(make('span', 'badge missing', 'folder missing'));
      row.append(button('Remove', '', 'Stop tracking this folder (its documents stay listed)',
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
      render(docs);
      renderFolders(tracked);
    } catch (err) {
      daemonLine.textContent = 'daemon unreachable (' + err.message + '): start it from the ' +
        'tray icon, Daemon > start';
    }
  }

  document.getElementById('restart').addEventListener('click', (e) => act(e.target, async () => {
    await call('POST', '/api/daemon/restart');
    return 'Restart asked to systemd: the page comes back in a few seconds.';
  }));
  document.getElementById('stop').addEventListener('click', (e) => act(e.target, async () => {
    if (!confirm('Stop the daemon? This page will stop working until it is started again ' +
                 'from the tray icon (Daemon > start).')) return '';
    await call('POST', '/api/daemon/stop');
    return 'Stop asked to systemd. Start it again from the tray icon (Daemon > start).';
  }));

  document.getElementById('add-folder').addEventListener('submit', (e) => {
    e.preventDefault();
    act(document.getElementById('add'), async () => {
      const days = Number(daysSelect.value);
      const answer = await call('POST', '/api/folders', { path: folderInput.value, days });
      folderInput.value = '';
      return 'Tracked: ' + tilde(answer.folder.path) + (answer.started
        ? '. Its files of the last ' + days + ' day(s) are being registered.'
        : '. A rescan was already running: rescan once it ends to take its recent files.');
    });
  });
  document.getElementById('rescan').addEventListener('click', (e) => act(e.target, async () => {
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

  // Refresh unless a click is being handled: re-rendering under the cursor
  // would swap the button being pressed.
  setInterval(() => { if (!busy) refresh(); }, REFRESH_MS);
  refresh();
})();
