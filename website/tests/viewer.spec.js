import { test, expect } from '@playwright/test';

test('loads all local model assets and supports pose and scene controls', async ({ page, baseURL }) => {
  const errors = [];
  const failed = [];
  const external = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('response', (response) => { if (response.status() >= 400) failed.push(response.url()); });
  page.on('request', (request) => { if (!request.url().startsWith(baseURL)) external.push(request.url()); });
  await page.goto('/');
  await expect(page.locator('body')).toHaveAttribute('data-ready', 'true', { timeout: 45000 });
  await expect(page.locator('#joints input[type="range"]')).toHaveCount(7);
  await expect(page.locator('#load-status')).toBeHidden();
  const position = await page.locator('#tool-position').textContent();
  await page.locator('#panda_joint1').fill('40');
  await expect(page.locator('output[for="panda_joint1"]')).toHaveText('40.0°');
  await expect(page.locator('#tool-position')).not.toHaveText(position);
  await page.locator('#gripper').fill('80');
  await expect(page.locator('#gripper-value')).toHaveText('80.0 mm');
  await page.getByRole('button', { name: 'Home pose' }).click();
  await expect(page.locator('#gripper-value')).toHaveText('16.2 mm');
  await expect(page.locator('#tool-position')).toHaveText(position);
  await page.getByRole('button', { name: 'Play demo' }).click();
  await expect(page.locator('#tool-position')).not.toHaveText(position);
  await page.getByRole('button', { name: 'Pause demo' }).click();
  await page.getByLabel('Wireframe', { exact: true }).check();
  await page.getByLabel('Base axes', { exact: true }).check();
  await page.getByLabel('Floor grid', { exact: true }).uncheck();
  await page.getByRole('button', { name: 'Top', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Top', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.getByLabel('Wireframe', { exact: true }).uncheck();
  await page.getByLabel('Base axes', { exact: true }).uncheck();
  await page.getByLabel('Floor grid', { exact: true }).check();
  await page.getByRole('button', { name: '3D', exact: true }).click();
  await page.getByRole('button', { name: 'Home pose' }).click();
  await page.locator('canvas').scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.screenshot({ path: 'test-results/franka-desktop.png', fullPage: true });
  expect(errors).toEqual([]);
  expect(failed).toEqual([]);
  expect(external).toEqual([]);
});

test('mobile layout fits and embedded mode hides the surrounding article', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?embed');
  await expect(page.locator('body')).toHaveAttribute('data-ready', 'true', { timeout: 45000 });
  await expect(page.locator('.masthead')).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await page.locator('#panda_joint2').fill('-25');
  await expect(page.locator('output[for="panda_joint2"]')).toHaveText('-25.0°');
  await page.screenshot({ path: 'test-results/franka-mobile.png', fullPage: true });
});

test('missing assets produce a visible error and disable controls', async ({ page }) => {
  await page.route('**/meshes/visual/link1.obj', (route) => route.fulfill({ status: 404, body: 'Missing' }));
  await page.goto('/');
  await expect(page.locator('#load-status')).toHaveAttribute('data-error', 'true', { timeout: 45000 });
  await expect(page.locator('#home')).toBeDisabled();
});

test('portfolio embed loads at a nested URL and links to the full viewer', async ({ page, baseURL }) => {
  // Existing portfolio fonts and media are unrelated to the self-contained viewer.
  await page.route('**/*', (route) => {
    if (route.request().url().startsWith(baseURL)) return route.continue();
    return route.abort();
  });
  await page.goto('/preview/#robot-playground');
  const frame = page.frameLocator('#franka-playground-frame');
  await expect(frame.locator('body')).toHaveAttribute('data-ready', 'true', { timeout: 45000 });
  await expect(frame.locator('.masthead')).toBeHidden();
  await expect(page.getByRole('link', { name: 'Robot lab', exact: true })).toBeVisible();
  await page.locator('#robot-playground').screenshot({ path: 'test-results/blog-embed.png' });
  await page.getByRole('link', { name: 'Open the full robot playground' }).click();
  await expect(page.locator('body')).toHaveAttribute('data-ready', 'true', { timeout: 45000 });
  await expect(page.locator('.masthead')).toBeVisible();
});
