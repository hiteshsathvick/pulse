"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Alert, Button, EmptyState, Input } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import { buildPatch, draftFromDashboard, type DashboardDraft } from "@/lib/dashboard-draft";
import {
  addTile,
  moveTile,
  removeTile,
  resizeTile,
  sizeKeyOf,
  TILE_SIZES,
  type TileSizeKey,
} from "@/lib/dashboard-layout";
import type { ResolvedRange } from "@/lib/dashboard-range";
import {
  deleteDashboard,
  updateDashboard,
  type Dashboard,
  type DashboardScope,
} from "@/lib/dashboards-api";
import { listInsights } from "@/lib/insights-api";
import { RangePicker } from "./RangePicker";

const SELECT_CLASS = "rounded border px-3 py-2";

export function DashboardEditor({
  orgId,
  projectId,
  dashboard,
  customSeed,
  onCancel,
  onSaved,
}: {
  orgId: string;
  projectId: string;
  dashboard: Dashboard;
  customSeed: ResolvedRange;
  onCancel: () => void;
  onSaved: (saved: Dashboard) => void;
}) {
  const { accessToken } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();

  const [original] = useState(() => draftFromDashboard(dashboard));
  const [draft, setDraft] = useState<DashboardDraft>(original);
  const [errors, setErrors] = useState<string[]>([]);
  const [noChanges, setNoChanges] = useState(false);
  const [toAdd, setToAdd] = useState("");

  const insightsQuery = useQuery({
    queryKey: ["insights", orgId, projectId],
    queryFn: () => listInsights(accessToken!, orgId, projectId),
    enabled: !!accessToken,
  });

  // Name/kind for every tile, including ones whose insight the list hasn't
  // returned (the dashboard response already embeds those).
  const known = new Map<string, { name: string; kind: string }>();
  for (const item of dashboard.items) known.set(item.insight.id, item.insight);
  for (const insight of insightsQuery.data ?? []) known.set(insight.id, insight);

  const available = (insightsQuery.data ?? []).filter(
    (insight) => !draft.tiles.some((tile) => tile.insightId === insight.id)
  );

  const edit = (patch: Partial<DashboardDraft>) => {
    setNoChanges(false);
    setDraft((d) => ({ ...d, ...patch }));
  };

  const saveMutation = useMutation({
    mutationFn: (patch: NonNullable<ReturnType<typeof buildPatch>["patch"]>) =>
      updateDashboard(accessToken!, orgId, projectId, dashboard.id, patch),
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: ["dashboards", orgId, projectId] });
      onSaved(saved);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteDashboard(accessToken!, orgId, projectId, dashboard.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["dashboards", orgId, projectId] });
      router.push(`/orgs/${orgId}/projects/${projectId}/dashboards`);
    },
  });

  function handleSave() {
    const result = buildPatch(original, draft);
    if (result.errors) {
      setErrors(result.errors);
      return;
    }
    setErrors([]);
    if (result.patch === null) {
      setNoChanges(true);
      return;
    }
    saveMutation.mutate(result.patch);
  }

  function handleDelete() {
    if (window.confirm(`Delete "${dashboard.name}"? The insights on it are kept.`)) {
      deleteMutation.mutate();
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Dashboard name</span>
          <Input
            value={draft.name}
            maxLength={200}
            onChange={(e) => edit({ name: e.target.value })}
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium">Who can see it</span>
          <select
            className={SELECT_CLASS}
            value={draft.sharedScope}
            onChange={(e) => edit({ sharedScope: e.target.value as DashboardScope })}
          >
            <option value="private">Only me</option>
            <option value="org">Everyone in my organization</option>
          </select>
          <span className="text-xs text-gray-500">
            Teammates can view a shared dashboard; only its creator and org admins can change it.
          </span>
        </label>
      </div>

      <RangePicker
        label="Default date range"
        value={draft.range}
        onChange={(range) => edit({ range })}
        customSeed={customSeed}
      />

      <section className="flex flex-col gap-3" aria-label="Tiles">
        <h2 className="text-lg font-medium">Insights on this dashboard</h2>
        {draft.tiles.length === 0 && <EmptyState>No insights yet. Add one below.</EmptyState>}
        <ol className="flex flex-col gap-2">
          {draft.tiles.map((tile, index) => {
            const info = known.get(tile.insightId);
            const name = info?.name ?? "Unknown insight";
            const sizeKey = sizeKeyOf(tile);
            return (
              <li
                key={tile.insightId}
                className="flex flex-wrap items-center gap-3 rounded border px-3 py-2"
              >
                <span className="min-w-0 flex-1 truncate font-medium">
                  {name}
                  {info && <span className="ml-2 text-xs font-normal text-gray-500">{info.kind}</span>}
                </span>
                <select
                  aria-label={`Size of ${name}`}
                  className={SELECT_CLASS}
                  value={sizeKey}
                  onChange={(e) =>
                    edit({ tiles: resizeTile(draft.tiles, index, e.target.value as TileSizeKey) })
                  }
                >
                  {sizeKey === "custom" && <option value="custom">Custom</option>}
                  {TILE_SIZES.map((size) => (
                    <option key={size.key} value={size.key}>
                      {size.label}
                    </option>
                  ))}
                </select>
                <Button
                  type="button"
                  variant="secondary"
                  aria-label={`Move ${name} up`}
                  disabled={index === 0}
                  onClick={() => edit({ tiles: moveTile(draft.tiles, index, -1) })}
                >
                  Up
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  aria-label={`Move ${name} down`}
                  disabled={index === draft.tiles.length - 1}
                  onClick={() => edit({ tiles: moveTile(draft.tiles, index, 1) })}
                >
                  Down
                </Button>
                <Button
                  type="button"
                  variant="danger"
                  aria-label={`Remove ${name}`}
                  onClick={() => edit({ tiles: removeTile(draft.tiles, index) })}
                >
                  Remove
                </Button>
              </li>
            );
          })}
        </ol>

        {insightsQuery.data && insightsQuery.data.length === 0 ? (
          <p className="text-sm text-gray-500">
            This project has no saved insights yet.{" "}
            <Link
              className="underline"
              href={`/orgs/${orgId}/projects/${projectId}/insights/new`}
            >
              Build one
            </Link>{" "}
            first.
          </p>
        ) : (
          <div className="flex flex-wrap items-end gap-2">
            <label className="flex flex-col gap-1 text-sm">
              <span className="font-medium">Add an insight</span>
              <select
                className={SELECT_CLASS}
                value={toAdd}
                onChange={(e) => setToAdd(e.target.value)}
              >
                <option value="">Choose an insight…</option>
                {available.map((insight) => (
                  <option key={insight.id} value={insight.id}>
                    {insight.name}
                  </option>
                ))}
              </select>
            </label>
            <Button
              type="button"
              variant="secondary"
              disabled={!toAdd}
              onClick={() => {
                edit({ tiles: addTile(draft.tiles, toAdd) });
                setToAdd("");
              }}
            >
              Add
            </Button>
          </div>
        )}
      </section>

      {errors.length > 0 && (
        <ul role="alert" className="flex flex-col gap-1">
          {errors.map((message) => (
            <li key={message}>
              <Alert>{message}</Alert>
            </li>
          ))}
        </ul>
      )}
      {saveMutation.isError && <Alert>{saveMutation.error.message}</Alert>}
      {deleteMutation.isError && <Alert>{deleteMutation.error.message}</Alert>}
      {noChanges && <p className="text-sm text-gray-500">Nothing has changed.</p>}

      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" onClick={handleSave} disabled={saveMutation.isPending}>
          Save dashboard
        </Button>
        <Button type="button" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="button" variant="danger" onClick={handleDelete}>
          Delete dashboard
        </Button>
      </div>
    </div>
  );
}
