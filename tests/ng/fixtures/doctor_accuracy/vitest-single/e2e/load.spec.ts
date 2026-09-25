import { expect, test } from '@playwright/test';

test('loads', async ({ page }) => {
  await page.goto('http://localhost:3000/');
  await expect(page).toHaveTitle(/demo/);
});
