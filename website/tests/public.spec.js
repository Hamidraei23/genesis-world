import { test, expect } from '@playwright/test';
import { readdir, readFile } from 'node:fs/promises';

test('runtime-only export excludes development files and still renders the robot', async ({ page }) => {
  const files = await readdir('public', { recursive: true });
  expect(files.some((file) => /(^|\/)(src|scripts|tests|node_modules)(\/|$)/.test(file))).toBe(false);
  expect(files.some((file) => file.endsWith('.map'))).toBe(false);
  expect(files).not.toContain('README.md');
  expect(files).not.toContain('assets/panda/manifest.json');
  expect(await readFile('public/assets/vendor/viewer.js', 'utf8')).not.toContain('sourceMappingURL=');
  expect(await readFile('public/assets/vendor/URDF-LOADER-NOTICE.md', 'utf8')).toContain('Copyright');
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('response', (response) => { if (response.status() >= 400) errors.push(response.url()); });
  await page.goto('/public/');
  await expect(page.locator('body')).toHaveAttribute('data-ready', 'true', { timeout: 45000 });
  await page.locator('#panda_joint1').fill('40');
  await expect(page.locator('output[for="panda_joint1"]')).toHaveText('40.0°');
  await page.getByRole('button', { name: 'Home pose' }).click();
  await expect(page.locator('output[for="panda_joint1"]')).toHaveText('0.0°');
  expect(errors).toEqual([]);
});
