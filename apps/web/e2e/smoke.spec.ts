import { test, expect } from '@playwright/test';

test('landing renders the V4 product promise', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  // V4 headline (standalone marketing landing, outside AppShell).
  await expect(page.getByText(/grade themselves/i)).toBeVisible();
  // honest Phase-2D payout language is intact (no implied live payouts).
  await expect(page.getByText(/payout state is always labeled honestly/i)).toBeVisible();
});

test('the app Primary nav exposes the six public sections (on an app route)', async ({ page }) => {
  // `/` is now the standalone marketing landing; the app Primary nav lives in the (app)
  // route group, so assert it on an app route (URL-transparent — path unchanged).
  await page.goto('/leaderboard');
  const nav = page.getByRole('navigation', { name: 'Primary' });
  for (const label of ['Competitions', 'Arena', 'Trials', 'Markets', 'Leaderboard', 'Agents']) {
    await expect(nav.getByRole('link', { name: label })).toBeVisible();
  }
});
