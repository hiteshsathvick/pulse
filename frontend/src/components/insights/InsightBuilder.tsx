"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Alert, Button, Input, Spinner } from "@/components/ui";
import { useAuth } from "@/lib/auth-context";
import {
  buildSpec,
  defaultDraft,
  INSIGHT_KINDS,
  specToDraft,
  switchKind,
  type Draft,
  type Filter,
  type InsightSpec,
} from "@/lib/insight-spec";
import {
  createInsight,
  deleteInsight,
  runInsightQuery,
  updateInsight,
  type Insight,
} from "@/lib/insights-api";
import { listEventSchemas, listPropertySchemas } from "@/lib/schema-api";
import { InsightResult } from "./InsightResult";
import { SuggestInput } from "./SuggestInput";

const SELECT_CLASS = "rounded border px-3 py-2";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="font-medium">{label}</span>
      {children}
    </label>
  );
}

function selectedEventNames(draft: Draft): string[] {
  if (draft.kind === "trend") return draft.events;
  if (draft.kind === "funnel") return draft.steps;
  return [];
}

export function InsightBuilder({
  orgId,
  projectId,
  initial,
}: {
  orgId: string;
  projectId: string;
  initial?: Insight;
}) {
  const { accessToken } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();

  const [draft, setDraft] = useState<Draft>(() =>
    initial ? specToDraft(initial.spec) : defaultDraft()
  );
  const [name, setName] = useState(initial?.name ?? "");
  const [activeSpec, setActiveSpec] = useState<InsightSpec | null>(initial?.spec ?? null);
  const [errors, setErrors] = useState<string[]>([]);
  const [saved, setSaved] = useState(false);

  const update = (patch: Partial<Draft>) => {
    setSaved(false);
    setDraft((d) => ({ ...d, ...patch }));
  };

  // --- Schema-driven autocomplete -------------------------------------------
  const eventsQuery = useQuery({
    queryKey: ["schema-events", orgId, projectId],
    queryFn: () => listEventSchemas(accessToken!, orgId, projectId),
    enabled: !!accessToken,
  });
  const visibleEvents = (eventsQuery.data ?? []).filter((e) => e.status !== "hidden");
  const eventNames = visibleEvents.map((e) => e.event_name);

  const pickedEventIds = selectedEventNames(draft)
    .map((n) => n.trim())
    .filter(Boolean)
    .map((n) => visibleEvents.find((e) => e.event_name === n)?.id)
    .filter((id): id is string => id !== undefined);

  const propertyQueries = useQueries({
    queries: [...new Set(pickedEventIds)].map((eventId) => ({
      queryKey: ["schema-properties", orgId, projectId, eventId],
      queryFn: () => listPropertySchemas(accessToken!, orgId, projectId, eventId),
      enabled: !!accessToken,
    })),
  });
  const properties = propertyQueries
    .flatMap((q) => q.data ?? [])
    .filter((p) => p.status !== "hidden");
  const propertyKeys = [...new Set(properties.map((p) => p.key))];
  const numericPropertyKeys = [
    ...new Set(properties.filter((p) => p.inferred_type === "number").map((p) => p.key)),
  ];

  // --- Running ---------------------------------------------------------------
  const resultQuery = useQuery({
    queryKey: ["insight-result", orgId, projectId, activeSpec],
    queryFn: () => runInsightQuery(accessToken!, orgId, projectId, activeSpec!),
    enabled: !!accessToken && activeSpec !== null,
  });

  function handleRun() {
    const built = buildSpec(draft);
    if (built.errors) {
      setErrors(built.errors);
      return;
    }
    setErrors([]);
    if (JSON.stringify(built.spec) === JSON.stringify(activeSpec)) {
      void resultQuery.refetch();
    } else {
      setActiveSpec(built.spec);
    }
  }

  // --- Saving ----------------------------------------------------------------
  const listKey = ["insights", orgId, projectId];
  const saveMutation = useMutation({
    mutationFn: async ({ spec }: { spec: InsightSpec }) =>
      initial
        ? updateInsight(accessToken!, orgId, projectId, initial.id, { name: name.trim(), spec })
        : createInsight(accessToken!, orgId, projectId, name.trim(), spec),
    onSuccess: (insight) => {
      void queryClient.invalidateQueries({ queryKey: listKey });
      void queryClient.invalidateQueries({ queryKey: ["insight", orgId, projectId, insight.id] });
      setSaved(true);
      if (!initial) {
        router.replace(`/orgs/${orgId}/projects/${projectId}/insights/${insight.id}`);
      }
    },
  });

  function handleSave() {
    const problems: string[] = [];
    if (!name.trim()) problems.push("Give the insight a name before saving.");
    const built = buildSpec(draft);
    if (built.errors) problems.push(...built.errors);
    if (problems.length > 0 || !built.spec) {
      setErrors(problems);
      return;
    }
    setErrors([]);
    saveMutation.mutate({ spec: built.spec });
  }

  const deleteMutation = useMutation({
    mutationFn: () => deleteInsight(accessToken!, orgId, projectId, initial!.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: listKey });
      router.push(`/orgs/${orgId}/projects/${projectId}/insights`);
    },
  });

  function handleDelete() {
    if (window.confirm(`Delete "${initial?.name}"? This can't be undone.`)) {
      deleteMutation.mutate();
    }
  }

  // --- List editing helpers ---------------------------------------------------
  const setAt = (list: string[], i: number, value: string) =>
    list.map((item, idx) => (idx === i ? value : item));
  const removeAt = <T,>(list: T[], i: number) => list.filter((_, idx) => idx !== i);
  const setFilter = (i: number, patch: Partial<Filter>) =>
    update({ filters: draft.filters.map((f, idx) => (idx === i ? { ...f, ...patch } : f)) });

  const showFilters = draft.kind !== "retention";

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <div className="flex flex-col gap-3">
        <Field label="Insight name">
          <Input
            value={name}
            onChange={(e) => {
              setSaved(false);
              setName(e.target.value);
            }}
            placeholder="e.g. Weekly signups"
            maxLength={200}
          />
        </Field>
        <div className="flex gap-2" role="group" aria-label="Insight type">
          {INSIGHT_KINDS.map(({ kind, label }) => (
            <Button
              key={kind}
              type="button"
              variant={draft.kind === kind ? "primary" : "secondary"}
              aria-pressed={draft.kind === kind}
              onClick={() => {
                setSaved(false);
                setDraft((d) => switchKind(d, kind));
              }}
            >
              {label}
            </Button>
          ))}
        </div>
      </div>

      <div className="flex flex-col gap-4 rounded border p-4">
        {draft.kind === "trend" && (
          <>
            <fieldset className="flex flex-col gap-2">
              <legend className="mb-1 text-sm font-medium">Events</legend>
              {draft.events.map((event, i) => (
                <div key={i} className="flex gap-2">
                  <SuggestInput
                    aria-label={`Event ${i + 1}`}
                    className="flex-1"
                    options={eventNames}
                    value={event}
                    onChange={(e) => update({ events: setAt(draft.events, i, e.target.value) })}
                    placeholder="Event name"
                  />
                  {draft.events.length > 1 && (
                    <Button
                      type="button"
                      variant="danger"
                      aria-label={`Remove event ${i + 1}`}
                      onClick={() => update({ events: removeAt(draft.events, i) })}
                    >
                      Remove
                    </Button>
                  )}
                </div>
              ))}
              <div>
                <Button
                  type="button"
                  variant="link"
                  onClick={() => update({ events: [...draft.events, ""] })}
                >
                  Add event
                </Button>
              </div>
            </fieldset>

            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="Measure">
                <select
                  className={SELECT_CLASS}
                  value={draft.measureType}
                  onChange={(e) =>
                    update({ measureType: e.target.value as Draft["measureType"] })
                  }
                >
                  <option value="count">Total count</option>
                  <option value="unique_users">Unique users</option>
                  <option value="property_sum">Sum of a property</option>
                  <option value="property_avg">Average of a property</option>
                </select>
              </Field>
              {(draft.measureType === "property_sum" || draft.measureType === "property_avg") && (
                <Field label="Property to aggregate">
                  <SuggestInput
                    aria-label="Property to aggregate"
                    options={numericPropertyKeys}
                    value={draft.measureProperty}
                    onChange={(e) => update({ measureProperty: e.target.value })}
                    placeholder="e.g. revenue"
                  />
                </Field>
              )}
              <Field label="Group by time">
                <select
                  className={SELECT_CLASS}
                  value={draft.granularity}
                  onChange={(e) => update({ granularity: e.target.value as Draft["granularity"] })}
                >
                  <option value="hour">Hour</option>
                  <option value="day">Day</option>
                  <option value="week">Week</option>
                  <option value="month">Month</option>
                </select>
              </Field>
            </div>
          </>
        )}

        {draft.kind === "funnel" && (
          <>
            <fieldset className="flex flex-col gap-2">
              <legend className="mb-1 text-sm font-medium">Steps (in order)</legend>
              {draft.steps.map((step, i) => (
                <div key={i} className="flex gap-2">
                  <SuggestInput
                    aria-label={`Step ${i + 1}`}
                    className="flex-1"
                    options={eventNames}
                    value={step}
                    onChange={(e) => update({ steps: setAt(draft.steps, i, e.target.value) })}
                    placeholder="Event name"
                  />
                  {draft.steps.length > 2 && (
                    <Button
                      type="button"
                      variant="danger"
                      aria-label={`Remove step ${i + 1}`}
                      onClick={() => update({ steps: removeAt(draft.steps, i) })}
                    >
                      Remove
                    </Button>
                  )}
                </div>
              ))}
              <div>
                <Button
                  type="button"
                  variant="link"
                  onClick={() => update({ steps: [...draft.steps, ""] })}
                >
                  Add step
                </Button>
              </div>
            </fieldset>
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="Conversion window">
                <Input
                  type="number"
                  min={1}
                  value={draft.windowValue}
                  onChange={(e) => update({ windowValue: e.target.value })}
                />
              </Field>
              <Field label="Window unit">
                <select
                  className={SELECT_CLASS}
                  value={draft.windowUnit}
                  onChange={(e) => update({ windowUnit: e.target.value as Draft["windowUnit"] })}
                >
                  <option value="hour">Hours</option>
                  <option value="day">Days</option>
                </select>
              </Field>
            </div>
          </>
        )}

        {draft.kind === "retention" && (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Cohort event (first seen)">
              <SuggestInput
                aria-label="Cohort event"
                options={eventNames}
                value={draft.bornEvent}
                onChange={(e) => update({ bornEvent: e.target.value })}
                placeholder="Event name"
              />
            </Field>
            <Field label="Return event">
              <SuggestInput
                aria-label="Return event"
                options={eventNames}
                value={draft.returnEvent}
                onChange={(e) => update({ returnEvent: e.target.value })}
                placeholder="Event name"
              />
            </Field>
            <Field label="Period">
              <select
                className={SELECT_CLASS}
                value={draft.period}
                onChange={(e) => update({ period: e.target.value as Draft["period"] })}
              >
                <option value="day">Day</option>
                <option value="week">Week</option>
              </select>
            </Field>
            <Field label="Number of periods">
              <Input
                type="number"
                min={1}
                max={52}
                value={draft.periods}
                onChange={(e) => update({ periods: e.target.value })}
              />
            </Field>
          </div>
        )}

        {showFilters && (
          <>
            <fieldset className="flex flex-col gap-2">
              <legend className="mb-1 text-sm font-medium">Filters</legend>
              {draft.filters.map((filter, i) => (
                <div key={i} className="flex flex-wrap gap-2">
                  <SuggestInput
                    aria-label={`Filter ${i + 1} property`}
                    options={propertyKeys}
                    value={filter.key}
                    onChange={(e) => setFilter(i, { key: e.target.value })}
                    placeholder="Property"
                  />
                  <select
                    aria-label={`Filter ${i + 1} operator`}
                    className={SELECT_CLASS}
                    value={filter.op}
                    onChange={(e) => setFilter(i, { op: e.target.value as Filter["op"] })}
                  >
                    <option value="eq">is</option>
                    <option value="neq">is not</option>
                    <option value="contains">contains</option>
                  </select>
                  <Input
                    aria-label={`Filter ${i + 1} value`}
                    value={filter.value}
                    onChange={(e) => setFilter(i, { value: e.target.value })}
                    placeholder="Value"
                  />
                  <Button
                    type="button"
                    variant="danger"
                    aria-label={`Remove filter ${i + 1}`}
                    onClick={() => update({ filters: removeAt(draft.filters, i) })}
                  >
                    Remove
                  </Button>
                </div>
              ))}
              <div>
                <Button
                  type="button"
                  variant="link"
                  onClick={() =>
                    update({ filters: [...draft.filters, { key: "", op: "eq", value: "" }] })
                  }
                >
                  Add filter
                </Button>
              </div>
            </fieldset>
            <Field label="Break down by">
              <SuggestInput
                aria-label="Break down by"
                options={propertyKeys}
                value={draft.breakdown}
                onChange={(e) => update({ breakdown: e.target.value })}
                placeholder="Optional property"
              />
            </Field>
          </>
        )}

        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="From">
            <Input type="date" value={draft.from} onChange={(e) => update({ from: e.target.value })} />
          </Field>
          <Field label="To">
            <Input type="date" value={draft.to} onChange={(e) => update({ to: e.target.value })} />
          </Field>
        </div>
      </div>

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
      {saved && <Alert variant="success">Saved.</Alert>}

      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" onClick={handleRun}>
          Run
        </Button>
        <Button
          type="button"
          variant="secondary"
          onClick={handleSave}
          disabled={saveMutation.isPending}
        >
          {initial ? "Save changes" : "Save insight"}
        </Button>
        {initial && (
          <Button type="button" variant="danger" onClick={handleDelete}>
            Delete
          </Button>
        )}
      </div>

      <section aria-label="Results" className="flex flex-col gap-3">
        {activeSpec === null && (
          <p className="text-gray-500">Choose what to measure, then press Run.</p>
        )}
        {resultQuery.isFetching && <Spinner />}
        {resultQuery.isError && <Alert>{resultQuery.error.message}</Alert>}
        {!resultQuery.isFetching && resultQuery.data && (
          <>
            <InsightResult result={resultQuery.data} />
            {resultQuery.data.cached && (
              <p className="text-xs text-gray-500">Served from cache (refreshes every minute).</p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
