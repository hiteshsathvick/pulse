import { describe, expect, it } from "vitest";
import { buildPatch, draftFromDashboard, type DashboardDraft } from "./dashboard-draft";
import { addTile, moveTile, resizeTile } from "./dashboard-layout";
import type { Dashboard } from "./dashboards-api";

const item = (id: string, x: number, y: number, w: number, h: number) => ({
  id: `item-${id}`,
  insight: {
    id,
    name: `Insight ${id}`,
    kind: "trend" as const,
    spec: {
      kind: "trend" as const,
      version: 1 as const,
      events: ["e"],
      measure: "count",
      filters: [],
      breakdown: null,
      range: { from: "2026-01-01", to: "2026-01-31", tz: "project" },
      granularity: "day" as const,
    },
  },
  position: { x, y, w, h },
});

const dashboard: Dashboard = {
  id: "d1",
  name: "Growth",
  layout: { columns: 12 },
  default_range: { type: "relative", days: 30 },
  shared_scope: "private",
  created_by: "u1",
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
  can_edit: true,
  items: [item("a", 0, 0, 6, 3), item("b", 6, 0, 6, 3)],
};

const original = draftFromDashboard(dashboard);

describe("draftFromDashboard", () => {
  it("takes tiles in the server's reading order, with their sizes", () => {
    expect(original).toEqual({
      name: "Growth",
      sharedScope: "private",
      range: { type: "relative", days: 30 },
      tiles: [
        { insightId: "a", w: 6, h: 3 },
        { insightId: "b", w: 6, h: 3 },
      ],
    });
  });
});

describe("buildPatch", () => {
  const patchOf = (draft: DashboardDraft) => {
    const result = buildPatch(original, draft);
    if (result.errors) throw new Error(result.errors.join("; "));
    return result.patch;
  };

  it("is null when nothing changed", () => {
    expect(patchOf({ ...original })).toBeNull();
    expect(patchOf({ ...original, name: "  Growth  " })).toBeNull(); // trimming isn't a change
  });

  it("sends only the fields that changed", () => {
    expect(patchOf({ ...original, name: "Renamed" })).toEqual({ name: "Renamed" });
    expect(patchOf({ ...original, sharedScope: "org" })).toEqual({ shared_scope: "org" });
    expect(patchOf({ ...original, range: { type: "relative", days: 7 } })).toEqual({
      default_range: { type: "relative", days: 7 },
    });
  });

  it("a layout change sends the whole packed layout, and nothing else", () => {
    const patch = patchOf({ ...original, tiles: moveTile(original.tiles, 0, 1) });
    expect(patch).toEqual({
      items: [
        { insight_id: "b", position: { x: 0, y: 0, w: 6, h: 3 } },
        { insight_id: "a", position: { x: 6, y: 0, w: 6, h: 3 } },
      ],
    });
  });

  it("combines several changes into one patch", () => {
    const tiles = resizeTile(addTile(original.tiles, "c"), 0, "large");
    const patch = patchOf({ ...original, name: "New", sharedScope: "org", tiles });
    expect(patch).toMatchObject({ name: "New", shared_scope: "org" });
    expect(patch?.items?.map((i) => [i.insight_id, i.position.y])).toEqual([
      ["a", 0],
      ["b", 4],
      ["c", 4],
    ]);
    expect(patch).not.toHaveProperty("default_range");
  });

  it("rejects a blank name or an invalid range, with reasons", () => {
    const blank = buildPatch(original, { ...original, name: "   " });
    expect(blank.errors).toContain("Give the dashboard a name.");
    const badRange = buildPatch(original, {
      ...original,
      range: { type: "absolute", from: "2026-02-01", to: "2026-01-01" },
    });
    expect(badRange.errors).toContain("Choose a valid default date range.");
  });
});
