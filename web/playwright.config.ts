import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: process.env.TTSGRAB_E2E_BASE_URL ?? 'http://127.0.0.1:8011',
    trace: 'retain-on-failure',
    // Use the locally installed Chrome instead of downloading Chromium.
    channel: process.env.CI ? undefined : 'chrome',
  },
  projects: [{ name: 'chromium', use: {} }],
})
