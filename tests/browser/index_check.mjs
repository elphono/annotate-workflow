// Drives the index page in a real headless Chromium: every tray control.
//
//   node index_check.mjs <playwright-dir> <base-url> <out-dir> <folder-to-add>
//
// The Python side (tests/test_browser.py) prepares three documents in two
// groups, and a folder holding a fourth, and checks what reached the
// registry, the disk, the tracked folders and the daemon.
import { createRequire } from 'node:module';
import { join } from 'node:path';

const [pwDir, base, outDir, folder] = process.argv.slice(2);
const require = createRequire(join(pwDir, 'package.json'));
const { chromium } = require('playwright');

const fail = (msg) => { console.error('FAIL ' + msg); process.exit(1); };
const step = (msg) => console.log('OK   ' + msg);

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const dialogs = [];
  page.on('dialog', async (d) => { dialogs.push(d.message()); await d.accept(); });
  await page.goto(base + '/');
  await page.waitForSelector('section .doc');

  const heads = await page.locator('#groups section h2').allTextContents();
  if (heads.length !== 2 || !heads[0].startsWith('Live session') ||
      heads[1] !== 'documents without a known session') fail('groups: ' + JSON.stringify(heads));
  step('grouped by session: ' + JSON.stringify(heads));
  const row = (title) => page.locator('.doc', { hasText: title });
  if (!(await row('Alpha').locator('.badge.live').count())) fail('no listening badge on Alpha');
  step('listening badge shown');
  await page.screenshot({ path: join(outDir, 'index-light.png'), fullPage: true });

  await row('Alpha').getByRole('button', { name: 'Send to session' }).click();
  await page.waitForFunction(() => document.getElementById('message').textContent.includes('delivered to the open session'));
  step('send: ' + (await page.locator('#message').textContent()));

  await row('Bravo').getByRole('button', { name: 'Unmanage' }).click();
  await page.waitForFunction(() => !document.body.textContent.includes('Bravo'));
  step('unmanage: row gone after confirm');

  await row('Charlie').getByRole('button', { name: 'Delete file…' }).click();
  await page.waitForFunction(() => !document.body.textContent.includes('Charlie'));
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
  await page.waitForFunction(() => document.body.textContent.includes('Delta'));
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

  await page.setViewportSize({ width: 390, height: 800 });
  const wide = await page.evaluate(() => document.documentElement.scrollWidth);
  if (wide > 390) fail('horizontal scroll at 390 px: ' + wide);
  step('fits at 390 px');
  await page.screenshot({ path: join(outDir, 'index-phone.png'), fullPage: true });
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.setViewportSize({ width: 1100, height: 800 });
  await page.screenshot({ path: join(outDir, 'index-dark.png'), fullPage: true });

  if (errors.length) fail('page errors: ' + errors.join(' | '));
  step('no page error');
} finally {
  await browser.close();
}
