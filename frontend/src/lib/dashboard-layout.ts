// The dashboard editor works on an *ordered list of sized tiles* and derives
// grid positions from it (`packLayout`), rather than letting a user place
// rectangles freely. That makes an overlapping or out-of-bounds layout
// unrepresentable from the UI; the server validates the same rules anyway.
// Mirrors backend/pulse/dashboards/schemas.py.

export const GRID_COLUMNS = 12;
export const MAX_ITEMS = 20;

export type Tile = { insightId: string; w: number; h: number };
export type Position = { x: number; y: number; w: number; h: number };
export type PlacedTile = Tile & Position;

export const TILE_SIZES = [
  { key: "small", label: "Small", w: 4, h: 3 },
  { key: "medium", label: "Medium", w: 6, h: 3 },
  { key: "large", label: "Large (full width)", w: 12, h: 4 },
] as const;

export type TileSizeKey = (typeof TILE_SIZES)[number]["key"];

export const DEFAULT_TILE_SIZE: TileSizeKey = "medium";

export function sizeOf(key: TileSizeKey): { w: number; h: number } {
  const size = TILE_SIZES.find((s) => s.key === key) ?? TILE_SIZES[1];
  return { w: size.w, h: size.h };
}

// A tile whose size isn't one of the presets (e.g. set through the API) is
// shown as "custom" and left alone until the user picks a preset.
export function sizeKeyOf(tile: Tile): TileSizeKey | "custom" {
  return TILE_SIZES.find((s) => s.w === tile.w && s.h === tile.h)?.key ?? "custom";
}

// Shelf packing: fill each row left to right, wrap when the next tile won't
// fit, and start the next row below the tallest tile in this one.
export function packLayout(tiles: Tile[]): PlacedTile[] {
  const placed: PlacedTile[] = [];
  let x = 0;
  let rowTop = 0;
  let rowHeight = 0;
  for (const tile of tiles) {
    if (x + tile.w > GRID_COLUMNS) {
      rowTop += rowHeight;
      x = 0;
      rowHeight = 0;
    }
    placed.push({ ...tile, x, y: rowTop });
    x += tile.w;
    rowHeight = Math.max(rowHeight, tile.h);
  }
  return placed;
}

export function toItemInputs(tiles: Tile[]): { insight_id: string; position: Position }[] {
  return packLayout(tiles).map(({ insightId, x, y, w, h }) => ({
    insight_id: insightId,
    position: { x, y, w, h },
  }));
}

export function addTile(
  tiles: Tile[],
  insightId: string,
  size: TileSizeKey = DEFAULT_TILE_SIZE
): Tile[] {
  if (tiles.length >= MAX_ITEMS || tiles.some((t) => t.insightId === insightId)) return tiles;
  return [...tiles, { insightId, ...sizeOf(size) }];
}

export function removeTile(tiles: Tile[], index: number): Tile[] {
  return tiles.filter((_, i) => i !== index);
}

// Moves a tile `delta` places in reading order, stopping at either end.
export function moveTile(tiles: Tile[], index: number, delta: number): Tile[] {
  const target = index + delta;
  if (index < 0 || index >= tiles.length || target < 0 || target >= tiles.length) return tiles;
  const next = [...tiles];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function resizeTile(tiles: Tile[], index: number, size: TileSizeKey): Tile[] {
  return tiles.map((tile, i) => (i === index ? { ...tile, ...sizeOf(size) } : tile));
}

export function sameLayout(a: Tile[], b: Tile[]): boolean {
  return (
    a.length === b.length &&
    a.every((t, i) => t.insightId === b[i].insightId && t.w === b[i].w && t.h === b[i].h)
  );
}
