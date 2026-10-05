/*
 * The index page: every control of the tray icon, in the browser (decision of
 * 2026-10-05: "il faut qu'on ait tous les contrôles qu'on a dans la pastille").
 *
 * Per document: Open, Send to session, New session, Unmanage, Delete file.
 * For the daemon: Restart and Stop. Start is the one control that cannot live
 * here: a stopped daemon serves no page to click it on; the tray keeps it.
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
  let busy = false;

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

  async function call(method, path) {
    const response = await fetch(path, { method, headers: HEADERS });
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
        'No document yet. A session that writes an HTML file under a docs/ folder of the ' +
        'workspace adds it here.'));
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

  async function refresh() {
    try {
      render(await call('GET', '/api/docs'));
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

  // Refresh unless a click is being handled: re-rendering under the cursor
  // would swap the button being pressed.
  setInterval(() => { if (!busy) refresh(); }, REFRESH_MS);
  refresh();
})();
