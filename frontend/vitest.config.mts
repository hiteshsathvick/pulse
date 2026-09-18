import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    // React Testing Library's automatic unmount-after-each-test relies on a
    // *global* afterEach; without this, DOM from one test leaks into the
    // next.
    globals: true,
    // Playwright specs live in e2e/ and run via `npm run test:e2e`, not
    // this runner.
    exclude: ["e2e/**", "node_modules/**"],
  },
});
