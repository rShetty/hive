// @ts-check
const { defineConfig, devices } = require('@playwright/test');

const HIVE_BASE = process.env.HIVE_BASE || 'http://localhost:8000';

module.exports = defineConfig({
  testDir: './tests/playwright',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: [['list']],
  timeout: 120000,
  use: {
    baseURL: HIVE_BASE,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    actionTimeout: 30000,
    navigationTimeout: 30000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
