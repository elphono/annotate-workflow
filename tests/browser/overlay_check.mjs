// Drives the overlay in a real headless Chromium, through Playwright.
//
//   node overlay_check.mjs <playwright-dir> <base-url> <doc-id> <out-dir> <doc-path>
//
// <playwright-dir> is any directory whose node_modules holds `playwright`
// (the repository does not depend on Node: see CLAUDE.md). Each step prints
// one line; any failure exits non-zero with the reason. The Python side
// (tests/test_browser.py) checks what reached the server.
import { createRequire } from 'node:module';
import { appendFileSync } from 'node:fs';
import { join } from 'node:path';

const [pwDir, base, docId, outDir, docPath] = process.argv.slice(2);
const require = createRequire(join(pwDir, 'package.json'));
const { chromium } = require('playwright');

const fail = (msg) => { console.error('FAIL ' + msg); process.exit(1); };
const step = (msg) => console.log('OK   ' + msg);

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(`${base}/docs/${docId}`);
  await page.waitForSelector('annotate-root', { state: 'attached' });
  await page.waitForFunction(() => {
    const bar = document.querySelector('annotate-root').shadowRoot.querySelector('.bar');
    return bar && bar.textContent.includes('to send');
  });
  step('overlay loaded');

  // 1. The pre-seeded orphan is listed with its quote, never dropped.
  const orphanText = await page.locator('.orphan').first().textContent();
  if (!orphanText.includes('A sentence that was deleted')) fail('orphan not listed: ' + orphanText);
  step('orphan listed in the bar: ' + orphanText.trim());

  // 2. The relative image loads through /docs/<id>/files/.
  const imgOk = await page.evaluate(() => document.querySelector('img').naturalWidth > 0);
  if (!imgOk) fail('relative image did not load');
  step('relative image loaded');

  // 3. Alt+click on the second paragraph opens the bubble.
  const target = page.locator('#body-text p').nth(1);
  const box = await target.boundingBox();
  const x = box.x + 40, y = box.y + box.height / 2;
  await page.keyboard.down('Alt');
  await page.mouse.click(x, y);
  await page.keyboard.up('Alt');
  await page.waitForSelector('.bubble textarea');
  await page.locator('.bubble textarea').fill('Rephrase this, it is unclear.');
  await page.screenshot({ path: join(outDir, 'overlay-bubble.png') });
  await page.locator('.bubble textarea').press('Enter');
  await page.waitForFunction(async (api) => {
    const r = await fetch(api); const d = await r.json();
    return d.annotations.length === 2;
  }, `/api/docs/${docId}/annotations`);
  step('alt+click note saved by PUT');

  // 3b. Alt+click in the figure, next to the "Browser" label (not on it).
  const fig = await page.locator('#fig').boundingBox();
  await page.keyboard.down('Alt');
  await page.mouse.click(fig.x + 360, fig.y + 20);
  await page.keyboard.up('Alt');
  await page.waitForSelector('.bubble textarea');
  const shown = await page.locator('.bubble-quote').textContent();
  if (!shown.includes('label "Browser"')) fail('figure bubble shows: ' + shown);
  await page.locator('.bubble textarea').fill('Rename this box.');
  await page.locator('.bubble textarea').press('Enter');
  await page.waitForFunction(async (api) => {
    const r = await fetch(api); const d = await r.json();
    return d.annotations.length === 3;
  }, `/api/docs/${docId}/annotations`);
  step('figure note saved, nearest label: ' + shown.trim());

  // 4. A plain click on a link still navigates: Alt+click must not break it.
  //    And a fragment link stays on the document despite the <base>.
  await page.locator('a[href="#second"]').click();
  await page.waitForFunction(() => location.hash === '#second');
  if (!page.url().endsWith(`/docs/${docId}#second`)) fail('fragment link left the page: ' + page.url());
  step('fragment link stays on the page: ' + page.url());

  // 5. After a reload, the pin comes back where it was clicked.
  await page.reload();
  await page.waitForSelector('.pin');
  const pins = await page.locator('.pin').count();
  if (pins !== 2) fail(`expected 2 pins (the orphan has none), got ${pins}`);
  const pinBox = await page.locator('.pin').first().boundingBox();
  const cx = pinBox.x + pinBox.width / 2, cy = pinBox.y + pinBox.height / 2;
  const box2 = await target.boundingBox();
  const ex = box2.x + 40, ey = box2.y + box.height / 2;
  if (Math.abs(cx - ex) > 2 || Math.abs(cy - ey) > 2) {
    fail(`pin at (${cx},${cy}), expected (${ex},${ey})`);
  }
  step(`pin restored at the click point (${cx.toFixed(1)}, ${cy.toFixed(1)})`);
  if (!(await page.locator('.orphan').count())) fail('orphan lost after reload');
  step('orphan still listed after reload');
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator('.pin').first().hover();
  const listening = await page.locator('.listening').textContent();
  if (!listening.startsWith('An open session listens')) fail('listening line: ' + listening);
  step('bar says: ' + listening);
  await page.screenshot({ path: join(outDir, 'overlay-before-send.png'), fullPage: false });

  // 6. Send to session: the open session receives the notes, the bar says so.
  await page.locator('.bar .bar-actions button.primary').first().click();
  await page.waitForFunction(() => {
    const bar = document.querySelector('annotate-root').shadowRoot.querySelector('.bar');
    return bar.textContent.includes('delivered to the open session');
  }, null, { timeout: 30000 });
  const counts = await page.locator('.counts').textContent();
  if (!counts.startsWith('0 to send')) fail('counts after send: ' + counts);
  step('send delivered, bar: ' + counts);
  const sentPin = await page.locator('.pin.sent').count();
  if (sentPin !== 2) fail('the sent pins are not greyed');
  step('sent pins greyed');

  // 6b. The session edits the document: the bar offers to reload.
  appendFileSync(docPath, '\n<!-- edited by the session -->\n');
  await page.waitForFunction(() => {
    const bar = document.querySelector('annotate-root').shadowRoot.querySelector('.bar');
    return bar.textContent.includes('changed on disk');
  }, null, { timeout: 15000 });
  step('the edit on disk is noticed, Reload offered');
  await page.screenshot({ path: join(outDir, 'overlay-after-send.png'), fullPage: false });

  // 7. Phone width: the bar fits, no horizontal scroll introduced by it.
  await page.setViewportSize({ width: 390, height: 800 });
  const barBox = await page.locator('.bar').boundingBox();
  if (barBox.x < 0 || barBox.x + barBox.width > 390) fail('bar overflows at 390 px');
  step('bar fits at 390 px');
  await page.screenshot({ path: join(outDir, 'overlay-phone.png') });

  if (errors.length) fail('page errors: ' + errors.join(' | '));
  step('no page error');
} finally {
  await browser.close();
}
