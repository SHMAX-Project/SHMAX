import { expect, test, test as _t, type Page } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Live acceptance addendum #6 — Help page routing + mobile layout.
 * Runs against the same production backend harness as wizard.spec.
 */

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATE_FILE = path.join(HERE, '.e2e-state.json');

function backendURL(projectName: string): string {
  const raw = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  const s = raw.projects[projectName];
  if (!s) throw new Error(`no e2e state for project ${projectName}`);
  return s.backendURL;
}

async function login(page: Page, base: string) {
  // Works in BOTH execution orders: help.spec may run before the
  // wizard full flow (alphabetical file order) — bootstrap the
  // admin through the one-time setup link if needed.
  const raw = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  const setupToken = raw.projects[_t.info().project.name].setupToken;
  await page.goto(`${base}/dashboard`);
  const bootstrapForm = page.getByRole('button', { name: 'Создать администратора' });
  try {
    await bootstrapForm.waitFor({ state: 'visible', timeout: 4000 });
  } catch {
    /* already bootstrapped */
  }
  if (await bootstrapForm.isVisible().catch(() => false)) {
    await page.goto(`${base}/setup?t=${setupToken}`);
    await page.locator('input[autocomplete="username"]').fill('owner');
    await page.locator('input[autocomplete="new-password"]').first().fill('owner-pass-12345');
    await page.locator('input[autocomplete="new-password"]').nth(1).fill('owner-pass-12345');
    await page.getByRole('button', { name: 'Создать администратора' }).click();
    await page.waitForURL(/\/setup\/max$/, { timeout: 20_000 });
    return;
  }
  await page.goto(`${base}/login`);
  await page.locator('input[name="username"]').fill('owner');
  await page.locator('input[name="password"]').fill('owner-pass-12345');
  await page.getByRole('button', { name: 'Войти' }).click();
  await page.waitForURL(/dashboard|setup/, { timeout: 20_000 });
}

/**
 * The authenticated pages (help / about) exist only after the
 * wizard completed setup. In the full project run this file
 * (zz-*) executes AFTER wizard.spec's full flow, so setup is
 * complete; standalone runs without a finished setup skip.
 */
async function requireSetupComplete(page: Page) {
  if (!page.url().match(/dashboard|help|about/)) {
    test.skip(true, 'setup not complete in this run');
  }
}

test('help page routes and renders canonical sections', async ({ page }, testInfo) => {
  const base = backendURL(testInfo.project.name);
  await login(page, base);
  await requireSetupComplete(page);
  await page.goto(`${base}/help`);
  await expect(page.getByTestId('help-page')).toBeVisible();
  await expect(page.getByText('Быстрый старт')).toBeVisible();
  await expect(page.getByTestId('help-section-telegram-bot')).toContainText('BotFather');
  await expect(page.getByTestId('help-section-telegram-group')).toContainText('/shmax_connect');
  await expect(page.getByTestId('help-section-telegram-network')).toContainText('Xray');
  await expect(page.getByTestId('troubleshooting')).toBeVisible();
});

test('mascot renders on About page', async ({ page }, testInfo) => {
  const base = backendURL(testInfo.project.name);
  await login(page, base);
  await requireSetupComplete(page);
  await page.goto(`${base}/about`);
  const mascot = page.getByTestId('mascot');
  await expect(mascot).toBeVisible();
  expect(await mascot.getAttribute('src')).toContain('shmax-icon');
});

test('mobile has no horizontal overflow on help page', async ({ page }, testInfo) => {
  const base = backendURL(testInfo.project.name);
  await login(page, base);
  await requireSetupComplete(page);
  await page.goto(`${base}/help`);
  const metrics = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(metrics.scroll).toBeLessThanOrEqual(metrics.client + 1);
});
