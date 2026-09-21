import { isValidRange, type DashboardRange } from "./dashboard-range";
import { sameLayout, toItemInputs, type Tile } from "./dashboard-layout";
import type { Dashboard, DashboardPatch, DashboardScope } from "./dashboards-api";

export type DashboardDraft = {
  name: string;
  sharedScope: DashboardScope;
  range: DashboardRange;
  tiles: Tile[];
};

// The server returns items already in reading order (top-to-bottom, then
// left-to-right), which is the order the editor's tile list works in.
export function draftFromDashboard(dashboard: Dashboard): DashboardDraft {
  return {
    name: dashboard.name,
    sharedScope: dashboard.shared_scope,
    range: dashboard.default_range,
    tiles: dashboard.items.map((item) => ({
      insightId: item.insight.id,
      w: item.position.w,
      h: item.position.h,
    })),
  };
}

export type PatchResult =
  | { patch: DashboardPatch | null; errors?: undefined }
  | { patch?: undefined; errors: string[] };

// Sends only what changed. That keeps a rename from also rewriting the layout
// (and keeps the server's audit log honest about what was edited). `patch: null`
// means nothing changed.
export function buildPatch(original: DashboardDraft, draft: DashboardDraft): PatchResult {
  const errors: string[] = [];
  const name = draft.name.trim();
  if (!name) errors.push("Give the dashboard a name.");
  if (!isValidRange(draft.range)) errors.push("Choose a valid default date range.");
  if (errors.length > 0) return { errors };

  const patch: DashboardPatch = {};
  if (name !== original.name) patch.name = name;
  if (draft.sharedScope !== original.sharedScope) patch.shared_scope = draft.sharedScope;
  if (JSON.stringify(draft.range) !== JSON.stringify(original.range)) {
    patch.default_range = draft.range;
  }
  if (!sameLayout(draft.tiles, original.tiles)) patch.items = toItemInputs(draft.tiles);
  return { patch: Object.keys(patch).length > 0 ? patch : null };
}
