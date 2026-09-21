import { describe, expect, it } from "vitest";
import {
  addTile,
  GRID_COLUMNS,
  MAX_ITEMS,
  moveTile,
  packLayout,
  removeTile,
  resizeTile,
  sameLayout,
  sizeKeyOf,
  toItemInputs,
  type PlacedTile,
  type Tile,
} from "./dashboard-layout";

const small = (id: string): Tile => ({ insightId: id, w: 4, h: 3 });
const medium = (id: string): Tile => ({ insightId: id, w: 6, h: 3 });
const large = (id: string): Tile => ({ insightId: id, w: 12, h: 4 });

function overlaps(a: PlacedTile, b: PlacedTile): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

describe("packLayout", () => {
  it("fills a row left to right, wraps when the next tile won't fit, and clears the tallest tile", () => {
    const placed = packLayout([medium("a"), medium("b"), small("c"), large("d"), small("e")]);
    expect(placed.map(({ insightId, x, y }) => [insightId, x, y])).toEqual([
      ["a", 0, 0],
      ["b", 6, 0],
      ["c", 0, 3], // row 1 is full (6+6), so wrap below the 3-row-tall tiles
      ["d", 0, 6], // a full-width tile can't share c's row
      ["e", 0, 10], // and the next row starts below d's 4 rows
    ]);
  });

  it("three small tiles share one row", () => {
    expect(packLayout([small("a"), small("b"), small("c")]).map((t) => t.x)).toEqual([0, 4, 8]);
  });

  it("a row is as tall as its tallest tile", () => {
    const placed = packLayout([
      { insightId: "a", w: 6, h: 2 },
      { insightId: "b", w: 6, h: 5 },
      { insightId: "c", w: 12, h: 1 },
    ]);
    expect(placed[2].y).toBe(5);
  });

  it("never overlaps or leaves the grid, for arbitrary tile mixes", () => {
    // Deterministic pseudo-random sequences, so a failure is reproducible.
    let seed = 12345;
    const next = (n: number) => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed % n;
    };
    for (let round = 0; round < 200; round++) {
      const tiles: Tile[] = Array.from({ length: next(MAX_ITEMS) + 1 }, (_, i) => ({
        insightId: `t${i}`,
        w: next(GRID_COLUMNS) + 1,
        h: next(8) + 1,
      }));
      const placed = packLayout(tiles);
      for (const t of placed) {
        expect(t.x).toBeGreaterThanOrEqual(0);
        expect(t.y).toBeGreaterThanOrEqual(0);
        expect(t.x + t.w).toBeLessThanOrEqual(GRID_COLUMNS);
      }
      for (let i = 0; i < placed.length; i++) {
        for (let j = i + 1; j < placed.length; j++) {
          expect(overlaps(placed[i], placed[j])).toBe(false);
        }
      }
      // Reading order is preserved: a tile is never above the one before it.
      for (let i = 1; i < placed.length; i++) {
        expect(placed[i].y).toBeGreaterThanOrEqual(placed[i - 1].y);
      }
    }
  });

  it("is empty for no tiles", () => {
    expect(packLayout([])).toEqual([]);
  });
});

describe("toItemInputs", () => {
  it("emits the API's shape with packed positions", () => {
    expect(toItemInputs([small("a"), large("b")])).toEqual([
      { insight_id: "a", position: { x: 0, y: 0, w: 4, h: 3 } },
      { insight_id: "b", position: { x: 0, y: 3, w: 12, h: 4 } },
    ]);
  });
});

describe("tile operations", () => {
  const tiles = [small("a"), medium("b"), large("c")];

  it("addTile appends a medium tile, once, up to the cap", () => {
    const added = addTile(tiles, "d");
    expect(added.at(-1)).toEqual({ insightId: "d", w: 6, h: 3 });
    expect(addTile(added, "d")).toBe(added); // no duplicates
    expect(addTile(added, "e", "small").at(-1)).toEqual({ insightId: "e", w: 4, h: 3 });

    const full: Tile[] = Array.from({ length: MAX_ITEMS }, (_, i) => small(`t${i}`));
    expect(addTile(full, "extra")).toBe(full);
  });

  it("moveTile swaps neighbours and stops at both ends", () => {
    expect(moveTile(tiles, 0, 1).map((t) => t.insightId)).toEqual(["b", "a", "c"]);
    expect(moveTile(tiles, 2, -1).map((t) => t.insightId)).toEqual(["a", "c", "b"]);
    expect(moveTile(tiles, 0, -1)).toBe(tiles);
    expect(moveTile(tiles, 2, 1)).toBe(tiles);
    expect(moveTile(tiles, 5, 1)).toBe(tiles);
    expect(tiles.map((t) => t.insightId)).toEqual(["a", "b", "c"]); // not mutated
  });

  it("removeTile drops one tile", () => {
    expect(removeTile(tiles, 1).map((t) => t.insightId)).toEqual(["a", "c"]);
  });

  it("resizeTile changes only the target tile", () => {
    const resized = resizeTile(tiles, 0, "large");
    expect(resized[0]).toEqual({ insightId: "a", w: 12, h: 4 });
    expect(resized[1]).toBe(tiles[1]);
  });

  it("sizeKeyOf names a preset, or 'custom' for anything else", () => {
    expect(sizeKeyOf(small("a"))).toBe("small");
    expect(sizeKeyOf(medium("a"))).toBe("medium");
    expect(sizeKeyOf(large("a"))).toBe("large");
    expect(sizeKeyOf({ insightId: "a", w: 5, h: 2 })).toBe("custom");
  });

  it("sameLayout compares order and sizes", () => {
    expect(sameLayout(tiles, [...tiles])).toBe(true);
    expect(sameLayout(tiles, moveTile(tiles, 0, 1))).toBe(false);
    expect(sameLayout(tiles, resizeTile(tiles, 0, "large"))).toBe(false);
    expect(sameLayout(tiles, removeTile(tiles, 0))).toBe(false);
  });
});
