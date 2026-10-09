// Drives the index page in a real headless Chromium: every tray control, and
// attaching a document to a session through the picker.
//
//   node index_check.mjs <playwright-dir> <base-url> <out-dir> <folder-to-add>
//
// The Python side (tests/test_browser.py) prepares five documents in three
// groups (an open session, another session, no session), the conversations
// the picker offers, and a folder holding a sixth document; it checks what
// reached the registry, the disk, the tracked folders and the daemon.
// Screenshots of the full page are written to <out-dir>, light and dark, at
// desktop and phone width, before anything is changed.
import { createRequire } from 'node:module';
import { join } from 'node:path';

const [pwDir, base, outDir, folder] = process.argv.slice(2);
const require = createRequire(join(pwDir, 'package.json'));
const { chromium } = require('playwright');

const fail = (msg) => { console.error('FAIL ' + msg); process.exit(1); };
const step = (msg) => console.log('OK   ' + msg);

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const dialogs = [];
  page.on('dialog', async (d) => { dialogs.push(d.message()); await d.accept(); });
  await page.goto(base + '/');
  await page.waitForSelector('section .doc');
  await page.waitForFunction(() => document.querySelectorAll('.doc').length === 5);

  const heads = await page.locator('#groups section h2').allTextContents();
  if (heads.length !== 3 || !heads.includes('Live session') || !heads.includes('Other session') ||
      heads[2] !== 'documents without a known session') fail('groups: ' + JSON.stringify(heads));
  step('grouped by session, unknown last: ' + JSON.stringify(heads));
  const group = (title) => page.locator('#groups section', { has: page.locator('h2', { hasText: title }) });
  const row = (title) => page.locator('.doc', { hasText: title });
  const kicker = async (title) => (await group(title).locator('.group-kicker').textContent()).trim();
  if (await kicker('Live session') !== 'Open session' ||
      !(await group('Live session').evaluate((node) => node.classList.contains('open')))) {
    fail('Live session not marked open');
  }
  if (await kicker('Other session') !== 'Session' ||
      await group('Other session').evaluate((node) => node.classList.contains('open'))) {
    fail('Other session shown open');
  }
  step('the open session is marked open, the other one is not');
  if (!(await row('Alpha').locator('.badge.live').count())) fail('no listening badge on Alpha');
  const classes = {};
  for (const title of ['Alpha', 'Charlie', 'Echo', 'Foxtrot']) classes[title] = await row(title).getAttribute('class');
  if (!classes.Alpha.includes('pending') || !classes.Echo.includes('delivered') ||
      !classes.Foxtrot.includes('missing') || classes.Charlie !== 'doc') fail('row states: ' + JSON.stringify(classes));
  if ((await row('Alpha').locator('.badge.accent').textContent()) !== '1 note to send') fail('pending badge');
  if (!(await row('Foxtrot').locator('.badge.danger').count())) fail('no missing-file badge');
  step('states at a glance: ' + JSON.stringify(classes));
  const stats = await page.locator('#overview .value').allTextContents();
  if (JSON.stringify(stats) !== '["5","1","1"]') fail('overview: ' + JSON.stringify(stats));
  step('overview: documents, notes to send, sessions open = ' + stats.join(', '));
  if ((await row('Alpha').getByRole('button', { name: 'Send to session' }).getAttribute('class')) !== 'primary' ||
      (await row('Charlie').getByRole('button', { name: 'Send to session' }).getAttribute('class')) === 'primary') {
    fail('Send to session is the main action only where notes wait');
  }
  step('the main action stands out only where notes wait');

  // Screenshots with every kind of row, before anything changes.
  const fits = async (width) => {
    const wide = await page.evaluate(() => document.documentElement.scrollWidth);
    if (wide > width) fail('horizontal scroll at ' + width + ' px: ' + wide);
  };
  await page.screenshot({ path: join(outDir, 'index-light.png'), fullPage: true });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: join(outDir, 'index-dark.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await fits(390);
  await page.screenshot({ path: join(outDir, 'index-phone-dark.png'), fullPage: true });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.screenshot({ path: join(outDir, 'index-phone.png'), fullPage: true });
  await page.setViewportSize({ width: 1100, height: 900 });
  step('screenshots taken; fits at 390 px');

  // The filter and the "Notes to send" view.
  await page.locator('#filter').fill('echo');
  await page.waitForFunction(() => document.querySelectorAll('.doc').length === 1);
  await page.locator('#filter').fill('');
  await page.locator('#show-pending').click();
  await page.waitForFunction(() => document.querySelectorAll('.doc').length === 1 &&
    document.querySelector('.doc').textContent.includes('Alpha'));
  await page.locator('#show-all').click();
  await page.waitForFunction(() => document.querySelectorAll('.doc').length === 5);
  step('filter by text, and "Notes to send" shows Alpha alone');

  // Attach Bravo, which has no known session, through the picker.
  await row('Bravo').getByRole('button', { name: 'Attach to session…' }).click();
  await page.waitForSelector('#picker[open] .option');
  const options = page.locator('#picker .option');
  const names = await options.locator('.name').allTextContents();
  if (JSON.stringify(names) !== '["Live session","Other session","Untitled session"]') {
    fail('picker: open first, then most recent, never a subagent nor an unresumable one: ' + JSON.stringify(names));
  }
  if (!(await options.nth(0).locator('.badge.live').count())) fail('no open badge in the picker');
  step('picker lists ' + JSON.stringify(names));
  if (await page.evaluate(() => document.activeElement.id) !== 'picker-search') fail('the search field has no focus');
  if (await page.locator('#picker-detach').isVisible()) fail('Detach offered for a document without a session');
  await page.screenshot({ path: join(outDir, 'index-picker.png') });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.screenshot({ path: join(outDir, 'index-picker-dark.png') });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 390, height: 844 });
  await fits(390);
  await page.screenshot({ path: join(outDir, 'index-picker-phone.png') });
  await page.setViewportSize({ width: 1100, height: 900 });
  const typed = async (text, want) => {
    await page.locator('#picker-search').fill(text);
    const got = await options.locator('.name').allTextContents();
    if (JSON.stringify(got) !== JSON.stringify(want)) fail('typing ' + text + ': ' + JSON.stringify(got));
  };
  await typed('third', ['Untitled session']);          // its folder
  await typed('s-unt', ['Untitled session']);          // the start of its id
  await typed('untitled-zzz', []);
  await typed('other', ['Other session']);             // its title
  step('the picker filters on folder, start of id and title');
  await page.locator('#picker-search').press('Enter');
  await page.waitForFunction(() => !document.getElementById('picker').open);
  await page.waitForFunction(() => [...document.querySelectorAll('#groups section')].some((s) =>
    s.querySelector('h2').textContent === 'Other session' && s.textContent.includes('Bravo')));
  const attached = await page.locator('#message').textContent();
  if (!attached.startsWith('Bravo attached to Other session')) fail('attach message: ' + attached);
  step('Enter attached Bravo, which moved to Other session: ' + attached);

  // From the More menu: the current session is marked; Esc closes the picker.
  await row('Charlie').locator('summary.more-toggle').click();
  await row('Charlie').getByRole('button', { name: 'Attach to session…' }).click();
  await page.waitForSelector('#picker[open] .option');
  const current = await page.locator('#picker .option', { has: page.locator('.badge', { hasText: 'current' }) })
    .locator('.name').allTextContents();
  if (JSON.stringify(current) !== '["Live session"]') fail('current session: ' + JSON.stringify(current));
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.getElementById('picker').open);
  if (await group('Live session').locator('.doc', { hasText: 'Charlie' }).count() !== 1) fail('Esc changed something');
  step('the picker marks the current session, Esc closes it and changes nothing');

  await row('Charlie').locator('summary.more-toggle').click();
  await row('Charlie').getByRole('button', { name: 'Attach to session…' }).click();
  await page.waitForSelector('#picker[open] .option');
  await page.locator('#picker-detach').click();
  await page.waitForFunction(() => !document.getElementById('picker').open);
  await page.waitForFunction(() => [...document.querySelectorAll('#groups section')].some((s) =>
    s.querySelector('h2').textContent === 'documents without a known session' && s.textContent.includes('Charlie')));
  step('Detach from the picker: ' + (await page.locator('#message').textContent()));

  await row('Bravo').locator('summary.more-toggle').click();
  await row('Bravo').getByRole('button', { name: 'Detach' }).click();
  await page.waitForFunction(() => [...document.querySelectorAll('#groups section')].some((s) =>
    s.querySelector('h2').textContent === 'documents without a known session' && s.textContent.includes('Bravo')));
  step('Detach: ' + (await page.locator('#message').textContent()));

  await row('Alpha').getByRole('button', { name: 'Send to session' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.includes('delivered to the open session'));
  if (!(await page.locator('#toast.show').count())) fail('the answer of an action is not shown');
  step('send: ' + (await page.locator('#message').textContent()));

  await row('Bravo').locator('summary.more-toggle').click();
  await row('Bravo').getByRole('button', { name: 'Unmanage' }).click();
  await page.waitForFunction(() => !document.getElementById('groups').textContent.includes('Bravo'));
  step('unmanage: row gone after confirm');

  await row('Charlie').locator('summary.more-toggle').click();
  await row('Charlie').getByRole('button', { name: 'Delete file…' }).click();
  await page.waitForFunction(() => !document.getElementById('groups').textContent.includes('Charlie'));
  step('delete: row gone after confirm');

  const tracked = await page.locator('#folder-list .tracked').allTextContents();
  if (tracked.length !== 1 || !tracked[0].includes('only under a docs/ folder')) {
    fail('default folder: ' + JSON.stringify(tracked));
  }
  step('default folder shown: ' + tracked[0]);
  const parent = folder.slice(0, folder.lastIndexOf('/') + 1);
  await page.locator('#folder-input').fill(parent);
  await page.waitForFunction((want) => [...document.querySelectorAll('#folder-suggest option')]
    .some((o) => o.value === want), folder + '/');
  step('subfolders proposed while typing');
  await page.locator('#folder-input').fill(folder);
  await page.getByRole('button', { name: 'Add folder' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.startsWith('Tracked:'));
  await page.waitForFunction(() => document.getElementById('scan-state').textContent.includes('1 document(s) added'));
  await page.waitForFunction(() => document.getElementById('groups').textContent.includes('Delta'));
  step('folder added, its recent document listed: ' + (await page.locator('#scan-state').textContent()));
  await page.screenshot({ path: join(outDir, 'index-folders.png'), fullPage: true });

  await page.locator('.tracked', { hasText: 'every .html' }).getByRole('button', { name: 'Remove' }).click();
  await page.waitForFunction(() => document.querySelectorAll('#folder-list .tracked').length === 1);
  step('folder removed after confirm');

  await page.locator('#days').selectOption('30');
  await page.getByRole('button', { name: 'Rescan all folders' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.startsWith('Rescan started'));
  await page.waitForFunction(() => document.getElementById('scan-state').textContent.includes('0 document(s) added'));
  if (await page.locator('.doc', { hasText: 'Bravo' }).count()) fail('the rescan brought back an unmanaged document');
  step('rescan: ' + (await page.locator('#scan-state').textContent()));

  await page.getByRole('button', { name: 'Restart daemon' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.startsWith('Restart asked'));
  await page.getByRole('button', { name: 'Stop daemon' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.startsWith('Stop asked'));
  step('daemon buttons answered; dialogs: ' + dialogs.length);
  if (dialogs.length !== 4) fail('expected 4 confirmations (unmanage, delete, remove folder, stop), got ' + dialogs.length);

  await page.setViewportSize({ width: 390, height: 844 });
  await fits(390);
  step('still fits at 390 px');

  if (errors.length) fail('page errors: ' + errors.join(' | '));
  step('no page error');
} finally {
  await browser.close();
}
