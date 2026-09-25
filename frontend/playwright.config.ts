import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: {
    // No `webServer` here: this app needs the whole docker-compose stack
    // (postgres/redis/clickhouse/objectstore/api) up and healthy, not just
    // `next dev` on its own -- start both separately before running these
    // (`docker compose up -d` for the stack, `npm run dev` for the
    // frontend), matching this phase's "local-only for now" scope for
    // e2e (kept out of CI until there's more to test end-to-end).
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
