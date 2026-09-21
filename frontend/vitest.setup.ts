import "@testing-library/jest-dom/vitest";

// jsdom has no ResizeObserver, which Recharts' ResponsiveContainer needs. A
// no-op stub is enough: jsdom has no layout, so a real observer would never
// report a size anyway -- tests assert on the chart's wrapper and the table
// view, not on rendered SVG geometry.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
